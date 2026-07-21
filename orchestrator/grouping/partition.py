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

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

Pair = tuple[str, str]
# node → group id. Group ids are contiguous ints; deterministic across runs.
Partition = dict[str, int]
# Injected estimator hook: relative work for one task node (tokens, symbols — any
# consistent unit). Never imported from the estimator module: keeps this module pure.
WorkFn = Callable[[str], float]

DEFAULT_HUB_THRESHOLD = 0.4  # CoCoder's live ROLE_THRESHOLD (partition_into_groups.py:37)
LOUVAIN_SEED = 42


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


class SingleGroupStrategy:
    """Trivial passthrough: every task in one group. Proves the R22 seam."""

    def partition(self, graph: TaskGraph) -> Partition:
        return {node: 0 for node in sorted(graph.nodes)}


def build_group_dag(graph: TaskGraph, partition: Partition) -> dict[int, set[int]]:
    """Group-level dependency edges {upstream_gid: {downstream_gid}}; cycles fail loudly.

    CoCoder has no cycle detection (a cyclic graph silently wedges its scheduler);
    here a cycle raises GroupCycleError naming the groups and the task edges that
    close the cycle.
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


@dataclass
class DefaultPartitionStrategy:
    """CoCoder's pipeline, ported: hub isolation → slice contraction → Louvain →
    lift → split → merge.

    ``work_fn`` and ``budget_cap`` are the injected estimator hooks: work is the
    relative size of one task node and the cap bounds a group's summed work. With
    the defaults (work 1.0, no cap) size policies degrade to CoCoder's file-count
    behavior with an unlimited group size.

    Slice labels (task-map must-links, docs/orchestrator-task-map.md) enter as
    deterministic node contraction: hub roles are detected first (a hub is never
    absorbed into a slice), each slice's core members contract into one supernode
    for clustering and lifting, then membership expands. Softness comes after
    expansion: ``split_over_budget`` may still break an oversized slice at its
    weakest internal edges and ``merge_small_groups`` may combine small ones.
    """

    work_fn: WorkFn = lambda node: 1.0
    budget_cap: float | None = None
    hub_threshold: float = DEFAULT_HUB_THRESHOLD
    louvain_resolution: float = 1.0

    def partition(self, graph: TaskGraph) -> Partition:
        if not graph.nodes:
            return {}
        roles = detect_hub_roles(graph, threshold=self.hub_threshold)
        atoms = _slice_atoms(graph, roles)
        if atoms:
            unit_graph, self_loops, unit_of = _contract_slices(graph, atoms)
            unit_roles = {unit_of[node]: role for node, role in roles.items()}
            unit_partition = _hub_isolated_clustering(
                unit_graph, unit_roles, self.louvain_resolution, self_loops
            )
            unit_partition = lift_independent(unit_graph, unit_partition)
            partition = {node: unit_partition[unit_of[node]] for node in graph.nodes}
        else:
            partition = _hub_isolated_clustering(graph, roles, self.louvain_resolution)
            partition = lift_independent(graph, partition)
        if self.budget_cap is not None:
            partition = split_over_budget(graph, partition, self.work_fn, self.budget_cap)
        partition = merge_small_groups(graph, partition, self.work_fn, self.budget_cap)
        partition = _renumber(partition)
        build_group_dag(graph, partition)  # cycles must fail loudly (plan U1)
        return partition


def detect_hub_roles(graph: TaskGraph, threshold: float = DEFAULT_HUB_THRESHOLD) -> dict[str, str]:
    """Degree-thresholded roles: utility_hub / aggregator_hub / core.

    Port of CoCoder ``detect_roles`` (common.py:43-63) by *behavior*: a node most
    others depend on is a ``utility_hub`` (CoCoder's misnamed ``in_hub``) and is
    isolated as its own group; a node depending on most others is an
    ``aggregator_hub`` (CoCoder's misnamed ``out_hub``) and lands in one trailing
    shared group. CoCoder classifies the aggregator check first — kept.
    """
    n = len(graph.nodes)
    if n <= 1:
        return {node: "core" for node in graph.nodes}
    dependencies_of: dict[str, set[str]] = defaultdict(set)
    dependents_of: dict[str, set[str]] = defaultdict(set)
    for up, down in graph.dependencies:
        dependencies_of[down].add(up)
        dependents_of[up].add(down)
    roles = {}
    for node in graph.nodes:
        depends_on = len(dependencies_of[node]) / (n - 1)
        depended_by = len(dependents_of[node]) / (n - 1)
        if depends_on > threshold:
            roles[node] = "aggregator_hub"
        elif depended_by > threshold:
            roles[node] = "utility_hub"
        else:
            roles[node] = "core"
    return roles


def _slice_atoms(graph: TaskGraph, roles: dict[str, str]) -> dict[str, list[str]]:
    """Slice label → sorted core members with 2+ tasks (the real must-links).

    Reads the ``slice`` node metadata the task map supplies. Hub-role nodes are
    excluded — hubs are isolated before slices contract and are never absorbed
    into a feature slice.
    """
    atoms: dict[str, list[str]] = defaultdict(list)
    for node in sorted(graph.nodes):
        if roles.get(node) != "core":
            continue
        label = graph.metadata.get(node, {}).get("slice")
        if isinstance(label, str) and label:
            atoms[label].append(node)
    return {label: members for label, members in sorted(atoms.items()) if len(members) >= 2}


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
    return {node: gid for gid, members in enumerate(ordered) for node in members}


def _hub_isolated_clustering(
    graph: TaskGraph,
    roles: dict[str, str],
    resolution: float,
    self_loops: Mapping[str, float] | None = None,
) -> Partition:
    """CoCoder ``role_grouping`` (post_processing.py:13-33): cluster only the core.

    Utility hubs come first as singleton groups, Louvain communities next,
    aggregator hubs last in one shared group.
    """
    utility_hubs = sorted(n for n, r in roles.items() if r == "utility_hub")
    aggregator_hubs = sorted(n for n, r in roles.items() if r == "aggregator_hub")
    core = {n for n, r in roles.items() if r == "core"}
    core_partition = _louvain(graph, core, resolution, self_loops)

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


def split_over_budget(
    graph: TaskGraph, partition: Partition, work_fn: WorkFn, budget_cap: float
) -> Partition:
    """Split any group whose summed work exceeds the cap at its lowest-affinity boundary.

    Not in CoCoder (its clustering output is size-unbounded upward); required by
    origin AE2. Reverse-Kruskal: drop the weakest internal affinity edges until the
    group falls apart, recurse on any component still over budget. A single node
    over budget stays a singleton — the estimator flags it downstream.
    """
    groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in partition.items():
        groups[gid].append(node)

    new_partition: Partition = {}
    next_gid = 0
    pending = [sorted(m) for _gid, m in sorted(groups.items())]
    while pending:
        members = pending.pop(0)
        total = sum(work_fn(n) for n in members)
        if total <= budget_cap or len(members) == 1:
            for node in members:
                new_partition[node] = next_gid
            next_gid += 1
            continue
        member_set = set(members)
        internal = sorted(
            ((w, pair) for pair, w in graph.affinity.items() if set(pair) <= member_set),
            key=lambda item: (item[0], item[1]),
        )
        components = _components_after_cut(member_set, internal)
        if len(components) == 1:
            # Fully cohesive at every weight level: cut the single weakest edge set
            # couldn't separate it, so peel the smallest-work node deterministically.
            peel = min(members, key=lambda n: (work_fn(n), n))
            components = [[peel], sorted(member_set - {peel})]
        pending = components + pending
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
) -> Partition:
    """CoCoder ``merge_small_groups`` (post_processing.py:295-401), always on.

    Bottom-up merge along dependency edges only — a group may merge only into a
    group it depends on — accepted only when the merged summed work stays within
    ``budget_cap`` (if set) and the simulated zero-communication makespan does not
    regress. CoCoder gates this behind an env var, off by default; unbounded
    clustering output is exactly the over-fragmentation this system exists to
    prevent, so here it always runs.
    """
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
                continue
            if not chain_compatible(groups[source], groups[target]):
                continue
            candidate = {
                node: (target if gid == source else gid) for node, gid in partition.items()
            }
            candidate_makespan = _simulate_makespan(graph, candidate, work)
            if candidate_makespan > current_makespan + 1e-9:
                continue
            removed_affinity = sum(
                w
                for pair, w in graph.affinity.items()
                if {partition[pair[0]], partition[pair[1]]} == {source, target}
            )
            source_wave = max(node_waves[n] for n in groups[source])
            key = (
                -source_wave,
                candidate_makespan,
                -removed_affinity,
                -edge_weight,
                merged_work,
                source,
                target,
            )
            if best_key is None or key < best_key:
                best, best_key = (source, target), key
        if best is None:
            break
        source, target = best
        partition = {node: (target if gid == source else gid) for node, gid in partition.items()}
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
