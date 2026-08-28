"""Partition core: ported CoCoder policies behind a swappable strategy interface.

The graph shapes here are the contract between the codegraph adapter (graphing.py),
the strategies in this module, and the estimator hooks injected into them:

- ``TaskGraph.affinity`` — symmetric clustering weight per unordered task pair
  (shared files, call proximity, impact overlap combined by the adapter).
- ``TaskGraph.dependencies`` — directed edges ``(upstream, downstream)``: the
  downstream task builds on the upstream task's output. Direction-sensitive
  policies (hub roles, sibling lifting, merge direction, makespan, the group DAG)
  read only these.

Ported from CoCoder (Apache-2.0, https://github.com/Flitternie/CoCoder) — see
docs/research/cocoder-analysis.md §3/§7 and docs/research/design-deviations.md for
what changed: corrected hub role names (CoCoder's ``detect_roles`` labels are
inverted vs its docstrings; behavior is ported, not names), Louvain instead of
InfoMap, always-on size-bounded merging, explicit cycle detection, and a
lowest-affinity split for over-budget groups.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

Pair = tuple[str, str]
# node → group id. Group ids are contiguous ints; deterministic across runs.
Partition = dict[str, int]
# Injected estimator hook: relative work for one task node (tokens, symbols — any
# consistent unit). Never imported from the estimator module: keeps this module pure.
WorkFn = Callable[[str], float]

# Plan U4: the granularity dial relaxes merge_small_groups' two guards, in order.
# `independent` (default) enforces both and reproduces today's behaviour byte-for-
# byte; `balanced` drops the makespan no-regression check; `monolithic` also drops
# chain_compatible. The budget cap, slice must-link and cycle checks stay hard at
# every level (docs/orchestrator-grouping.md §"Prior art and known limits of the dial").
Granularity = Literal["independent", "balanced", "monolithic"]
GRANULARITY_LEVELS: tuple[Granularity, ...] = ("independent", "balanced", "monolithic")

DEFAULT_HUB_THRESHOLD = 0.4  # CoCoder's live ROLE_THRESHOLD (partition_into_groups.py:37)
LOUVAIN_SEED = 42
# Plan U12 (R19b): mirrors PartitionConfig.target_fill_ratio's default — the two
# are kept in sync deliberately (config.py carries the justification) since this
# module must not import config.py (kept pure, see module docstring) and tests
# call merge_small_groups/DefaultPartitionStrategy directly without a config object.
DEFAULT_TARGET_FILL_RATIO = 0.75

_Node = TypeVar("_Node")


def canonical_pair(a: str, b: str) -> Pair:
    """Unordered pair key for affinity maps."""
    return (a, b) if a <= b else (b, a)


@dataclass(frozen=True)
class TaskGraph:
    """Weighted task graph: symmetric affinity for clustering, directed dependencies."""

    nodes: frozenset[str]
    affinity: Mapping[Pair, float] = field(default_factory=dict)
    dependencies: Mapping[Pair, float] = field(default_factory=dict)
    metadata: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    # Observational edge provenance (graphing.EdgeProvenance) — deliberately typed as
    # ``object`` so this module gains no import and stays stdlib-pure. Nothing in the
    # partitioner reads it; it exists to be serialized into the edge-provenance.json
    # sidecar after the partition is computed.
    provenance: object | None = None

    def __post_init__(self) -> None:
        for u, v in self.affinity:
            if (u, v) != canonical_pair(u, v):
                raise ValueError(f"affinity key not canonical: {(u, v)}")
        for kind, pairs in (("affinity", self.affinity), ("dependency", self.dependencies)):
            for u, v in pairs:
                if u == v:
                    raise ValueError(f"self-loop {kind} edge on {u!r}")
                missing = {u, v} - self.nodes
                if missing:
                    raise ValueError(f"{kind} edge {(u, v)} references unknown nodes {missing}")

    def assert_acyclic_dependencies(self) -> None:
        """Task-level precedence must be a DAG — the builder-output contract.

        Deliberately **not** in ``__post_init__``: slice contraction legitimately
        creates cycles that do not exist at task level (``_contract_slices``
        merging a1+a2 and b1+b2 turns an acyclic a1->b1, b2->a2 into s1<->s2),
        and that is exactly what ``repair_cycles`` is for. The contract is on what
        a *builder* emits, so this is called at the end of ``build_task_graph``
        and again in ``compute_partition`` — never on internal intermediates.

        Nothing used to check this at all. The only acyclicity check was on the
        *output* group DAG, which ``repair_cycles`` can always satisfy by
        collapsing every task into one group — so a saturated dependency graph
        produced a legal, useless, single-group "success" instead of an error
        (docs/orchestrator-grouping.md, limitations 4-5). Builders are responsible
        for withdrawing inferred precedence until this holds
        (``graphing._drop_inferred_cycles``).
        """
        adjacency: dict[str, set[str]] = {}
        for up, down in self.dependencies:
            adjacency.setdefault(up, set()).add(down)
        cyclic = [
            c for c in _strongly_connected_components(adjacency, set(self.nodes)) if len(c) > 1
        ]
        if cyclic:
            component = cyclic[0]
            members = set(component)
            edges = sorted((u, v) for u, v in self.dependencies if u in members and v in members)
            shown = ", ".join(f"{u} -> {v}" for u, v in edges[:8])
            more = f" (+{len(edges) - 8} more)" if len(edges) > 8 else ""
            raise ValueError(
                f"dependency cycle among tasks {sorted(component)}: {shown}{more} — "
                "task precedence must be a DAG"
            )


class GroupCycleError(Exception):
    """The computed group DAG contains a dependency cycle."""

    def __init__(self, cycle_groups: list[int], offending_edges: list[Pair]):
        self.cycle_groups = cycle_groups
        self.offending_edges = offending_edges
        edges = ", ".join(f"{u} -> {v}" for u, v in offending_edges)
        super().__init__(
            f"dependency cycle across groups {cycle_groups}; offending task edges: {edges}"
        )


class PartitionStrategy(Protocol):
    """Strategy interface (R22): swap the partitioner without touching the pipeline."""

    def partition(self, graph: TaskGraph) -> Partition: ...


class PartitionRecorder(Protocol):
    """Structural type for the optional trace hook (plan U8).

    This module never imports ``orchestrator.grouping.trace`` — a caller
    passes anything satisfying this shape (duck typing; no inheritance
    required), which keeps the dependency one-directional and this module's
    import surface pure (``TestStrategySeam::test_module_imports_stay_pure``).
    Every stage function below accepts ``recorder: PartitionRecorder | None =
    None`` and only ever calls these methods — never reads them back into a
    decision, so attaching a recorder cannot change the partition produced.
    """

    def record_stage(self, stage: str, partition: Partition) -> None: ...
    def record_hub_role(
        self,
        node: str,
        role: str,
        depends_on_ratio: float,
        depended_by_ratio: float,
        threshold: float,
    ) -> None: ...
    def record_louvain(
        self, resolution: float, seed: int, communities: list[list[str]]
    ) -> None: ...
    def record_split(
        self,
        members: list[str],
        total_work: float,
        budget_cap: float,
        candidates: list[dict],
        components: list[list[str]],
    ) -> None: ...
    def record_merge_candidate(
        self,
        round_: int,
        source: int,
        target: int,
        accepted: bool,
        reason: str,
        merged_work: float,
        edge_weight: float,
    ) -> None: ...
    def record_repair(
        self,
        cyclic_groups: list[int],
        evidence_edges: list[Pair],
        merge_target: int,
        resplit_chunks: list[list[str]],
        overshoots: list[str],
    ) -> None: ...


class SingleGroupStrategy:
    """Trivial passthrough: every task in one group. Proves the R22 seam."""

    def partition(self, graph: TaskGraph) -> Partition:
        return {node: 0 for node in sorted(graph.nodes)}


def _group_edges(graph: TaskGraph, partition: Partition) -> dict[int, set[int]]:
    """Group-level dependency edges {upstream_gid: {downstream_gid}}, unfiltered —
    may contain cycles. Shared by build_group_dag's authoritative check, the U4
    merge guard, and the U5 SCC repair."""
    group_edges: dict[int, set[int]] = defaultdict(set)
    for up, down in graph.dependencies:
        gu, gd = partition[up], partition[down]
        if gu != gd:
            group_edges[gu].add(gd)
    return group_edges


def _is_acyclic(group_edges: Mapping[int, set[int]], gids: set[int]) -> bool:
    """Kahn's algorithm over a group-level edge map; the surviving-node count
    is order-independent, so this needs no sorting to be deterministic."""
    indegree: dict[int, int] = dict.fromkeys(gids, 0)
    for gu, downs in group_edges.items():
        for gd in downs:
            if gd in indegree:
                indegree[gd] += 1
    queue = [gid for gid, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        gid = queue.pop()
        seen += 1
        for gd in group_edges.get(gid, ()):
            if gd in indegree:
                indegree[gd] -= 1
                if indegree[gd] == 0:
                    queue.append(gd)
    return seen == len(indegree)


def build_group_dag(graph: TaskGraph, partition: Partition) -> dict[int, set[int]]:
    """Group-level dependency edges {upstream_gid: {downstream_gid}}; cycles fail loudly.

    CoCoder has no cycle detection (a cyclic graph silently wedges its scheduler);
    here a cycle raises GroupCycleError naming the groups and the task edges that
    close the cycle. By the time DefaultPartitionStrategy reaches this call
    (plan U4/U5), the merge guard has refused every cycle-creating merge and the
    SCC repair has folded away whatever still cycled after Louvain/lift/split —
    a GroupCycleError surfacing from here is an orchestrator bug, not a
    user-facing outcome.
    """
    group_edges: dict[int, set[int]] = defaultdict(set)
    edge_evidence: dict[tuple[int, int], list[Pair]] = defaultdict(list)
    for (up, down), _w in sorted(graph.dependencies.items()):
        gu, gd = partition[up], partition[down]
        if gu != gd:
            group_edges[gu].add(gd)
            edge_evidence[(gu, gd)].append((up, down))

    # Kahn's algorithm; whatever survives is part of (or downstream of) a cycle.
    indegree: dict[int, int] = {gid: 0 for gid in set(partition.values())}
    for gu, downs in group_edges.items():
        for gd in downs:
            indegree[gd] += 1
    queue = sorted(gid for gid, d in indegree.items() if d == 0)
    seen = 0
    while queue:
        gid = queue.pop(0)
        seen += 1
        for gd in sorted(group_edges.get(gid, ())):
            indegree[gd] -= 1
            if indegree[gd] == 0:
                queue.append(gd)
        queue.sort()
    if seen != len(indegree):
        cyclic = sorted(gid for gid, d in indegree.items() if d > 0)
        offending = [
            e
            for (gu, gd), evs in sorted(edge_evidence.items())
            for e in evs
            if gu in cyclic and gd in cyclic
        ]
        raise GroupCycleError(cyclic, offending)
    return {gid: set(downs) for gid, downs in group_edges.items()}


def _renumber(partition: Partition) -> Partition:
    """Deterministic contiguous group ids, ordered by each group's smallest node."""
    groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in partition.items():
        groups[gid].append(node)
    ordered = sorted(groups.values(), key=lambda members: min(members))
    return {node: new_gid for new_gid, members in enumerate(ordered) for node in members}


