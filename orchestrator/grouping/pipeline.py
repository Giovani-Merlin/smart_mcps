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
    build_group_dag,
    canonical_pair,
    detect_hub_roles,
    slice_atoms,
)
from orchestrator.grouping.plan_reader import TaskMapError, parse_task_map, strip_task_map
from orchestrator.grouping.speccer import write_specs
from orchestrator.model import Group, GroupingResult


class GrouperError(Exception):
    """The grouping pipeline could not produce a valid result."""


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
    it is the only part of this prefix that is not itself deterministic)."""

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
    base_context: str
    base_tokens: int


def compute_partition(
    plan_path: Path,
    repo_root: Path,
    config: OrchestratorConfig | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
    allow_unknown_symbols: bool = False,
) -> PartitionOutcome:
    """Mapper → graph → partition → group DAG (R19 seam): everything ``run_grouping``
    does before handing off to the speccer, callable on its own."""
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

    strategy = DefaultPartitionStrategy(
        work_fn=node_work_fn,
        budget_cap=budget_cap,
        hub_threshold=config.partition.hub_threshold,
        louvain_resolution=config.partition.louvain_resolution,
    )
    partition = strategy.partition(graph)
    dag = build_group_dag(graph, partition)
    roles = detect_hub_roles(graph, threshold=config.partition.hub_threshold)

    return PartitionOutcome(
        plan_text=plan_text,
        mapper_out=mapper_out,
        graph=graph,
        partition=partition,
        dag=dag,
        node_work={node: node_work_fn(node) for node in graph.nodes},
        budget_cap=budget_cap,
        hub_roles=roles,
        slice_atoms=slice_atoms(graph, roles),
        last_stage=strategy.last_stage,
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
    flags = list(mapper_out.flags)
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
        groups.append(
            Group(
                id=gid_str,
                name=spec.name,
                summary=spec.summary,
                spec=spec.spec,
                difficulty=difficulty,
                intensity=intensity_for(difficulty, config.difficulty),
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
