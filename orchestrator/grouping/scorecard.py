"""Grouping quality scorecard (plan U5): a fixed set of numbers computed once
for every partition, printed by ``group --no-spec``, and recorded verbatim into
``grouping-trace.json`` — the two must never drift, so both read from this one
computation rather than each recomputing it independently.

None of this feeds back into partitioning decisions (observation is inert,
same discipline as ``TraceRecorder``): it is purely a report on a partition
already produced by ``partition.py``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator.grouping.partition import Partition, TaskGraph


@dataclass(frozen=True)
class Scorecard:
    group_count: int
    cross_group_edges: int
    work_fraction_min: float
    work_fraction_mean: float
    work_fraction_max: float
    critical_path_length: int
    modularity: float
    slice_integrity_ok: bool


def compute_scorecard(
    graph: TaskGraph,
    partition: Partition,
    node_work: Mapping[str, float],
    budget_cap: float,
    dag: Mapping[int, set[int]],
    slice_atoms: Mapping[str, list[str]],
) -> Scorecard:
    groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in partition.items():
        groups[gid].append(node)

    cross_group_edges = sum(
        1 for up, down in graph.dependencies if partition[up] != partition[down]
    )

    fractions = [
        (sum(node_work.get(n, 0.0) for n in members) / budget_cap if budget_cap else 0.0)
        for members in groups.values()
    ]

    slice_integrity_ok = all(
        len({partition[m] for m in members if m in partition}) <= 1
        for members in slice_atoms.values()
    )

    return Scorecard(
        group_count=len(groups),
        cross_group_edges=cross_group_edges,
        work_fraction_min=min(fractions) if fractions else 0.0,
        work_fraction_mean=(sum(fractions) / len(fractions)) if fractions else 0.0,
        work_fraction_max=max(fractions) if fractions else 0.0,
        critical_path_length=_longest_group_chain(dag, set(groups)),
        modularity=_modularity(graph, partition),
        slice_integrity_ok=slice_integrity_ok,
    )


def _longest_group_chain(dag: Mapping[int, set[int]], gids: set[int]) -> int:
    """Number of groups on the longest chain of the group-level DAG — the
    partition's own serialization depth, independent of any work-time model.
    ``dag`` is guaranteed acyclic by the time a scorecard is computed
    (``build_group_dag`` already raised otherwise), so plain memoized recursion
    terminates."""
    predecessors_of: dict[int, list[int]] = defaultdict(list)
    for up, downs in dag.items():
        for down in downs:
            predecessors_of[down].append(up)

    memo: dict[int, int] = {}

    def depth(gid: int) -> int:
        if gid in memo:
            return memo[gid]
        preds = predecessors_of.get(gid, ())
        memo[gid] = 1 + max((depth(p) for p in preds), default=0)
        return memo[gid]

    return max((depth(gid) for gid in gids), default=0)


def _modularity(graph: TaskGraph, partition: Partition) -> float:
    """Newman modularity Q over the affinity graph (never computed anywhere
    before this unit — graphing.py and partition.py only mention it in
    comments). networkx is imported lazily, matching ``partition._louvain``'s
    own discipline of keeping module-level imports stdlib-only elsewhere."""
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(graph.nodes)
    for (a, b), w in graph.affinity.items():
        if w > 0:
            g.add_edge(a, b, weight=w)
    if g.number_of_edges() == 0:
        return 0.0

    communities_by_gid: dict[int, set[str]] = defaultdict(set)
    for node, gid in partition.items():
        communities_by_gid[gid].add(node)
    communities = [members for members in communities_by_gid.values() if members]
    return nx.algorithms.community.quality.modularity(g, communities, weight="weight")