def _membership(partition: Partition) -> frozenset[frozenset[str]]:
    """Group membership independent of gid numbering — the stage-diffing key."""
    groups: dict[int, set[str]] = defaultdict(set)
    for node, gid in partition.items():
        groups[gid].add(node)
    return frozenset(frozenset(members) for members in groups.values())


def _last_modifying_stage(stages: list[tuple[str, Partition]]) -> str:
    """The last stage whose membership differs from the stage before it.

    The first stage in the list is always the baseline (it always "changed"
    from no partition at all), so it is the default when nothing after it
    changes anything.
    """
    last = stages[0][0]
    previous = _membership(stages[0][1])
    for label, snapshot in stages[1:]:
        membership = _membership(snapshot)
        if membership != previous:
            last = label
        previous = membership
    return last


@dataclass
class DefaultPartitionStrategy:
    """CoCoder's pipeline, ported: hub isolation → slice contraction → Louvain →
    lift → split → merge → repair.

    ``work_fn`` and ``budget_cap`` are the injected estimator hooks: work is the
    relative size of one task node and the cap bounds a group's summed work. With
    the defaults (work 1.0, no cap) size policies degrade to CoCoder's file-count
    behavior with an unlimited group size.

    Slice labels (task-map must-links, docs/orchestrator-task-map.md) enter as
    deterministic node contraction: hub roles are detected first (a hub is never
    absorbed into a slice), each slice's core members contract into one supernode
    for clustering and lifting, then membership expands. A slice is an indivisible
    block from here on: ``split_over_budget`` (plan U3) cuts around a slice, never
    through it, and ``merge_small_groups`` (plan U4) refuses any merge that would
    close a group-level dependency cycle.

    Prevention is not exhaustive — a cycle can still originate at Louvain, lift or
    split, before merge ever runs (plan U5). ``repair_cycles`` merges every such
    surviving cyclic group-SCC and re-splits it back inside budget where possible;
    acyclicity is an internal invariant from this point on, so the final
    ``build_group_dag`` call below is a safety net whose ``GroupCycleError`` would
    mean a bug in this repair, not a user-facing outcome.

    ``last_stage`` (R18) records which internal stage last *changed* the
    partition's membership (contraction/louvain, lift, split, merge, repair) — set
    after every ``partition()`` call by comparing group membership after each
    stage, not by re-running anything. ``flags`` (R10) accumulates one message per
    repaired group that could not be re-split back under budget.
    """

    work_fn: WorkFn = lambda node: 1.0
    budget_cap: float | None = None
    hub_threshold: float = DEFAULT_HUB_THRESHOLD
    louvain_resolution: float = 1.0
    granularity: Granularity = "independent"
    target_fill_ratio: float = DEFAULT_TARGET_FILL_RATIO
    recorder: PartitionRecorder | None = None
    last_stage: str | None = field(default=None, init=False)
    flags: list[str] = field(default_factory=list, init=False)
    # Structured counterpart to the overshoot strings in ``flags`` (plan U9):
    # each repaired group that stayed over budget also lands here with its
    # offending task-level edges attached, so a caller can classify them
    # (declared vs. inferred) without re-deriving the SCC.
    degenerate_repairs: list[DegenerateRepair] = field(default_factory=list, init=False)

    def _record_stage(self, name: str, partition: Partition) -> None:
        if self.recorder is not None:
            self.recorder.record_stage(name, partition)

    def partition(self, graph: TaskGraph) -> Partition:
        self.flags = []
        self.degenerate_repairs = []
        if not graph.nodes:
            self.last_stage = None
            return {}
        roles = detect_hub_roles(graph, threshold=self.hub_threshold, recorder=self.recorder)
        atoms = slice_atoms(graph, roles)
        stages: list[tuple[str, Partition]] = []
        if atoms:
            unit_graph, self_loops, unit_of = _contract_slices(graph, atoms)
            unit_roles = {unit_of[node]: role for node, role in roles.items()}
            unit_partition = _hub_isolated_clustering(
                unit_graph, unit_roles, self.louvain_resolution, self_loops, recorder=self.recorder
            )
            partition = {node: unit_partition[unit_of[node]] for node in graph.nodes}
            stages.append(("contraction", dict(partition)))
            self._record_stage("contraction", partition)
            unit_partition = lift_independent(unit_graph, unit_partition)
            partition = {node: unit_partition[unit_of[node]] for node in graph.nodes}
            stages.append(("lift", dict(partition)))
            self._record_stage("lift", partition)
        else:
            partition = _hub_isolated_clustering(
                graph, roles, self.louvain_resolution, recorder=self.recorder
            )
            stages.append(("louvain", dict(partition)))
            self._record_stage("louvain", partition)
            partition = lift_independent(graph, partition)
            stages.append(("lift", dict(partition)))
            self._record_stage("lift", partition)
        if self.budget_cap is not None:
            partition = split_over_budget(
                graph, partition, self.work_fn, self.budget_cap, recorder=self.recorder
            )
            stages.append(("split", dict(partition)))
            self._record_stage("split", partition)
        partition = merge_small_groups(
            graph,
            partition,
            self.work_fn,
            self.budget_cap,
            recorder=self.recorder,
            granularity=self.granularity,
            target_fill_ratio=self.target_fill_ratio,
        )
        stages.append(("merge", dict(partition)))
        self._record_stage("merge", partition)
        partition = repair_cycles(
            graph,
            partition,
            self.work_fn,
            self.budget_cap,
            self.flags,
            recorder=self.recorder,
            degenerate=self.degenerate_repairs,
        )
        stages.append(("repair", dict(partition)))
        self._record_stage("repair", partition)
        partition = _renumber(partition)
        self._record_stage("renumber", partition)
        build_group_dag(graph, partition)  # an orchestrator bug if this still raises (plan U5)
        self.last_stage = _last_modifying_stage(stages)
        return partition


