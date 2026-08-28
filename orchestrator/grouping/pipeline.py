"""End-to-end grouping pipeline: plan document in → groups + DAG + base context out.

LLM only at the mapper edge (foreign plans with no embedded task map); specs are
assembled deterministically from the plan's own unit sections (plan U2), zero LLM.
Partition computation stays strictly separate from execution — CoCoder fused them;
we deliberately do not (docs/research/cocoder-analysis.md §8 point 1).
"""

from __future__ import annotations

import functools
import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.assembler import ASSEMBLED_FLAG, AssemblyInputs, assemble_group_specs
from orchestrator.grouping.base_context import compile_base_context
from orchestrator.grouping.errors import ErrorAccumulator
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
    SIGNAL_PROSE_NEIGHBOR,
    CodegraphClient,
    EdgeContribution,
    EdgeProvenance,
    EdgeWeights,
    TaskGraph,
    await_index_quiescence,
    build_task_graph,
    index_fingerprint,
    source_bytes_of,
)
from orchestrator.grouping.llm import JsonRunner, LlmCallRecorder, claude_json_runner
from orchestrator.grouping.mapper import MapperOutput, map_tasks
from orchestrator.grouping.partition import (
    LOUVAIN_SEED,
    DefaultPartitionStrategy,
    Partition,
    WorkFn,
    build_group_dag,
    canonical_pair,
    detect_hub_roles,
    slice_atoms,
)
from orchestrator.grouping.plan_reader import TaskMapError, parse_task_map
from orchestrator.grouping.plan_sections import parse_plan_sections
from orchestrator.grouping.scorecard import compute_scorecard
from orchestrator.grouping.trace import (
    BudgetArithmetic,
    GroupDifficultyEntry,
    NodeWorkEntry,
    TraceRecorder,
)
from orchestrator.model import Group, GroupingResult


