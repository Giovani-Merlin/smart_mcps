"""Versioned grouping trace (plan U8): the answer to "why is this node in this
group" without a debugging session.

``GroupingTrace`` is a plain pydantic record; ``TraceRecorder`` accumulates one
during a single ``compute_partition``/``run_grouping`` call. Every ``record_*``/
``set_*`` method only appends to ``self.trace`` — none of them read the trace
back into a decision, so attaching a recorder can never change the partition a
run produces (observation is inert). ``partition.py`` never imports this
module: it accepts a structurally-typed ``PartitionRecorder`` (see its
``Protocol``) so this module can depend on it instead, keeping the dependency
one-directional and ``partition.py``'s own import surface pure (see
``tests/test_partition.py::TestStrategySeam::test_module_imports_stay_pure``).

No field here carries a timestamp (R18): file mtime and the run snapshot
supply time, this model supplies content, and two runs against the same input
must serialize to identical bytes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TRACE_SCHEMA_VERSION = 1

# The closed set established by plan U4's merge guards — a rejected merge
# candidate's `reason` is always one of these; never free text.
MERGE_REJECTION_REASONS = (
    "over_budget",
    "not_chain_compatible",
    "makespan_regression",
    "would_create_cycle",
)

MergeReason = Literal[
    "over_budget", "not_chain_compatible", "makespan_regression", "would_create_cycle", ""
]


class GraphSnapshot(BaseModel):
    """The input ``TaskGraph``, flattened to JSON-stable lists."""

    nodes: list[str] = Field(default_factory=list)
    affinity: list[tuple[str, str, float]] = Field(default_factory=list)
    dependencies: list[tuple[str, str, float]] = Field(default_factory=list)


class NodeWorkEntry(BaseModel):
    """Per-node work (plan U8: "with its components")."""

    node: str
    source_bytes: int
    file_count: int
    bytes_tokens: float
    file_allowance_tokens: float
    total: float


class BudgetArithmetic(BaseModel):
    base_tokens: int
    spec_tokens_allowance: int
    slack_multiplier: float
    token_budget: int
    head: float
    budget_cap: float


class HubRoleEntry(BaseModel):
    """One node's hub-role decision: its degree ratios against the threshold
    that decided ``role`` (plan U8: "hub scores vs threshold")."""

    node: str
    role: Literal["utility_hub", "aggregator_hub", "core"]
    depends_on_ratio: float
    depended_by_ratio: float
    threshold: float


class SliceAtomEntry(BaseModel):
    label: str
    members: list[str] = Field(default_factory=list)


class StageSnapshot(BaseModel):
    """The full partition after one internal stage ran."""

    stage: str
    partition: dict[str, int] = Field(default_factory=dict)


class LouvainEntry(BaseModel):
    resolution: float
    seed: int
    communities: list[list[str]] = Field(default_factory=list)


class CutCandidate(BaseModel):
    """One block-to-block affinity edge ``split_over_budget`` weighed against
    the others before choosing where to cut (plan U8: "the compared
    alternatives")."""

    block_a: str
    block_b: str
    weight: float
    cut: bool


class SplitEntry(BaseModel):
    group_members: list[str] = Field(default_factory=list)
    total_work: float
    budget_cap: float
    candidates: list[CutCandidate] = Field(default_factory=list)
    resulting_components: list[list[str]] = Field(default_factory=list)


class MergeCandidateEntry(BaseModel):
    """One merge candidate ``merge_small_groups`` evaluated in one round.

    ``reason`` is only meaningful when ``accepted`` is ``False``, and is then
    always one of the closed set established by plan U4's guards
    (``over_budget``, ``not_chain_compatible``, ``makespan_regression``,
    ``would_create_cycle``) — never a free-text explanation.
    """

    round: int
    source: int
    target: int
    accepted: bool
    reason: MergeReason = ""
    merged_work: float
    edge_weight: float


class RepairEntry(BaseModel):
    """One cyclic group-SCC ``repair_cycles`` merged, with the task-level
    edges that evidence the cycle (plan U8: "evidence edges") and, when a
    re-split ran, the chunks it produced and any that stayed over budget."""

    cyclic_groups: list[int] = Field(default_factory=list)
    evidence_edges: list[tuple[str, str]] = Field(default_factory=list)
    merge_target: int
    resplit_chunks: list[list[str]] = Field(default_factory=list)
    overshoots: list[str] = Field(default_factory=list)


class GroupDifficultyEntry(BaseModel):
    """Per-group difficulty (R14): populated only on the full ``run_grouping``
    path, since ``verification_items`` needs the speccer's output."""

    group_id: str
    files_touched: int
    max_fan_in: int
    max_fan_out: int
    hub_touches: int
    cross_group_edges: int
    verification_items: int
    difficulty: float
    intensity: str
    d_review: float
    d_hard: float


class FailureEntry(BaseModel):
    """A grouping run that raised before producing a result — the trace still
    carries whatever was recorded up to that point plus this section."""

    kind: str
    message: str


class GroupingTrace(BaseModel):
    """Everything recorded for one ``group`` invocation.

    ``groups`` stays empty on the partition-only path (``--no-spec``,
    ``compute_partition``) and is filled by ``run_grouping``. ``failure`` is
    set only when the run raised before completing.
    """

    schema_version: int = TRACE_SCHEMA_VERSION
    input_graph: GraphSnapshot | None = None
    node_work: list[NodeWorkEntry] = Field(default_factory=list)
    budget: BudgetArithmetic | None = None
    config: dict = Field(default_factory=dict)
    hub_roles: list[HubRoleEntry] = Field(default_factory=list)
    slice_atoms: list[SliceAtomEntry] = Field(default_factory=list)
    stages: list[StageSnapshot] = Field(default_factory=list)
    louvain: list[LouvainEntry] = Field(default_factory=list)
    splits: list[SplitEntry] = Field(default_factory=list)
    merges: list[MergeCandidateEntry] = Field(default_factory=list)
    repairs: list[RepairEntry] = Field(default_factory=list)
    dag: dict[int, list[int]] = Field(default_factory=dict)
    last_stage: str | None = None
    groups: list[GroupDifficultyEntry] = Field(default_factory=list)
    mapper_flags: list[str] = Field(default_factory=list)
    partition_flags: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    failure: FailureEntry | None = None


def serialize_trace(trace: GroupingTrace) -> str:
    """Canonical grouping-trace.json bytes — mirrors ``serialize_grouping``."""
    return trace.model_dump_json(indent=2) + "\n"


class TraceRecorder:
    """Accumulates a ``GroupingTrace`` during one grouping run.

    Passed around as an optional, default-``None`` seam (plan U8/U9): every
    stage function that accepts one checks ``if recorder is not None`` before
    calling any ``record_*`` method, so ``recorder=None`` reproduces today's
    behaviour exactly.
    """

    def __init__(self) -> None:
        self.trace = GroupingTrace()

    # -------------------------------------------------------------- pipeline-level

    def set_config(self, config: dict) -> None:
        self.trace.config = config

    def set_input_graph(self, nodes, affinity, dependencies) -> None:
        self.trace.input_graph = GraphSnapshot(
            nodes=sorted(nodes),
            affinity=[(a, b, w) for (a, b), w in sorted(affinity.items())],
            dependencies=[(a, b, w) for (a, b), w in sorted(dependencies.items())],
        )

    def set_node_work(self, entries: list[NodeWorkEntry]) -> None:
        self.trace.node_work = sorted(entries, key=lambda e: e.node)

    def set_budget(self, budget: BudgetArithmetic) -> None:
        self.trace.budget = budget

    def record_slice_atoms(self, atoms: dict) -> None:
        self.trace.slice_atoms = [
            SliceAtomEntry(label=label, members=list(members))
            for label, members in sorted(atoms.items())
        ]

    def set_dag(self, dag: dict) -> None:
        self.trace.dag = {gid: sorted(downs) for gid, downs in sorted(dag.items())}

    def set_last_stage(self, last_stage: str | None) -> None:
        self.trace.last_stage = last_stage

    def set_flags(self, mapper_flags, partition_flags) -> None:
        self.trace.mapper_flags = list(mapper_flags)
        self.trace.partition_flags = list(partition_flags)
        self.trace.flags = list(mapper_flags) + list(partition_flags)

    def set_final_flags(self, flags) -> None:
        self.trace.flags = list(flags)

    def record_group_difficulty(self, entry: GroupDifficultyEntry) -> None:
        self.trace.groups.append(entry)

    def record_failure(self, exc: BaseException) -> None:
        self.trace.failure = FailureEntry(kind=type(exc).__name__, message=str(exc))

    # -------------------------------------------------------------- partition-level
    # (structurally satisfy partition.py's PartitionRecorder protocol)

    def record_stage(self, stage: str, partition: dict) -> None:
        self.trace.stages.append(
            StageSnapshot(stage=stage, partition=dict(sorted(partition.items())))
        )

    def record_hub_role(
        self,
        node: str,
        role: str,
        depends_on_ratio: float,
        depended_by_ratio: float,
        threshold: float,
    ) -> None:
        self.trace.hub_roles.append(
            HubRoleEntry(
                node=node,
                role=role,
                depends_on_ratio=depends_on_ratio,
                depended_by_ratio=depended_by_ratio,
                threshold=threshold,
            )
        )

    def record_louvain(self, resolution: float, seed: int, communities: list[list[str]]) -> None:
        self.trace.louvain.append(
            LouvainEntry(
                resolution=resolution, seed=seed, communities=[list(c) for c in communities]
            )
        )

    def record_split(
        self,
        members: list[str],
        total_work: float,
        budget_cap: float,
        candidates: list[dict],
        components: list[list[str]],
    ) -> None:
        self.trace.splits.append(
            SplitEntry(
                group_members=list(members),
                total_work=total_work,
                budget_cap=budget_cap,
                candidates=[CutCandidate(**c) for c in candidates],
                resulting_components=[list(c) for c in components],
            )
        )

    def record_merge_candidate(
        self,
        round_: int,
        source: int,
        target: int,
        accepted: bool,
        reason: str,
        merged_work: float,
        edge_weight: float,
    ) -> None:
        self.trace.merges.append(
            MergeCandidateEntry(
                round=round_,
                source=source,
                target=target,
                accepted=accepted,
                reason=reason,  # type: ignore[arg-type]
                merged_work=merged_work,
                edge_weight=edge_weight,
            )
        )

    def record_repair(
        self,
        cyclic_groups: list[int],
        evidence_edges: list[tuple[str, str]],
        merge_target: int,
        resplit_chunks: list[list[str]],
        overshoots: list[str],
    ) -> None:
        self.trace.repairs.append(
            RepairEntry(
                cyclic_groups=list(cyclic_groups),
                evidence_edges=list(evidence_edges),
                merge_target=merge_target,
                resplit_chunks=[list(c) for c in resplit_chunks],
                overshoots=list(overshoots),
            )
        )