def detect_hub_roles(
    graph: TaskGraph,
    threshold: float = DEFAULT_HUB_THRESHOLD,
    recorder: PartitionRecorder | None = None,
) -> dict[str, str]:
    """Degree-thresholded roles: utility_hub / aggregator_hub / core.

    Port of CoCoder ``detect_roles`` (common.py:43-63) by *behavior*: a node most
    others depend on is a ``utility_hub`` (CoCoder's misnamed ``in_hub``) and is
    isolated as its own group; a node depending on most others is an
    ``aggregator_hub`` (CoCoder's misnamed ``out_hub``) and lands in one trailing
    shared group. CoCoder classifies the aggregator check first — kept.
    """
    n = len(graph.nodes)
    if n <= 1:
        roles = {node: "core" for node in graph.nodes}
        if recorder is not None:
            for node in sorted(roles):
                recorder.record_hub_role(node, "core", 0.0, 0.0, threshold)
        return roles
    dependencies_of: dict[str, set[str]] = defaultdict(set)
    dependents_of: dict[str, set[str]] = defaultdict(set)
    for up, down in graph.dependencies:
        dependencies_of[down].add(up)
        dependents_of[up].add(down)
    roles = {}
    # Sorted, not `graph.nodes` (a frozenset — hash-seed order): this loop is
    # the only place `record_hub_role` fires, and unsorted iteration would
    # make the trace's `hub_roles` list order vary across runs (R18).
    for node in sorted(graph.nodes):
        depends_on = len(dependencies_of[node]) / (n - 1)
        depended_by = len(dependents_of[node]) / (n - 1)
        if depends_on > threshold:
            role = "aggregator_hub"
        elif depended_by > threshold:
            role = "utility_hub"
        else:
            role = "core"
        roles[node] = role
        if recorder is not None:
            recorder.record_hub_role(node, role, depends_on, depended_by, threshold)
    return roles