def _git_provenance(repo_root: Path) -> tuple[str, bool]:
    """Repo commit SHA and worktree-dirty flag (plan U5). Best-effort: a plan
    document grouped outside a git repo (or with no git binary available)
    records empty/clean rather than failing the grouping over provenance."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "", False
    return sha, dirty


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


# The residual plan U7 accepts explicitly: the mapper is an LLM shelled with no
# temperature or seed control, so a matching index fingerprint proves the
# partition's *input* is unchanged, not that re-running the mapper would
# reproduce the same task→file mapping. Every mismatch message repeats this so
# an operator reading `--allow-index-drift`'s warning never mistakes
# index-stability for full reproducibility.
INDEX_DRIFT_RESIDUAL_NOTE = (
    "note: this makes grouping index-stable, not reproducible — the mapper is "
    "an unseeded LLM call and can still choose a different task→file mapping "
    "against a byte-identical index"
)


class IndexFingerprintMismatch(GrouperError):
    """Plan U7: a recorded grouping's index fingerprint no longer matches the
    current codegraph index — the partition on disk was built against a
    different index than the one the run would execute against now."""

    def __init__(self, recorded: str, current: str) -> None:
        self.recorded = recorded
        self.current = current
        super().__init__(
            "index fingerprint mismatch: this grouping was built against index "
            f"{recorded}, the current index is {current} — re-group with "
            "`smart-mcps-orchestrate group <plan> --name <name>` to pick up the "
            "new index, or pass --allow-index-drift to force a re-partition now "
            f"({INDEX_DRIFT_RESIDUAL_NOTE})"
        )


def verify_index_fingerprint(
    recorded: str,
    client: CodegraphClient,
    *,
    allow_drift: bool,
    log: Callable[[str], None] | None = None,
) -> tuple[str, bool]:
    """Plan U7: compare a grouping's recorded ``index_fingerprint`` (written
    once at grouping time into ``ProvenanceEntry`` — see
    ``pipeline.run_grouping``/``compute_partition`` — but until now never read
    back) against the current index.

    Returns ``(current_fingerprint, matched)``. A mismatch raises
    ``IndexFingerprintMismatch`` unless ``allow_drift`` is set, in which case
    the mismatch is logged as a loud warning instead and reported back via
    ``matched=False`` — the caller is responsible for treating that as a
    signal to re-partition rather than silently reusing the stale result;
    this function never partitions anything itself.
    """
    current = index_fingerprint(client.logical_export())
    if current == recorded:
        return current, True
    message = (
        f"warning: index drift — this grouping was built against index {recorded}, "
        f"the current index is {current}; forcing a re-partition "
        f"(--allow-index-drift). {INDEX_DRIFT_RESIDUAL_NOTE}"
    )
    if not allow_drift:
        raise IndexFingerprintMismatch(recorded, current)
    if log is not None:
        log(message)
    else:
        print(message)
    return current, False


# Stage/spec progress lines (plan U24): a `group` invocation is otherwise silent
# for as long as the mapper and speccer LLM calls take, so this is the seam the
# CLI hangs an unbuffered `print(..., flush=True)` off of.
ProgressFn = Callable[[str], None]


def _emit(progress: ProgressFn | None, message: str) -> None:
    if progress is not None:
        progress(message)


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
    coder_slack_multiplier: float,
) -> None:
    """R5: a slice's own summed work can exceed the cap no matter how the rest
    of the graph is partitioned — ``split_over_budget`` (U3) already keeps such
    a slice whole rather than dissolving it, so this is where the overshoot
    itself is judged. Loud by default, naming the slice, every member and its
    work, the cap and the overshoot; ``allow_oversized_slice`` (CLI
    ``--allow-oversized-slice``, config ``[partition] allow_oversized_slice`` —
    exactly equivalent) accepts the overshoot instead and records it in
    ``flags`` for the caller to surface.

    Every over-cap slice is collected before raising (plan U6/C1) — a plan
    with several oversized slices names all of them in one ``GrouperError``
    instead of costing one ``group`` invocation per slice.

    ``node_work_fn`` (and therefore ``budget_cap``) is already scaled to a
    coder — plan U8/C2 requires the operator-facing message to distinguish
    that "coder work" figure from the unscaled "node work" it was derived
    from, and to state the multiplier, rather than reporting one bare "work"
    number that could be read as either.
    """
    errors = ErrorAccumulator()
    for label, members in sorted(atoms.items()):
        coder_work_by_member = {m: node_work_fn(m) for m in sorted(members)}
        node_work_by_member = {
            m: v / coder_slack_multiplier for m, v in coder_work_by_member.items()
        }
        total_coder_work = sum(coder_work_by_member.values())
        if total_coder_work <= budget_cap:
            continue
        overshoot = total_coder_work - budget_cap
        total_node_work = sum(node_work_by_member.values())
        detail = ", ".join(
            f"{m}={node_work_by_member[m]:.0f} node work / {coder_work_by_member[m]:.0f} coder work"
            for m in sorted(coder_work_by_member)
        )
        message = (
            f"slice {label!r} cannot fit in one group: members [{detail}] sum to "
            f"{total_node_work:.0f} node work / {total_coder_work:.0f} coder work "
            f"(coder_slack_multiplier={coder_slack_multiplier:g}), exceeding the "
            f"{budget_cap:.0f} coder work cap by {overshoot:.0f}"
        )
        if not allow_oversized_slice:
            errors.add(message)
            continue
        flags.append(
            f"partition: slice {label!r} accepted {overshoot:.0f} coder work over the "
            f"{budget_cap:.0f} cap (--allow-oversized-slice / allow_oversized_slice)"
        )
    errors.raise_all(GrouperError)


def _check_degenerate_partition(
    repair_flags: list[str],
    allow_degenerate_partition: bool,
) -> None:
    """A cycle repair that could not re-split back under budget is a *failure*.

    ``repair_cycles`` merges a cyclic group-SCC and re-splits it by dependency
    wave; when no acyclic re-split under budget exists it leaves one over-cap
    group and appends a flag. Nothing blocked on that flag, so a saturated
    dependency graph produced a legal single-group "success" 3.8x over the cap
    and `group` exited 0 (docs/orchestrator-grouping.md, limitation 5).

    Loud by default, mirroring ``_check_slice_overflow``: the overshoot is
    reported with the partition's own message, and the escape hatch
    (``--allow-degenerate-partition`` / ``[partition] allow_degenerate_partition``
    — exactly equivalent) accepts it instead. Unlike an oversized slice, this is
    never something the operator declared, so the default is an error rather than
    a warning.
    """
    if not repair_flags or allow_degenerate_partition:
        return
    detail = "\n  ".join(repair_flags)
    raise GrouperError(
        "partition is degenerate — cycle repair collapsed groups it could not "
        f"re-split back under budget:\n  {detail}\n"
        "This almost always means the task dependency graph is saturated rather "
        "than the plan being too large. Inspect grouping-trace.json ('repairs') for "
        "the offending edges, or accept it with --allow-degenerate-partition."
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


EDGE_PROVENANCE_VERSION = 1


def edge_provenance_document(graph: TaskGraph, partition: Partition) -> dict:
    """The ``edge-provenance.json`` payload: per-edge ledgers plus a group rollup.

    A sidecar rather than an extension of ``grouping-trace.json`` (plan P2): the
    trace is byte-stable by contract and is what operators diff, and its
    ``input_graph`` is post-cycle-drop, so it structurally cannot hold withdrawn
    edges. The rollup is computed here, at write time, so ``partition.py`` needs
    nothing beyond the ``provenance`` field it carries through.
    """
    provenance = graph.provenance
    if not isinstance(provenance, EdgeProvenance):
        return {
            "version": EDGE_PROVENANCE_VERSION,
            "max_contributions_per_edge": 0,
            "affinity": [],
            "dependencies": [],
            "withdrawn": [],
            "groups": [],
            "note": "graph carried no provenance ledgers",
        }

    affinity_edges = [
        {"a": pair[0], "b": pair[1], **provenance.affinity[pair].as_dict()}
        for pair in sorted(provenance.affinity)
    ]
    dependency_edges = [
        {"upstream": key[0], "downstream": key[1], **provenance.dependencies[key].as_dict()}
        for key in sorted(provenance.dependencies)
    ]
    withdrawn = [
        edge.as_dict()
        for edge in sorted(provenance.withdrawn, key=lambda e: (e.upstream, e.downstream))
    ]

    members_by_gid: dict[int, list[str]] = {}
    for node, gid in sorted(partition.items()):
        members_by_gid.setdefault(gid, []).append(node)

    groups = []
    for gid, members in sorted(members_by_gid.items()):
        member_set = set(members)
        internal = 0.0
        external = 0.0
        by_kind: dict[str, float] = {}
        for pair, ledger in provenance.affinity.items():
            inside = len(member_set & set(pair))
            if inside == 2:
                internal += ledger.total_weight
                for contribution in ledger.contributions:
                    by_kind[contribution.kind] = (
                        by_kind.get(contribution.kind, 0.0) + contribution.scaled_weight
                    )
            elif inside == 1:
                external += ledger.total_weight
        groups.append(
            {
                "group_id": group_label(gid),
                "tasks": list(members),
                "internal_affinity_weight": internal,
                "external_affinity_weight": external,
                # Recorded contributions only: a truncated edge's dropped weight is
                # counted in ``internal_affinity_weight`` but has no kind to bill it to.
                "internal_affinity_by_kind": dict(sorted(by_kind.items())),
                "upstream_dependency_edges": sorted(
                    [key[0], key[1]]
                    for key in provenance.dependencies
                    if key[1] in member_set and key[0] not in member_set
                ),
            }
        )

    return {
        "version": EDGE_PROVENANCE_VERSION,
        "max_contributions_per_edge": provenance.max_contributions_per_edge,
        "affinity": affinity_edges,
        "dependencies": dependency_edges,
        "withdrawn": withdrawn,
        "groups": groups,
    }


def serialize_edge_provenance(document: dict) -> str:
    """Canonical ``edge-provenance.json`` bytes."""
    return json.dumps(document, indent=2) + "\n"


@dataclass
class EdgeProvenanceRecorder:
    """Inert observation seam, matching ``TraceRecorder``'s contract: attaching one
    fills ``document`` alongside the computation without changing it."""

    document: dict | None = None

    def capture(self, graph: TaskGraph, partition: Partition) -> None:
        self.document = edge_provenance_document(graph, partition)


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


@dataclass(frozen=True)
class GraphBuildOutcome:
    """The mapper → graph prefix of ``compute_partition`` (plan U11 seam),
    extracted so ``--advise`` can build the task graph **once** and partition
    it at every granularity preset, instead of repeating the mapper and
    codegraph work per preset. ``compute_partition`` itself is just this
    followed by one partition; nothing about its behavior changes.
    """

    plan_text: str
    mapper_out: MapperOutput
    graph: TaskGraph
    quiesced_fingerprint: str
    base_context: str
    base_tokens: int
    budget_cap: float


def build_partition_graph(
    plan_path: Path,
    repo_root: Path,
    config: OrchestratorConfig,
    llm_runner: JsonRunner,
    client: CodegraphClient,
    allow_unknown_symbols: bool = False,
    recorder: TraceRecorder | None = None,
    llm_recorder: LlmCallRecorder | None = None,
    progress: ProgressFn | None = None,
) -> GraphBuildOutcome:
    """Mapper → graph (R19/plan U11 seam): everything before a partition
    strategy runs. Callers must have already defaulted ``config``/``llm_runner``/
    ``client`` — this function takes them as required so it never silently
    builds a second ``CodegraphClient`` behind a caller's back.
    """
    if not plan_path.is_file():
        raise GrouperError(f"plan document not found: {plan_path}")
    failure_dir = repo_root / ".orchestrator" / "failures"

    _emit(progress, "stage: mapper")
    plan_text = plan_path.read_text()
    client.sync()
    _emit(progress, "stage: quiescence")
    # Plan U6: `sync` returning is not proof the index has stopped moving — the
    # fingerprint that motivated this handshake churned three times in fifteen
    # minutes at one commit while `sync` reported "already up to date". Partition
    # against the settled value, not whatever the very next read happens to be.
    quiesced_fingerprint = await_index_quiescence(client, recorder=recorder)
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
            plan_text,
            llm_runner,
            client,
            failure_dir=failure_dir,
            codegraph_files=codegraph_files,
            recorder=llm_recorder,
        )
    else:
        mapper_out.flags.insert(0, "task map: parsed from plan — mapper LLM skipped")
    if not mapper_out.mappings:
        raise GrouperError("mapper produced no tasks from the plan document")
    _flag_self_modification(mapper_out)

    _emit(progress, "stage: graph")
    weights = EdgeWeights(**config.edge_weights.model_dump(exclude={"prose_neighbor"}))
    graph = build_task_graph(mapper_out.mappings, client, weights, flags=mapper_out.flags)
    graph = _with_prose_fallback(graph, mapper_out, config.edge_weights.prose_neighbor)
    # The fallback only adds affinity, but it rebuilds the graph — re-assert rather
    # than trust that it stays that way.
    graph.assert_acyclic_dependencies()

    base_context = compile_base_context(repo_root, plan_path, codegraph_files)
    base_tokens = int(len(base_context) / config.estimator.bytes_per_token)
    budget_cap = partition_budget_cap(base_tokens, config.estimator)

    if recorder is not None:
        recorder.set_config(config.model_dump())
        recorder.set_input_graph(graph.nodes, graph.affinity, graph.dependencies)
        recorder.set_node_work(_node_work_entries(graph, config.estimator))
        # Mirrors partition_budget_cap's head, coder scaling included — a trace
        # that reported the unscaled head would not explain the cap beside it.
        head = (
            (base_tokens + config.estimator.spec_tokens_allowance)
            * config.estimator.slack_multiplier
            * config.estimator.coder_slack_multiplier
        )
        recorder.set_budget(
            BudgetArithmetic(
                base_tokens=base_tokens,
                spec_tokens_allowance=config.estimator.spec_tokens_allowance,
                slack_multiplier=config.estimator.slack_multiplier,
                coder_slack_multiplier=config.estimator.coder_slack_multiplier,
                token_budget=config.estimator.token_budget,
                head=head,
                budget_cap=budget_cap,
            )
        )

    return GraphBuildOutcome(
        plan_text=plan_text,
        mapper_out=mapper_out,
        graph=graph,
        quiesced_fingerprint=quiesced_fingerprint,
        base_context=base_context,
        base_tokens=base_tokens,
        budget_cap=budget_cap,
    )


def compute_partition(
    plan_path: Path,
    repo_root: Path,
    config: OrchestratorConfig | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
    allow_unknown_symbols: bool = False,
    recorder: TraceRecorder | None = None,
    llm_recorder: LlmCallRecorder | None = None,
    provenance_recorder: EdgeProvenanceRecorder | None = None,
    progress: ProgressFn | None = None,
) -> PartitionOutcome:
    """Mapper → graph → partition → group DAG (R19 seam): everything ``run_grouping``
    does before handing off to the speccer, callable on its own.

    ``recorder`` is an optional, default-``None`` seam (plan U8): passing one
    fills a ``GroupingTrace`` alongside the computation without changing it —
    every fixture partitions identically with or without one attached.

    ``progress``, if given, is called with one short stage-name string as each
    stage of the pipeline starts (plan U24) — the CLI's seam for turning three
    and a half minutes of silence into a streamable job log.
    """
    config = config or OrchestratorConfig()
    llm_runner = llm_runner or functools.partial(
        claude_json_runner, model=config.session.speccer_model
    )
    client = client or CodegraphClient(repo_root=repo_root)

    build = build_partition_graph(
        plan_path=plan_path,
        repo_root=repo_root,
        config=config,
        llm_runner=llm_runner,
        client=client,
        allow_unknown_symbols=allow_unknown_symbols,
        recorder=recorder,
        llm_recorder=llm_recorder,
        progress=progress,
    )
    plan_text = build.plan_text
    mapper_out = build.mapper_out
    graph = build.graph
    quiesced_fingerprint = build.quiesced_fingerprint
    base_context = build.base_context
    base_tokens = build.base_tokens
    budget_cap = build.budget_cap

    def node_work_fn(node: str) -> float:
        return node_work(graph.metadata.get(node, {}), config.estimator)

    strategy = DefaultPartitionStrategy(
        work_fn=node_work_fn,
        budget_cap=budget_cap,
        hub_threshold=config.partition.hub_threshold,
        louvain_resolution=config.partition.louvain_resolution,
        granularity=config.partition.granularity,
        target_fill_ratio=config.partition.target_fill_ratio,
        recorder=recorder,
    )
    _emit(progress, "stage: partition")
    partition = strategy.partition(graph)
    # Before _check_slice_overflow appends to the same list: at this point
    # strategy.flags carries only the repair-overshoot messages, which is exactly
    # what the degeneracy gate judges.
    _check_degenerate_partition(
        repair_flags=list(strategy.flags),
        allow_degenerate_partition=config.partition.allow_degenerate_partition,
    )
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
        coder_slack_multiplier=config.estimator.coder_slack_multiplier,
    )
    dag = build_group_dag(graph, partition)

    # g7's trace capture must run after g5's overflow gate, not before: the gate
    # can append an override flag, and `dag` is what the recorder reads. Recording
    # `flags` rather than `strategy.flags` keeps the trace honest about that
    # appended flag — the two are identical unless an oversized slice was allowed.
    node_work_map = {node: node_work_fn(node) for node in graph.nodes}
    if recorder is not None:
        recorder.record_slice_atoms(atoms)
        recorder.set_dag(dag)
        recorder.set_last_stage(strategy.last_stage)
        recorder.set_flags(mapper_out.flags, flags)
        recorder.set_scorecard(
            compute_scorecard(
                graph=graph,
                partition=partition,
                node_work=node_work_map,
                budget_cap=budget_cap,
                dag=dag,
                slice_atoms=atoms,
            )
        )
        repo_commit_sha, worktree_dirty = _git_provenance(repo_root)
        recorder.set_provenance(
            timestamp=datetime.now(UTC).isoformat(),
            plan_path=_portable_path(plan_path, repo_root),
            plan_content_sha256=hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
            repo_commit_sha=repo_commit_sha,
            worktree_dirty=worktree_dirty,
            index_fingerprint=quiesced_fingerprint,
            louvain_seed=LOUVAIN_SEED,
            louvain_resolution=config.partition.louvain_resolution,
        )

    # Same inert-observation contract as the trace recorder, and the same single
    # call site for both `group` and `group --no-spec`: the sidecar is a function of
    # the graph plus the final partition, both of which are settled here.
    if provenance_recorder is not None:
        provenance_recorder.capture(graph, partition)

    return PartitionOutcome(
        plan_text=plan_text,
        mapper_out=mapper_out,
        graph=graph,
        partition=partition,
        dag=dag,
        node_work=node_work_map,
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
    llm_recorder: LlmCallRecorder | None = None,
    provenance_recorder: EdgeProvenanceRecorder | None = None,
    progress: ProgressFn | None = None,
) -> tuple[GroupingResult, str]:
    """Full pipeline: mapper (LLM) → graph → partition → estimator → deterministic
    spec assembly (zero LLM, plan U2)."""
    config = config or OrchestratorConfig()
    llm_runner = llm_runner or functools.partial(
        claude_json_runner, model=config.session.speccer_model
    )
    client = client or CodegraphClient(repo_root=repo_root)

    outcome = compute_partition(
        plan_path=plan_path,
        repo_root=repo_root,
        config=config,
        llm_runner=llm_runner,
        client=client,
        allow_unknown_symbols=allow_unknown_symbols,
        recorder=recorder,
        llm_recorder=llm_recorder,
        provenance_recorder=provenance_recorder,
        progress=progress,
    )
    graph, partition, dag = outcome.graph, outcome.partition, outcome.dag
    mapper_out = outcome.mapper_out
    base_context, base_tokens = outcome.base_context, outcome.base_tokens

    members_by_gid: dict[int, list[str]] = {}
    for node, gid in partition.items():
        members_by_gid.setdefault(gid, []).append(node)

    _emit(progress, "stage: assemble")
    plan_sections = parse_plan_sections(outcome.plan_text)
    specs = assemble_group_specs(
        AssemblyInputs(
            plan_sections=plan_sections,
            graph=graph,
            partition=partition,
            dag=dag,
            members_by_gid=members_by_gid,
            descriptions=mapper_out.descriptions,
            group_label=group_label,
        )
    )

    upstream_of: dict[int, list[int]] = {gid: [] for gid in members_by_gid}
    for up_gid, downs in dag.items():
        for down_gid in downs:
            upstream_of[down_gid].append(up_gid)

    roles = outcome.hub_roles
    flags = list(mapper_out.flags) + list(outcome.flags) + [ASSEMBLED_FLAG]
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
        if isinstance(graph.provenance, EdgeProvenance):
            graph.provenance.record_affinity(
                pair,
                EdgeContribution(
                    kind=SIGNAL_PROSE_NEIGHBOR,
                    declared=False,
                    scaled_weight=weight,
                    detail={"reason": "region-less task attached to its plan-order neighbor"},
                ),
            )
    return TaskGraph(
        nodes=graph.nodes,
        affinity=affinity,
        dependencies=graph.dependencies,
        metadata=graph.metadata,
        provenance=graph.provenance,
    )


def _portable_path(plan_path: Path, repo_root: Path) -> str:
    try:
        return str(plan_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(plan_path)
