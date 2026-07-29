"""End-to-end grouping pipeline: plan document in → groups + DAG + base context out.

LLM at the edges (mapper in, speccer out), deterministic core in between (plan U4).
Partition computation stays strictly separate from execution — CoCoder fused them;
we deliberately do not (docs/research/cocoder-analysis.md §8 point 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.base_context import compile_base_context
from orchestrator.grouping.estimator import (
    DifficultySignals,
    difficulty_score,
    estimate_group_tokens,
    intensity_for,
    is_over_budget,
    node_work,
    partition_budget_cap,
)
from orchestrator.grouping.graphing import (
    CodegraphClient,
    EdgeWeights,
    TaskGraph,
    build_task_graph,
    source_bytes_of,
)
from orchestrator.grouping.llm import JsonRunner, claude_json_runner
from orchestrator.grouping.mapper import MapperOutput, map_tasks
from orchestrator.grouping.partition import (
    DefaultPartitionStrategy,
    Partition,
    WorkFn,
    build_group_dag,
    canonical_pair,
    detect_hub_roles,
    slice_atoms,
)
from orchestrator.grouping.plan_reader import TaskMapError, parse_task_map, strip_task_map
from orchestrator.grouping.speccer import write_specs
from orchestrator.grouping.trace import (
    BudgetArithmetic,
    GroupDifficultyEntry,
    NodeWorkEntry,
    TraceRecorder,
)
from orchestrator.model import Group, GroupingResult


def _node_work_entries(graph: TaskGraph, config) -> list[NodeWorkEntry]:
    """Per-node work broken into its components (plan U8), mirroring
    ``estimator.node_work``'s formula without importing it back in — this
    module isn't in that unit's file list, and the arithmetic is one line."""
    entries = []
    for node in sorted(graph.nodes):
        meta = graph.metadata.get(node, {})
        source_bytes = int(meta.get("source_bytes", 0) or 0)
        file_count = len(meta.get("files", ()) or ()) + len(meta.get("prospective_files", ()) or ())
        bytes_tokens = source_bytes / config.bytes_per_token * config.slack_multiplier
        file_allowance_tokens = file_count * config.per_file_tool_allowance
        entries.append(
            NodeWorkEntry(
                node=node,
                source_bytes=source_bytes,
                file_count=file_count,
                bytes_tokens=bytes_tokens,
                file_allowance_tokens=file_allowance_tokens,
                total=bytes_tokens + file_allowance_tokens,
            )
        )
    return entries


class GrouperError(Exception):
    """The grouping pipeline could not produce a valid result."""


def _assert_slice_integrity(atoms: dict[str, list[str]], partition: Partition) -> None:
    """Safety net for plan U6: split_over_budget (U3), the acyclic merge guard
    (U4) and the SCC repair's re-split (U5) all treat a declared slice as one
    indivisible block, so no group should ever end up holding a strict subset
    of one. A violation here means a bug in one of those stages, not something
    a user did — enforcement lives at the stage that could break the
    invariant; this is only the assertion on the final result.
    """
    for label, members in sorted(atoms.items()):
        gids = {partition[m] for m in members}
        if len(gids) > 1:
            raise GrouperError(
                f"internal error: slice {label!r} split across groups {sorted(gids)} "
                f"(members: {', '.join(sorted(members))}) — this should be unreachable"
            )


def _check_slice_overflow(
    atoms: dict[str, list[str]],
    node_work_fn: WorkFn,
    budget_cap: float,
    allow_oversized_slice: bool,
    flags: list[str],
) -> None:
    """R5: a slice's own summed work can exceed the cap no matter how the rest
    of the graph is partitioned — ``split_over_budget`` (U3) already keeps such
    a slice whole rather than dissolving it, so this is where the overshoot
    itself is judged. Loud by default, naming the slice, every member and its
    work, the cap and the overshoot; ``allow_oversized_slice`` (CLI
    ``--allow-oversized-slice``, config ``[partition] allow_oversized_slice`` —
    exactly equivalent) accepts the overshoot instead and records it in
    ``flags`` for the caller to surface.
    """
    for label, members in sorted(atoms.items()):
        work_by_member = {m: node_work_fn(m) for m in sorted(members)}
        total = sum(work_by_member.values())
        if total <= budget_cap:
            continue
        overshoot = total - budget_cap
        detail = ", ".join(f"{m}={work_by_member[m]:.0f}" for m in sorted(work_by_member))
        message = (
            f"slice {label!r} cannot fit in one group: members [{detail}] sum to "
            f"{total:.0f} work, exceeding the {budget_cap:.0f} cap by {overshoot:.0f}"
        )
        if not allow_oversized_slice:
            raise GrouperError(message)
        flags.append(
            f"partition: slice {label!r} accepted {overshoot:.0f} over the "
            f"{budget_cap:.0f} cap (--allow-oversized-slice / allow_oversized_slice)"
        )


SELF_MODIFICATION_FLAG = (
    "self-modification: this plan's mappings touch orchestrator/ — the changes "
    "take effect on the next run, not this one (see orchestrator/README.md)"
)


def _flag_self_modification(mapper_out: MapperOutput) -> None:
    """R15: warn when a plan edits the orchestrator that is about to drive it.

    Workers run from the installed console script, not the worktree they edit,
    so a change to ``orchestrator/`` can never be exercised by the run that
    makes it (D12) — surfaced here, at grouping time, rather than discovered
    mid-run.
    """
    touches_orchestrator = any(
        file == "orchestrator" or file.startswith("orchestrator/")
        for mapping in mapper_out.mappings
        for file in (*mapping.files, *mapping.prospective_files)
    )
    if touches_orchestrator:
        mapper_out.flags.append(SELF_MODIFICATION_FLAG)


def group_label(gid: int) -> str:
    """Group id → display id, e.g. ``g1``. Shared by the partition-only report
    (cli.py --no-spec) and the full assembly below — group numbering must match."""
    return f"g{gid + 1}"


@dataclass(frozen=True)
class PartitionOutcome:
    """The deterministic, sub-second prefix of ``run_grouping`` (R19): mapper →
    graph → partition → group DAG. Zero LLM calls whenever the plan carries a
    task map (the mapper-LLM fallback below still runs here for foreign plans —
    it is the only part of this prefix that is not itself deterministic).

    ``flags`` (R10) carries the partitioner's own warnings — currently just a
    repaired group that could not be re-split back under budget (plan U5) —
    distinct from ``mapper_out.flags``, which are mapper-level warnings.
    """

    plan_text: str
    mapper_out: MapperOutput
    graph: TaskGraph
    partition: Partition
    dag: dict[int, set[int]]
    node_work: dict[str, float]
    budget_cap: float
    hub_roles: dict[str, str]
    slice_atoms: dict[str, list[str]]
    last_stage: str | None
    flags: list[str]
    base_context: str
    base_tokens: int


def compute_partition(
    plan_path: Path,
    repo_root: Path,
    config: OrchestratorConfig | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
    allow_unknown_symbols: bool = False,
    recorder: TraceRecorder | None = None,
) -> PartitionOutcome:
    """Mapper → graph → partition → group DAG (R19 seam): everything ``run_grouping``
    does before handing off to the speccer, callable on its own.

    ``recorder`` is an optional, default-``None`` seam (plan U8): passing one
    fills a ``GroupingTrace`` alongside the computation without changing it —
    every fixture partitions identically with or without one attached.
    """
    if not plan_path.is_file():
        raise GrouperError(f"plan document not found: {plan_path}")
    config = config or OrchestratorConfig()
    llm_runner = llm_runner or claude_json_runner
    client = client or CodegraphClient(repo_root=repo_root)
    failure_dir = repo_root / ".orchestrator" / "failures"

    plan_text = plan_path.read_text()
    client.sync()
    codegraph_files = client.files_overview()
    # Deterministic fast path: a plan carrying a task map already answered what
    # the mapper LLM would have to guess. Malformed maps fail hard (silent
    # fallback would hide prose↔map drift); absent maps keep foreign plans working.
    try:
        mapper_out = parse_task_map(plan_text, client, allow_unknown_symbols=allow_unknown_symbols)
    except TaskMapError as exc:
        raise GrouperError(f"task map: {exc}") from exc
    if mapper_out is None:
        mapper_out = map_tasks(
            plan_text, llm_runner, client, failure_dir=failure_dir, codegraph_files=codegraph_files
        )
    else:
        mapper_out.flags.insert(0, "task map: parsed from plan — mapper LLM skipped")
    if not mapper_out.mappings:
        raise GrouperError("mapper produced no tasks from the plan document")
    _flag_self_modification(mapper_out)

    weights = EdgeWeights(**config.edge_weights.model_dump(exclude={"prose_neighbor"}))
    graph = build_task_graph(mapper_out.mappings, client, weights)
    graph = _with_prose_fallback(graph, mapper_out, config.edge_weights.prose_neighbor)

    base_context = compile_base_context(repo_root, plan_path, codegraph_files)
    base_tokens = int(len(base_context) / config.estimator.bytes_per_token)
    budget_cap = partition_budget_cap(base_tokens, config.estimator)

    def node_work_fn(node: str) -> float:
        return node_work(graph.metadata.get(node, {}), config.estimator)

    if recorder is not None:
        recorder.set_config(config.model_dump())
        recorder.set_input_graph(graph.nodes, graph.affinity, graph.dependencies)
        recorder.set_node_work(_node_work_entries(graph, config.estimator))
        head = (
            base_tokens + config.estimator.spec_tokens_allowance
        ) * config.estimator.slack_multiplier
        recorder.set_budget(
            BudgetArithmetic(
                base_tokens=base_tokens,
                spec_tokens_allowance=config.estimator.spec_tokens_allowance,
                slack_multiplier=config.estimator.slack_multiplier,
                token_budget=config.estimator.token_budget,
                head=head,
                budget_cap=budget_cap,
            )
        )

    strategy = DefaultPartitionStrategy(
        work_fn=node_work_fn,
        budget_cap=budget_cap,
        hub_threshold=config.partition.hub_threshold,
        louvain_resolution=config.partition.louvain_resolution,
        recorder=recorder,
    )
    partition = strategy.partition(graph)
    roles = detect_hub_roles(graph, threshold=config.partition.hub_threshold)
    atoms = slice_atoms(graph, roles)
    _assert_slice_integrity(atoms, partition)
    flags = list(strategy.flags)
    _check_slice_overflow(
        atoms=atoms,
        node_work_fn=node_work_fn,
        budget_cap=budget_cap,
        allow_oversized_slice=config.partition.allow_oversized_slice,
        flags=flags,
    )
    dag = build_group_dag(graph, partition)

    # g7's trace capture must run after g5's overflow gate, not before: the gate
    # can append an override flag, and `dag` is what the recorder reads. Recording
    # `flags` rather than `strategy.flags` keeps the trace honest about that
    # appended flag — the two are identical unless an oversized slice was allowed.
    if recorder is not None:
        recorder.record_slice_atoms(atoms)
        recorder.set_dag(dag)
        recorder.set_last_stage(strategy.last_stage)
        recorder.set_flags(mapper_out.flags, flags)

    return PartitionOutcome(
        plan_text=plan_text,
        mapper_out=mapper_out,
        graph=graph,
        partition=partition,
        dag=dag,
        node_work={node: node_work_fn(node) for node in graph.nodes},
        budget_cap=budget_cap,
        hub_roles=roles,
        slice_atoms=atoms,
        last_stage=strategy.last_stage,
        flags=flags,
        base_context=base_context,
        base_tokens=base_tokens,
    )


def run_grouping(
    plan_path: Path,
    repo_root: Path,
    config: OrchestratorConfig | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
    allow_unknown_symbols: bool = False,
    recorder: TraceRecorder | None = None,
) -> tuple[GroupingResult, str]:
    """Full pipeline: mapper (LLM) → graph → partition → estimator → speccer (LLM)."""
    config = config or OrchestratorConfig()
    llm_runner = llm_runner or claude_json_runner
    client = client or CodegraphClient(repo_root=repo_root)
    failure_dir = repo_root / ".orchestrator" / "failures"

    outcome = compute_partition(
        plan_path=plan_path,
        repo_root=repo_root,
        config=config,
        llm_runner=llm_runner,
        client=client,
        allow_unknown_symbols=allow_unknown_symbols,
        recorder=recorder,
    )
    graph, partition, dag = outcome.graph, outcome.partition, outcome.dag
    mapper_out = outcome.mapper_out
    base_context, base_tokens = outcome.base_context, outcome.base_tokens

    members_by_gid: dict[int, list[str]] = {}
    for node, gid in partition.items():
        members_by_gid.setdefault(gid, []).append(node)

    skeletons = {
        group_label(gid): {
            "tasks": sorted(members),
            "descriptions": {t: mapper_out.descriptions.get(t, "") for t in sorted(members)},
            "files": _union_files(graph, members),
        }
        for gid, members in sorted(members_by_gid.items())
    }
    specs = write_specs(
        strip_task_map(outcome.plan_text), skeletons, llm_runner, failure_dir=failure_dir
    )

    upstream_of: dict[int, list[int]] = {gid: [] for gid in members_by_gid}
    for up_gid, downs in dag.items():
        for down_gid in downs:
            upstream_of[down_gid].append(up_gid)

    roles = outcome.hub_roles
    flags = list(mapper_out.flags) + list(outcome.flags)
    groups: list[Group] = []
    for gid, members in sorted(members_by_gid.items()):
        gid_str = group_label(gid)
        spec = specs[gid_str]
        files = _union_files(graph, members)
        metas = [graph.metadata.get(node, {}) for node in sorted(members)]
        # Size the group from its union of files: a file shared by several member
        # tasks (the usual reason they clustered) must count once, not once per task.
        estimated = estimate_group_tokens(
            source_bytes=source_bytes_of(client.repo_root, files),
            file_count=len(files),
            spec_tokens=int(len(spec.spec) / config.estimator.bytes_per_token),
            base_tokens=base_tokens,
            config=config.estimator,
        )
        if is_over_budget(estimated, config.estimator):
            flags.append(
                f"estimator: group {gid_str} estimate {estimated} exceeds budget "
                f"{config.estimator.token_budget} and cannot be split further"
            )
        member_set = set(members)
        signals = DifficultySignals(
            files_touched=len(files),
            max_fan_in=max((int(m.get("max_symbol_fan_in", 0) or 0) for m in metas), default=0),
            max_fan_out=max((int(m.get("max_symbol_fan_out", 0) or 0) for m in metas), default=0),
            hub_touches=sum(1 for node in members if roles.get(node) != "core"),
            cross_group_edges=sum(
                1 for up, down in graph.dependencies if (up in member_set) != (down in member_set)
            ),
            verification_items=len(spec.verification),
        )
        difficulty = difficulty_score(signals, config.difficulty)
        intensity = intensity_for(difficulty, config.difficulty)
        if recorder is not None:
            recorder.record_group_difficulty(
                GroupDifficultyEntry(
                    group_id=gid_str,
                    files_touched=signals.files_touched,
                    max_fan_in=signals.max_fan_in,
                    max_fan_out=signals.max_fan_out,
                    hub_touches=signals.hub_touches,
                    cross_group_edges=signals.cross_group_edges,
                    verification_items=signals.verification_items,
                    difficulty=difficulty,
                    intensity=intensity.value,
                    d_review=config.difficulty.d_review,
                    d_hard=config.difficulty.d_hard,
                )
            )
        groups.append(
            Group(
                id=gid_str,
                name=spec.name,
                summary=spec.summary,
                spec=spec.spec,
                difficulty=difficulty,
                intensity=intensity,
                dependencies=sorted(group_label(up) for up in upstream_of[gid]),
                verification=spec.verification,
                tasks=sorted(members),
                files=files,
                estimated_tokens=estimated,
            )
        )

    result = GroupingResult(
        plan_path=_portable_path(plan_path, repo_root), groups=groups, flags=flags
    )
    if recorder is not None:
        recorder.set_final_flags(flags)
    return result, base_context


def serialize_grouping(result: GroupingResult) -> str:
    """Canonical groups.json bytes — the determinism contract (plan U4)."""
    return result.model_dump_json(indent=2) + "\n"


def _union_files(graph: TaskGraph, members: list[str]) -> list[str]:
    """Existing plus prospective files: workers create the prospective ones."""
    files: set[str] = set()
    for node in members:
        meta = graph.metadata.get(node, {})
        files.update(meta.get("files", ()) or ())
        files.update(meta.get("prospective_files", ()) or ())
    return sorted(files)


def _with_prose_fallback(graph: TaskGraph, mapper_out: MapperOutput, weight: float) -> TaskGraph:
    """Attach region-less tasks to their plan-order neighbor with a small affinity.

    Pairs are collected first and weighted once each: two adjacent region-less
    tasks nominate the same pair from both sides, which must not double its weight.
    """
    order = [m.task_id for m in mapper_out.mappings]
    # A task with prospective files is no longer region-less: its planned files
    # already give it real shared-file affinity.
    regionless = [
        m.task_id
        for m in mapper_out.mappings
        if not m.files and not m.symbols and not m.prospective_files
    ]
    if not regionless or len(order) < 2 or weight <= 0:
        return graph
    fallback_pairs = set()
    for task in regionless:
        index = order.index(task)
        neighbor = order[index - 1] if index > 0 else order[1]
        fallback_pairs.add(canonical_pair(task, neighbor))
    affinity = dict(graph.affinity)
    for pair in sorted(fallback_pairs):
        affinity[pair] = affinity.get(pair, 0.0) + weight
    return TaskGraph(
        nodes=graph.nodes,
        affinity=affinity,
        dependencies=graph.dependencies,
        metadata=graph.metadata,
    )


def _portable_path(plan_path: Path, repo_root: Path) -> str:
    try:
        return str(plan_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(plan_path)