def slice_atoms(graph: TaskGraph, roles: dict[str, str]) -> dict[str, list[str]]:
    """Slice label → sorted members with 2+ tasks (the real must-links).

    Reads the ``slice`` node metadata the task map supplies. A declared slice
    outranks an inferred hub role: every member joins its atom regardless of
    ``roles``, because the planner bound those tasks explicitly and hub
    classification is only a degree-ratio inference (``detect_hub_roles``).
    ``roles`` is accepted for signature stability and trace context, not to
    filter membership. Hub isolation still applies to every task carrying no
    slice label. Public: the R18 partition-only report surfaces these.
    """
    atoms: dict[str, list[str]] = defaultdict(list)
    for node in sorted(graph.nodes):
        label = graph.metadata.get(node, {}).get("slice")
        if isinstance(label, str) and label:
            atoms[label].append(node)
    return {label: members for label, members in sorted(atoms.items()) if len(members) >= 2}


@dataclass(frozen=True)
class SliceReentryPath:
    """A dependency path that leaves a declared slice and returns to it (plan
    U9/C5): once the slice contracts to one supernode for Louvain, this is
    exactly what closes a cycle that does not exist at task level, and it used
    to surface only as a generic degenerate-partition saturation message with
    no actionable edit. ``path`` runs slice member to slice member, in order,
    naming every node in between.
    """

    slice: str
    path: tuple[str, ...]


def _find_path_leaving_and_returning(
    start: str, member_set: set[str], successors: Mapping[str, list[str]]
) -> tuple[str, ...] | None:
    """Shortest dependency path from ``start`` (a slice member), through nodes
    outside the slice, back to another slice member — sorted BFS, so the
    reported path is stable across runs."""
    queue: deque[tuple[str, ...]] = deque([(start,)])
    visited = {start}
    while queue:
        path = queue.popleft()
        current = path[-1]
        for nxt in sorted(successors.get(current, ())):
            if nxt in member_set:
                if nxt != start and len(path) > 1:
                    return (*path, nxt)
                continue
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append((*path, nxt))
    return None


def find_slice_reentrant_paths(
    graph: TaskGraph, atoms: dict[str, list[str]]
) -> list[SliceReentryPath]:
    """Detect, on the contracted graph, every slice a dependency path leaves
    and re-enters (plan U9/C5) — the shape ``_contract_slices`` turns into a
    cycle that does not exist at task level. The full task graph is already
    required to be acyclic (``TaskGraph.assert_acyclic_dependencies``), so any
    cycle introduced by contraction must pass through at least one slice
    supernode; every slice whose supernode sits in such a cycle is reported
    here (R5 discipline: the caller collects and raises all of them together),
    each with one concrete path reconstructed from the original graph.
    """
    if not atoms:
        return []
    unit_graph, _self_loops, _unit_of = _contract_slices(graph, atoms)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for up, down in unit_graph.dependencies:
        adjacency[up].add(down)
    sccs = _strongly_connected_components(adjacency, set(unit_graph.nodes))
    cyclic_units = {unit for scc in sccs if len(scc) > 1 for unit in scc}

    successors: dict[str, list[str]] = defaultdict(list)
    for up, down in graph.dependencies:
        successors[up].append(down)

    results: list[SliceReentryPath] = []
    for label, members in sorted(atoms.items()):
        if f"slice::{label}" not in cyclic_units:
            continue
        member_set = set(members)
        for start in sorted(members):
            path = _find_path_leaving_and_returning(start, member_set, successors)
            if path is not None:
                results.append(SliceReentryPath(slice=label, path=path))
                break
    return results


def _contract_slices(
    graph: TaskGraph, atoms: dict[str, list[str]]
) -> tuple[TaskGraph, dict[str, float], dict[str, str]]:
    """Contract each slice into a ``slice::<label>`` supernode (Rey et al. 2022:
    contracted-graph modularity ≡ constraint-respecting original modularity).

    Sorted iteration for byte-stability; parallel edge weights summed. Intra-slice
    affinity becomes self-loop weight returned separately — ``TaskGraph`` forbids
    self-loops, but Louvain must still see the supernode's internal mass. Intra-
    slice dependency edges vanish (ordering inside one group is the worker's job).
    Returns ``(unit graph, self-loop weights, node → unit mapping)``.
    """
    unit_of = {node: node for node in graph.nodes}
    for label, members in sorted(atoms.items()):
        for node in members:
            unit_of[node] = f"slice::{label}"

    affinity: dict[Pair, float] = {}
    self_loops: dict[str, float] = {}
    for (a, b), w in sorted(graph.affinity.items()):
        ua, ub = unit_of[a], unit_of[b]
        if ua == ub:
            self_loops[ua] = self_loops.get(ua, 0.0) + w
            continue
        pair = canonical_pair(ua, ub)
        affinity[pair] = affinity.get(pair, 0.0) + w
    dependencies: dict[Pair, float] = {}
    for (up, down), w in sorted(graph.dependencies.items()):
        uu, ud = unit_of[up], unit_of[down]
        if uu == ud:
            continue
        dependencies[(uu, ud)] = dependencies.get((uu, ud), 0.0) + w

    unit_graph = TaskGraph(
        nodes=frozenset(unit_of.values()), affinity=affinity, dependencies=dependencies
    )
    return unit_graph, self_loops, unit_of


def _louvain(
    graph: TaskGraph,
    nodes: set[str],
    resolution: float,
    self_loops: Mapping[str, float] | None = None,
    recorder: PartitionRecorder | None = None,
) -> Partition:
    """Seeded directed Louvain over the affinity weights restricted to ``nodes``.

    Dependency direction is preserved on edges that have one (CoCoder kept import
    direction for its clustering); pure-affinity pairs get both directions.
    ``self_loops`` carries contracted supernodes' internal mass. networkx shuffles
    node order by default — the pinned seed plus deterministic community numbering
    keep the result stable across runs.
    """
    import networkx as nx

    g = nx.DiGraph()
    g.add_nodes_from(sorted(nodes))
    for (a, b), w in sorted(graph.affinity.items()):
        if w <= 0 or a not in nodes or b not in nodes:
            continue
        forward = (a, b) in graph.dependencies
        backward = (b, a) in graph.dependencies
        if forward or not backward:
            g.add_edge(a, b, weight=w)
        if backward or not forward:
            g.add_edge(b, a, weight=w)
    for node, w in sorted((self_loops or {}).items()):
        if node in nodes and w > 0:
            g.add_edge(node, node, weight=w)
    if g.number_of_edges() == 0:
        return {node: i for i, node in enumerate(sorted(nodes))}
    communities = nx.community.louvain_communities(
        g, weight="weight", resolution=resolution, seed=LOUVAIN_SEED
    )
    ordered = sorted((sorted(c) for c in communities), key=lambda c: c[0])
    if recorder is not None:
        recorder.record_louvain(resolution, LOUVAIN_SEED, ordered)
    return {node: gid for gid, members in enumerate(ordered) for node in members}


def _hub_isolated_clustering(
    graph: TaskGraph,
    roles: dict[str, str],
    resolution: float,
    self_loops: Mapping[str, float] | None = None,
    recorder: PartitionRecorder | None = None,
) -> Partition:
    """CoCoder ``role_grouping`` (post_processing.py:13-33): cluster only the core.

    Utility hubs come first as singleton groups, Louvain communities next,
    aggregator hubs last in one shared group.
    """
    utility_hubs = sorted(n for n, r in roles.items() if r == "utility_hub")
    aggregator_hubs = sorted(n for n, r in roles.items() if r == "aggregator_hub")
    core = {n for n, r in roles.items() if r == "core"}
    core_partition = _louvain(graph, core, resolution, self_loops, recorder=recorder)

    partition: Partition = {}
    gid = 0
    for node in utility_hubs:
        partition[node] = gid
        gid += 1
    communities: dict[int, list[str]] = defaultdict(list)
    for node, cid in core_partition.items():
        communities[cid].append(node)
    for members in sorted(communities.values(), key=lambda m: min(m)):
        for node in members:
            partition[node] = gid
        gid += 1
    for node in aggregator_hubs:
        partition[node] = gid
    return partition


def lift_independent(graph: TaskGraph, partition: Partition) -> Partition:
    """CoCoder ``lift_independent`` (post_processing.py:36-105), on task dependencies.

    Within each group: siblings that depend only on the group's internal hubs
    (nodes with >=2 internal dependents) and have no internal dependents split off
    as their own groups; hub-less groups split by weakly connected components.
    Groups of <=2 nodes pass through unchanged.
    """
    dependencies_of: dict[str, set[str]] = defaultdict(set)
    for up, down in graph.dependencies:
        dependencies_of[down].add(up)

    groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in partition.items():
        groups[gid].append(node)

    new_partition: Partition = {}
    next_gid = 0
    for _gid, members in sorted(groups.items()):
        if len(members) <= 2:
            for node in members:
                new_partition[node] = next_gid
            next_gid += 1
            continue

        member_set = set(members)
        internal_deps: dict[str, set[str]] = defaultdict(set)
        internal_dependents: dict[str, set[str]] = defaultdict(set)
        for node in members:
            for dep in dependencies_of.get(node, set()):
                if dep in member_set:
                    internal_deps[node].add(dep)
                    internal_dependents[dep].add(node)
        internal_hubs = {n for n in members if len(internal_dependents.get(n, set())) >= 2}

        if not internal_hubs:
            adjacency: dict[str, set[str]] = defaultdict(set)
            for node in members:
                for dep in internal_deps.get(node, set()):
                    adjacency[node].add(dep)
                    adjacency[dep].add(node)
            for component in _connected_components(member_set, adjacency):
                for member in component:
                    new_partition[member] = next_gid
                next_gid += 1
        else:
            siblings, chain = [], set()
            for node in members:
                if node in internal_hubs:
                    chain.add(node)
                    continue
                deps = internal_deps.get(node, set())
                dependents = internal_dependents.get(node, set())
                if deps and deps.issubset(internal_hubs) and not dependents:
                    siblings.append(node)
                else:
                    chain.add(node)
            if chain:
                for node in sorted(chain):
                    new_partition[node] = next_gid
                next_gid += 1
            for node in sorted(siblings):
                new_partition[node] = next_gid
                next_gid += 1
    return new_partition


def _slice_block_of(graph: TaskGraph) -> dict[str, str]:
    """Node → indivisible block id: a declared slice's members share one id
    (``slice::<label>``), every other node is its own block. The splitter and
    the SCC re-split (plan U3/U5) both cut between blocks, never inside one."""
    block_of: dict[str, str] = {}
    for label, members in slice_atoms(graph, {}).items():
        for node in members:
            block_of[node] = f"slice::{label}"
    for node in graph.nodes:
        block_of.setdefault(node, node)
    return block_of


def _blocks_of(members: list[str], block_of: dict[str, str]) -> dict[str, list[str]]:
    """Group ``members`` by block id, each list sorted for determinism."""
    blocks: dict[str, list[str]] = defaultdict(list)
    for node in members:
        blocks[block_of[node]].append(node)
    return {block_id: sorted(nodes) for block_id, nodes in blocks.items()}


def split_over_budget(
    graph: TaskGraph,
    partition: Partition,
    work_fn: WorkFn,
    budget_cap: float,
    recorder: PartitionRecorder | None = None,
) -> Partition:
    """Split any group whose summed work exceeds the cap at its lowest-affinity boundary.

    Not in CoCoder (its clustering output is size-unbounded upward); required by
    origin AE2. Cut candidates are computed *between indivisible blocks* — a
    declared slice's members or a lone node (plan U3) — so a cut can separate a
    slice from the rest of an over-budget group but never break the slice apart.
    Reverse-Kruskal: drop the weakest internal block-to-block affinity until the
    group falls apart, recurse on any component still over budget. A group made
    of a single block over budget stays whole — the estimator flags it downstream
    (or, for a slice, U6's overflow gate).
    """
    block_of = _slice_block_of(graph)
    groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in partition.items():
        groups[gid].append(node)

    new_partition: Partition = {}
    next_gid = 0
    pending = [sorted(m) for _gid, m in sorted(groups.items())]
    while pending:
        members = pending.pop(0)
        total = sum(work_fn(n) for n in members)
        blocks = _blocks_of(members, block_of)
        if total <= budget_cap or len(blocks) == 1:
            for node in members:
                new_partition[node] = next_gid
            next_gid += 1
            continue
        member_set = set(members)
        block_affinity: dict[Pair, float] = defaultdict(float)
        for (a, b), w in graph.affinity.items():
            if a in member_set and b in member_set:
                ba, bb = block_of[a], block_of[b]
                if ba != bb:
                    block_affinity[canonical_pair(ba, bb)] += w
        block_ids = set(blocks)
        internal = sorted(
            ((w, pair) for pair, w in block_affinity.items()),
            key=lambda item: (item[0], item[1]),
        )
        components = _components_after_cut(block_ids, internal)
        if len(components) == 1:
            # Fully cohesive at every weight level: cut the single weakest edge set
            # couldn't separate it, so peel the smallest-work block deterministically.
            block_work = {b: sum(work_fn(n) for n in blocks[b]) for b in block_ids}
            peel = min(block_ids, key=lambda b: (block_work[b], b))
            components = [[peel], sorted(block_ids - {peel})]
        if recorder is not None:
            component_of = {b: i for i, comp in enumerate(components) for b in comp}
            candidates = [
                {
                    "block_a": pair[0],
                    "block_b": pair[1],
                    "weight": w,
                    "cut": component_of[pair[0]] != component_of[pair[1]],
                }
                for w, pair in internal
            ]
            recorder.record_split(members, total, budget_cap, candidates, components)
        pending = [
            sorted(node for block_id in component for node in blocks[block_id])
            for component in components
        ] + pending
    return new_partition


def _connected_components(nodes: set[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Deterministic undirected components: sorted seeds, sorted neighbors, sorted output."""
    components: list[list[str]] = []
    visited: set[str] = set()
    for node in sorted(nodes):
        if node in visited:
            continue
        component = []
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            queue.extend(nb for nb in sorted(adjacency.get(current, ())) if nb not in visited)
        components.append(sorted(component))
    return components


def _components_after_cut(
    nodes: set[str], weighted_edges: list[tuple[float, Pair]]
) -> list[list[str]]:
    """Drop edges weakest-first until the node set splits; return the components."""
    for cut_index in range(len(weighted_edges) + 1):
        adjacency: dict[str, set[str]] = defaultdict(set)
        for _w, (a, b) in weighted_edges[cut_index:]:
            adjacency[a].add(b)
            adjacency[b].add(a)
        components = _connected_components(nodes, adjacency)
        if len(components) > 1:
            return components
    return [sorted(nodes)]


def _simulate_makespan(graph: TaskGraph, partition: Partition, work: Mapping[str, float]) -> float:
    """CoCoder ``_simulate_zero_comm_makespan`` (post_processing.py:146-187).

    Greedy topological schedule where each group is a serial worker and
    cross-group dependencies gate start times; no communication penalty. Cyclic
    leftovers are appended deterministically instead of failing (cycles are
    rejected loudly later, at group-DAG build).
    """
    successors: dict[str, list[str]] = defaultdict(list)
    predecessors: dict[str, list[str]] = defaultdict(list)
    in_degree = {node: 0 for node in graph.nodes}
    for up, down in graph.dependencies:
        successors[up].append(down)
        predecessors[down].append(up)
        in_degree[down] += 1

    topo = []
    queue = sorted(node for node, deg in in_degree.items() if deg == 0)
    while queue:
        node = queue.pop(0)
        topo.append(node)
        for succ in sorted(successors.get(node, ())):
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)
                queue.sort()
    topo.extend(sorted(set(graph.nodes) - set(topo)))

    finish: dict[str, float] = {}
    group_free: dict[int, float] = defaultdict(float)
    for node in topo:
        gid = partition[node]
        start = group_free[gid]
        for pred in predecessors.get(node, ()):
            start = max(start, finish.get(pred, 0.0))
        finish[node] = start + work.get(node, 1.0)
        group_free[gid] = finish[node]
    return max(finish.values()) if finish else 0.0


def _build_reachability(graph: TaskGraph) -> dict[str, set[str]]:
    """Downstream closure per node over dependency edges (CoCoder _build_reachability)."""
    successors: dict[str, list[str]] = defaultdict(list)
    for up, down in graph.dependencies:
        successors[up].append(down)
    reach = {}
    for node in graph.nodes:
        seen: set[str] = set()
        stack = list(successors.get(node, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(successors.get(current, ()))
        reach[node] = seen
    return reach


def merge_small_groups(
    graph: TaskGraph,
    partition: Partition,
    work_fn: WorkFn,
    budget_cap: float | None,
    recorder: PartitionRecorder | None = None,
    granularity: Granularity = "independent",
    target_fill_ratio: float = DEFAULT_TARGET_FILL_RATIO,
) -> Partition:
    """CoCoder ``merge_small_groups`` (post_processing.py:295-401), always on.

    Bottom-up merge along dependency edges only — a group may merge only into a
    group it depends on — accepted only when the merged summed work stays within
    ``budget_cap`` (if set), and, depending on ``granularity`` (plan U4): the pair
    is ``chain_compatible`` (dependency-ordered — never a merge of truly parallel
    work into one serial worker) and the simulated zero-communication makespan
    does not regress. ``independent`` enforces both (today's default behaviour).

    Empirically (not just per Kim & Browne 1988), on every acyclic graph this
    partitioner ever produces, ``chain_compatible`` passing already guarantees
    the makespan check passes too — the total cross-group order it demands is
    exactly Sarkar's sufficient condition for a non-regressing merge. So
    relaxing the makespan check alone (independent -> drop only Sarkar) changes
    nothing observable; the guard that actually gates additional merges is
    ``chain_compatible``. ``balanced`` therefore drops ``chain_compatible``
    while keeping the makespan check as the sole acceptance test (Sarkar's test
    stands on its own without the total-order guarantee — it still rejects a
    merge that would genuinely serialize independent work); ``monolithic`` also
    drops the makespan check. ``over_budget`` and ``would_create_cycle`` are
    never relaxed. CoCoder gates this behind an env var, off by default;
    unbounded clustering output is exactly the over-fragmentation this system
    exists to prevent, so here it always runs.
    """
    relax_chain = granularity in ("balanced", "monolithic")
    relax_makespan = granularity == "monolithic"
    work = {node: work_fn(node) for node in graph.nodes}
    reachability = _build_reachability(graph)
    node_waves = _compute_waves(graph)

    def group_work(members: list[str]) -> float:
        return sum(work[n] for n in members)

    def chain_compatible(members_a: list[str], members_b: list[str]) -> bool:
        # Every cross pair must be dependency-ordered one way or the other:
        # merging truly parallel nodes into one serial worker loses parallelism.
        for a in members_a:
            for b in members_b:
                if a != b and b not in reachability[a] and a not in reachability[b]:
                    return False
        return True

    merge_round = 0
    while True:
        groups: dict[int, list[str]] = defaultdict(list)
        for node, gid in partition.items():
            groups[gid].append(node)

        if budget_cap is None:
            eligible = set(groups)
        else:
            eligible = {gid for gid, members in groups.items() if group_work(members) < budget_cap}
        if not eligible:
            break

        # Bottom-up only: dependent group S may merge into its dependency group N.
        pair_edges: dict[tuple[int, int], float] = defaultdict(float)
        for (up, down), weight in graph.dependencies.items():
            up_gid, down_gid = partition[up], partition[down]
            if up_gid != down_gid and down_gid in eligible:
                pair_edges[(down_gid, up_gid)] += weight

        current_makespan = _simulate_makespan(graph, partition, work)

        best: tuple[int, int] | None = None
        best_key: tuple | None = None
        for (source, target), edge_weight in sorted(pair_edges.items()):
            merged_work = group_work(groups[source]) + group_work(groups[target])
            if budget_cap is not None and merged_work > budget_cap:
                if recorder is not None:
                    recorder.record_merge_candidate(
                        merge_round, source, target, False, "over_budget", merged_work, edge_weight
                    )
                continue
            if not relax_chain and not chain_compatible(groups[source], groups[target]):
                if recorder is not None:
                    recorder.record_merge_candidate(
                        merge_round,
                        source,
                        target,
                        False,
                        "not_chain_compatible",
                        merged_work,
                        edge_weight,
                    )
                continue
            candidate = {
                node: (target if gid == source else gid) for node, gid in partition.items()
            }
            if not _is_acyclic(_group_edges(graph, candidate), set(candidate.values())):
                # Plan U4 (M2): folding an upstream hub's group together with a
                # downstream aggregator's group across an intermediate group
                # inverts an edge in the quotient graph — refuse it here
                # rather than let build_group_dag discover it at the end.
                if recorder is not None:
                    recorder.record_merge_candidate(
                        merge_round,
                        source,
                        target,
                        False,
                        "would_create_cycle",
                        merged_work,
                        edge_weight,
                    )
                continue
            candidate_makespan = _simulate_makespan(graph, candidate, work)
            if not relax_makespan and candidate_makespan > current_makespan + 1e-9:
                if recorder is not None:
                    recorder.record_merge_candidate(
                        merge_round,
                        source,
                        target,
                        False,
                        "makespan_regression",
                        merged_work,
                        edge_weight,
                    )
                continue
            removed_affinity = sum(
                w
                for pair, w in graph.affinity.items()
                if {partition[pair[0]], partition[pair[1]]} == {source, target}
            )
            source_wave = max(node_waves[n] for n in groups[source])
            # Plan U12 (R19b): distance of the *resulting* group's work from a
            # target-fill band, ranked above merged_work. merged_work alone
            # (the old 5th-place tiebreak) is monotonic — it always prefers
            # whichever candidate produces the smallest number, full stop — so
            # once some group is the cheapest available sink it keeps winning
            # every round, one small increment at a time, right up to the hard
            # cap, while a sibling that only has a bigger (but perfectly
            # reasonable) candidate available never gets picked at all.
            # Distance-from-band is not monotonic: a candidate that tops a
            # group up near the band scores well even if its merged_work is
            # numerically larger than a rival's, and a candidate that would
            # push a group past the band scores worse even if its merged_work
            # is numerically smaller — so a group stops looking like the best
            # option once it is well-filled, instead of being the greedy sink
            # for every merge until it hits ~96% of the cap. No cap means no
            # band to aim for, so the term is neutral (0) for every candidate.
            fill_gap = (
                abs(merged_work - target_fill_ratio * budget_cap) if budget_cap is not None else 0.0
            )
            key = (
                -source_wave,
                candidate_makespan,
                -removed_affinity,
                -edge_weight,
                fill_gap,
                merged_work,
                source,
                target,
            )
            if best_key is None or key < best_key:
                best, best_key = (source, target), key
        if best is None:
            break
        source, target = best
        if recorder is not None:
            merged_work = group_work(groups[source]) + group_work(groups[target])
            recorder.record_merge_candidate(
                merge_round, source, target, True, "", merged_work, pair_edges[(source, target)]
            )
        partition = {node: (target if gid == source else gid) for node, gid in partition.items()}
        merge_round += 1
    return partition


def _compute_waves(graph: TaskGraph) -> dict[str, int]:
    """Topological wave layer per node: wave = max(wave(dependencies)) + 1."""
    dependencies_of: dict[str, set[str]] = defaultdict(set)
    for up, down in graph.dependencies:
        dependencies_of[down].add(up)
    layer: dict[str, int] = {}
    changed = True
    while changed:
        changed = False
        for node in graph.nodes:
            if node in layer:
                continue
            deps = dependencies_of.get(node, set())
            if all(d in layer for d in deps):
                layer[node] = max([0] + [layer[d] + 1 for d in deps])
                changed = True
    for node in graph.nodes:
        layer.setdefault(node, 0)
    return layer


def _strongly_connected_components(
    edges: Mapping[_Node, set[_Node]], node_ids: set[_Node]
) -> list[list[_Node]]:
    """Kosaraju's SCC, sorted-order deterministic (plan U5): fixed traversal
    order at both passes makes the assignment of nodes to components
    reproducible across runs, which is what byte-stable repair (R18) needs
    from this step. Generic over the node type: used both for the group
    quotient graph (int ids) and the block-level graph inside a re-split
    (str block ids, see _resplit_by_wave)."""
    visited: set[_Node] = set()
    finish_order: list[_Node] = []
    for start in sorted(node_ids):
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[_Node, list[_Node]]] = [(start, sorted(edges.get(start, ())))]
        while stack:
            node, remaining = stack[-1]
            advanced = False
            while remaining:
                nxt = remaining.pop(0)
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, sorted(edges.get(nxt, ()))))
                    advanced = True
                    break
            if not advanced:
                finish_order.append(node)
                stack.pop()

    reverse_edges: dict[_Node, set[_Node]] = defaultdict(set)
    for u, downs in edges.items():
        for v in downs:
            reverse_edges[v].add(u)

    assigned: set[_Node] = set()
    components: list[list[_Node]] = []
    for node_id in reversed(finish_order):
        if node_id in assigned:
            continue
        assigned.add(node_id)
        component = [node_id]
        stack = [node_id]
        while stack:
            node = stack.pop()
            for nxt in sorted(reverse_edges.get(node, ())):
                if nxt not in assigned:
                    assigned.add(nxt)
                    component.append(nxt)
                    stack.append(nxt)
        components.append(sorted(component))
    return components


def _resplit_by_wave(
    graph: TaskGraph, members: list[str], work_fn: WorkFn, budget_cap: float
) -> list[list[str]]:
    """Chunk a merged group's members along ascending dependency-wave
    boundaries (plan U5), never splitting a slice block apart.

    This is what makes the re-split provably cycle-safe: an external group X
    cannot have edges in both directions against the members of a just-merged
    SCC (if it did, X would have been part of that SCC), so X's relationship
    to every chunk carved from those members is the same single direction as
    its relationship to the whole merged set. Chunking by ascending wave
    order — computed from dependencies internal to ``members`` only — means
    no chunk can ever depend on a later chunk either. Together, no re-split
    chunk boundary can introduce a new cross-group edge running the wrong way.
    """
    member_set = set(members)
    block_of = _slice_block_of(graph)
    blocks: dict[str, list[str]] = defaultdict(list)
    for node in members:
        blocks[block_of[node]].append(node)
    for nodes in blocks.values():
        nodes.sort()

    block_edges: dict[str, set[str]] = defaultdict(set)
    for up, down in graph.dependencies:
        if up in member_set and down in member_set:
            bu, bd = block_of[up], block_of[down]
            if bu != bd:
                block_edges[bu].add(bd)

    # A slice can straddle two blocks that were only cyclic at the group
    # level (see repair_cycles): e.g. a1/a2 share a slice while a1 -> b ->
    # a2, so contracting a1+a2 makes the slice block and b's block depend on
    # each other. Condensing this block graph's own SCCs first — the same
    # theorem repair_cycles already relies on — guarantees the wave loop
    # below always terminates instead of deadlocking on a mutual dependency.
    block_ids = set(blocks)
    super_of: dict[str, str] = {}
    for component in _strongly_connected_components(block_edges, block_ids):
        rep = min(component)
        for block_id in component:
            super_of[block_id] = rep
    supers: dict[str, list[str]] = defaultdict(list)
    for block_id, nodes in blocks.items():
        supers[super_of[block_id]].extend(nodes)
    for nodes in supers.values():
        nodes.sort()

    dependencies_of_super: dict[str, set[str]] = defaultdict(set)
    for bu, downs in block_edges.items():
        for bd in downs:
            su, sd = super_of[bu], super_of[bd]
            if su != sd:
                dependencies_of_super[sd].add(su)

    wave: dict[str, int] = {}
    changed = True
    while changed:
        changed = False
        for super_id in supers:
            if super_id in wave:
                continue
            deps = dependencies_of_super.get(super_id, set())
            if all(d in wave for d in deps):
                wave[super_id] = max([0] + [wave[d] + 1 for d in deps])
                changed = True
    ordered = sorted(supers, key=lambda s: (wave[s], s))

    chunks: list[list[str]] = []
    current: list[str] = []
    current_work = 0.0
    for super_id in ordered:
        super_members = supers[super_id]
        super_work = sum(work_fn(n) for n in super_members)
        if current and current_work + super_work > budget_cap:
            chunks.append(current)
            current, current_work = [], 0.0
        current.extend(super_members)
        current_work += super_work
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class DegenerateRepair:
    """One cyclic group-SCC ``repair_cycles`` could not re-split back under
    budget (plan U9/C4.2), carrying the task-level edges that closed the
    cycle — the same set ``evidence_edges`` computes internally — so a caller
    can classify them (declared vs. inferred) without a second SCC walk.
    """

    cycle_groups: tuple[int, ...]
    evidence_edges: tuple[Pair, ...]
    overshoot_messages: tuple[str, ...]


def repair_cycles(
    graph: TaskGraph,
    partition: Partition,
    work_fn: WorkFn,
    budget_cap: float | None,
    flags: list[str],
    recorder: PartitionRecorder | None = None,
    degenerate: list[DegenerateRepair] | None = None,
) -> Partition:
    """SCC-merge, then a mandatory dependency-safe re-split (plan U5).

    A cycle surviving the U4 merge guard can only have originated earlier —
    at Louvain, lift or split — so it is repaired here rather than raised to
    the caller. Merging every cyclic group-SCC's members into one supergroup
    always yields an acyclic condensation (standard SCC theorem); the
    wave-ordered re-split that follows (``_resplit_by_wave``) never
    reintroduces a cycle. A chunk that still cannot fit under budget after
    re-splitting is left over budget with an entry appended to ``flags``
    naming it and the overshoot, rather than failing — these are greenfield
    estimates, and a hard failure here would be unactionable. When ``degenerate``
    is given, the same overshoot also lands there as a ``DegenerateRepair``
    (plan U9) — a structured counterpart the caller can source edge-provenance
    counts from without re-deriving the SCC.
    """
    group_edges = _group_edges(graph, partition)
    gids = set(partition.values())
    components = _strongly_connected_components(group_edges, gids)
    cyclic = [c for c in components if len(c) > 1]
    if not cyclic:
        return partition

    def evidence_edges(component: list[int]) -> list[Pair]:
        members = set(component)
        return sorted(
            (up, down)
            for up, down in graph.dependencies
            if partition[up] in members
            and partition[down] in members
            and partition[up] != partition[down]
        )

    merge_target = {gid: min(component) for component in cyclic for gid in component}
    merged_partition = {node: merge_target.get(gid, gid) for node, gid in partition.items()}
    if budget_cap is None:
        if recorder is not None:
            for component in cyclic:
                recorder.record_repair(
                    sorted(component), evidence_edges(component), min(component), [], []
                )
        return merged_partition

    merged_groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in merged_partition.items():
        merged_groups[gid].append(node)

    result = dict(merged_partition)
    next_gid = max(partition.values(), default=-1) + 1
    for component in cyclic:
        target = min(component)
        members = sorted(merged_groups[target])
        total = sum(work_fn(n) for n in members)
        if total <= budget_cap:
            if recorder is not None:
                recorder.record_repair(sorted(component), evidence_edges(component), target, [], [])
            continue
        chunks = _resplit_by_wave(graph, members, work_fn, budget_cap)
        overshoots: list[str] = []
        for chunk in chunks:
            chunk_work = sum(work_fn(n) for n in chunk)
            if chunk_work > budget_cap:
                message = (
                    f"partition: group containing {chunk[0]!r} stays "
                    f"{chunk_work - budget_cap:.0f} over the {budget_cap:.0f} cap "
                    "after cycle repair (an acyclic re-split under budget did not exist)"
                )
                flags.append(message)
                overshoots.append(message)
        if recorder is not None:
            recorder.record_repair(
                sorted(component),
                evidence_edges(component),
                target,
                [list(chunk) for chunk in chunks],
                overshoots,
            )
        if overshoots and degenerate is not None:
            degenerate.append(
                DegenerateRepair(
                    cycle_groups=tuple(sorted(component)),
                    evidence_edges=tuple(evidence_edges(component)),
                    overshoot_messages=tuple(overshoots),
                )
            )
        first, *rest = chunks
        for node in first:
            result[node] = target
        for chunk in rest:
            for node in chunk:
                result[node] = next_gid
            next_gid += 1
    return result
