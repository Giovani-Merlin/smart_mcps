"""`group --advise` (plan U11, R12-R14): one graph build, every granularity
preset, cohesion diagnostics — zero LLM calls, never touches ``groups.json``.

The whole point of this module is that it never calls the mapper/codegraph
work more than once per invocation: ``build_partition_graph`` (pipeline.py's
R19 seam) runs a single time, and every ``GRANULARITY_LEVELS`` preset below
partitions the *same* ``TaskGraph`` object. Partitioning itself (Louvain,
lift, split, merge, repair) is pure Python over that in-memory graph, so
running it three times costs milliseconds, not more codegraph subprocesses.

Preview semantics (plan U8's ``preview/`` quarantine, reused unchanged): the
CLI writes this module's output only under ``preview/advisory.json`` and
never touches a persisted ``groups.json`` — see ``_cmd_group``'s ``--advise``
branch.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.estimator import node_work
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.llm import JsonRunner, claude_json_runner
from orchestrator.grouping.partition import (
    GRANULARITY_LEVELS,
    DefaultPartitionStrategy,
    Granularity,
    Partition,
    TaskGraph,
    _compute_waves,
    _simulate_makespan,
    build_group_dag,
)
from orchestrator.grouping.pipeline import ProgressFn, _emit, build_partition_graph
from orchestrator.grouping.scorecard import _modularity

# --------------------------------------------------------------------- tuning
#
# All four thresholds below are named module constants (plan U11's own
# requirement, so the advisory report's judgment calls are visible and
# adjustable in one place, not buried in expressions). Each is justified
# against this repo's own edge-weight/estimator conventions rather than
# picked arbitrarily; "tune in 1c" (the plan's decision) means these are
# expected to move as the eval harness (R20, out of scope here) accumulates
# more real runs to check them against — nothing here claims to be final.

# WCC-before-Louvain (R13): an affinity edge weaker than the *weakest* signal
# the edge-weight model can produce (EdgeWeights.semantic_floor = 0.5, plan
# U... graphing.py) does not meaningfully bridge two clusters — pruning
# edges at or below that floor and re-checking connectivity is what turns a
# single weakly-connected component into a "near-disconnected" one. Declared
# dependency edges are never pruned: they are required precedence, not a soft
# affinity signal, so they can never make a plan read as "separate" no matter
# how weak their weight.
NEAR_DISCONNECTED_EDGE_WEIGHT_THRESHOLD = 0.5

# Seriality (critical path / max wave width): a plan whose longest dependency
# chain is at least twice as long as its widest wave has, by construction,
# more depth than breadth — most of the plan's tasks are waiting on a
# predecessor rather than running in parallel with siblings. 2.0 is the point
# where "somewhat serial" tips into "this plan reads as serial phases" rather
# than "a DAG with some structure"; a plan that is one task wide throughout
# (a pure chain) scores arbitrarily high above this, a balanced fan-out/fan-in
# DAG scores at or below 1.0.
SERIALITY_DEPTH_WIDTH_RATIO_THRESHOLD = 2.0

# Cut-sweep valley detection: a candidate phase boundary is a topological-order
# split whose crossing dependency-edge count is a strict local minimum (fewer
# edges cross here than on either side) — the sweep's natural "waist" points.
# No magnitude threshold is applied on top of the local-minimum test itself:
# any strict local minimum is reported, since a valley that fails to be one
# is already excluded by construction; the module constant exists so the
# comparison is named rather than an inline `<` at each call site, and so a
# future revision that *does* want a magnitude floor has one place to add it.
CUT_SWEEP_VALLEY_MARGIN = 0

# "Structurally monolithic": modularity below this is Newman's own rule of
# thumb for "no meaningful community structure" (values above roughly 0.3-0.7
# indicate real cluster structure in practice; this repo's own healthy
# partitions score well above 0.1 per grouping-metrics.jsonl history) — so
# 0.1 is a conservative floor, not a guess. Paired with conductance: even the
# *best* achievable cut (minimum over the whole topological sweep) must leave
# most of the smaller side's connections crossing it (0.6) for the plan to be
# called monolithic — a plan is only flagged when neither Louvain nor any
# ordered bipartition finds a good split.
MONOLITHIC_MODULARITY_THRESHOLD = 0.1
MONOLITHIC_CONDUCTANCE_THRESHOLD = 0.6


# -------------------------------------------------------------- report shape


class GranularityMetrics(BaseModel):
    """One ``GRANULARITY_LEVELS`` preset's readout, all computed off the one
    cached ``TaskGraph`` (R12/R13)."""

    granularity: Literal["independent", "balanced", "monolithic"]
    group_count: int
    node_work_fraction_mean: float
    node_work_fraction_max: float
    cross_group_edge_cut: int
    group_dag_depth: int
    simulated_makespan: float
    modularity: float
    pareto_dominant: bool


class CohesionFinding(BaseModel):
    """One zero-LLM structural diagnostic (R13): disconnection, seriality, or
    monolithic structure. ``task_sets``/``boundary``/``detail`` are populated
    per ``kind`` — never all three at once — so a reader can tell what fired
    without inspecting the message text."""

    kind: Literal["disconnected", "serial", "monolithic"]
    message: str
    task_sets: list[list[str]] = []
    boundary: dict[str, object] = {}


class AdvisoryReport(BaseModel):
    """The ``advisory.json`` payload (plan U11): one entry per granularity
    preset plus the cohesion diagnostics, all derived from a single graph
    build with zero LLM calls."""

    version: int = 1
    plan_path: str
    granularities: list[GranularityMetrics]
    cohesion: list[CohesionFinding]


def serialize_advisory_report(report: AdvisoryReport) -> str:
    """Canonical ``advisory.json`` bytes — the same determinism contract as
    ``serialize_grouping``."""
    return report.model_dump_json(indent=2) + "\n"


# ---------------------------------------------------------------- main entry


def build_advisory_report(
    plan_path: Path,
    repo_root: Path,
    config: OrchestratorConfig | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
    allow_unknown_symbols: bool = False,
    progress: ProgressFn | None = None,
) -> AdvisoryReport:
    """Build the task graph once, partition it at every granularity preset,
    and compute the cohesion diagnostics — the whole ``--advise`` pipeline.

    No cross-invocation cache (plan U11 decision): a second call rebuilds the
    graph from scratch. Reuse is within this one call only.
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
        progress=progress,
    )
    graph = build.graph
    node_work_map = {
        node: node_work(graph.metadata.get(node, {}), config.estimator) for node in graph.nodes
    }

    def node_work_fn(node: str) -> float:
        return node_work_map[node]

    _emit(progress, "stage: advise")
    presets: list[GranularityMetrics] = []
    raw_metrics: dict[Granularity, dict] = {}
    for level in GRANULARITY_LEVELS:
        strategy = DefaultPartitionStrategy(
            work_fn=node_work_fn,
            budget_cap=build.budget_cap,
            hub_threshold=config.partition.hub_threshold,
            louvain_resolution=config.partition.louvain_resolution,
            granularity=level,
        )
        partition = strategy.partition(graph)
        dag = build_group_dag(graph, partition)
        raw_metrics[level] = _preset_metrics(graph, partition, dag, node_work_map, build.budget_cap)

    pareto = _pareto_dominant(raw_metrics)
    for level in GRANULARITY_LEVELS:
        metrics = raw_metrics[level]
        presets.append(
            GranularityMetrics(
                granularity=level,
                group_count=metrics["group_count"],
                node_work_fraction_mean=metrics["node_work_fraction_mean"],
                node_work_fraction_max=metrics["node_work_fraction_max"],
                cross_group_edge_cut=metrics["cross_group_edge_cut"],
                group_dag_depth=metrics["group_dag_depth"],
                simulated_makespan=metrics["simulated_makespan"],
                modularity=metrics["modularity"],
                pareto_dominant=pareto[level],
            )
        )

    cohesion = _cohesion_diagnostics(graph, node_work_map)

    from orchestrator.grouping.pipeline import _portable_path

    return AdvisoryReport(
        plan_path=_portable_path(plan_path, repo_root),
        granularities=presets,
        cohesion=cohesion,
    )


# ------------------------------------------------------------- per-preset


def _preset_metrics(
    graph: TaskGraph,
    partition: Partition,
    dag: dict[int, set[int]],
    node_work_map: Mapping[str, float],
    budget_cap: float,
) -> dict:
    groups: dict[int, list[str]] = defaultdict(list)
    for node, gid in partition.items():
        groups[gid].append(node)

    fractions = [
        (sum(node_work_map.get(n, 0.0) for n in members) / budget_cap if budget_cap else 0.0)
        for members in groups.values()
    ]
    cross_group_edge_cut = sum(
        1 for up, down in graph.dependencies if partition[up] != partition[down]
    )
    return {
        "group_count": len(groups),
        "node_work_fraction_mean": (sum(fractions) / len(fractions)) if fractions else 0.0,
        "node_work_fraction_max": max(fractions) if fractions else 0.0,
        "cross_group_edge_cut": cross_group_edge_cut,
        "group_dag_depth": _dag_depth(dag, set(groups)),
        "simulated_makespan": _simulate_makespan(graph, partition, node_work_map),
        "modularity": _modularity(graph, partition),
    }


def _dag_depth(dag: Mapping[int, set[int]], gids: set[int]) -> int:
    """Same longest-chain computation as ``scorecard._longest_group_chain``,
    duplicated rather than imported (it is private there and this module
    should not reach into another module's underscore names)."""
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


# Objective direction per metric used to rank presets (R14): "better" always
# means "closer to what an operator running the group would want" — fewer
# cross-group edges to coordinate, a shallower serialization depth, a lower
# simulated makespan, higher modularity (cleaner clusters), and higher mean
# utilization of the budget cap (less headroom wasted). All five are used
# together so a preset that merely inflates one number cannot look dominant.
_MINIMIZE = ("cross_group_edge_cut", "group_dag_depth", "simulated_makespan")
_MAXIMIZE = ("modularity", "node_work_fraction_mean")


def _pareto_dominant(raw_metrics: Mapping[Granularity, dict]) -> dict[Granularity, bool]:
    def dominates(a: dict, b: dict) -> bool:
        at_least_as_good = all(a[k] <= b[k] for k in _MINIMIZE) and all(
            a[k] >= b[k] for k in _MAXIMIZE
        )
        strictly_better = any(a[k] < b[k] for k in _MINIMIZE) or any(a[k] > b[k] for k in _MAXIMIZE)
        return at_least_as_good and strictly_better

    result = {}
    for level, metrics in raw_metrics.items():
        dominated = any(
            dominates(other, metrics)
            for other_level, other in raw_metrics.items()
            if other_level != level
        )
        result[level] = not dominated
    return result


# ------------------------------------------------------------- cohesion


def _undirected_affinity_graph(graph: TaskGraph) -> dict[str, dict[str, float]]:
    """Weighted undirected adjacency over affinity only — the same scope
    ``_louvain``/``_modularity`` cluster over, so conductance and modularity
    are judging the same structure."""
    adjacency: dict[str, dict[str, float]] = {node: {} for node in graph.nodes}
    for (a, b), w in graph.affinity.items():
        if w <= 0:
            continue
        adjacency[a][b] = adjacency[a].get(b, 0.0) + w
        adjacency[b][a] = adjacency[b].get(a, 0.0) + w
    return adjacency


def _combined_adjacency(
    graph: TaskGraph, *, min_affinity_weight: float = 0.0
) -> dict[str, set[str]]:
    """Undirected connectivity over affinity (weight-thresholded) plus
    dependencies (never thresholded — declared precedence is always
    structural, never a "weak" signal)."""
    adjacency: dict[str, set[str]] = {node: set() for node in graph.nodes}
    for (a, b), w in graph.affinity.items():
        if w < min_affinity_weight:
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)
    for a, b in graph.dependencies:
        adjacency[a].add(b)
        adjacency[b].add(a)
    return adjacency


def _weakly_connected_components(adjacency: Mapping[str, set[str]]) -> list[list[str]]:
    visited: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(adjacency):
        if start in visited:
            continue
        component = []
        queue = [start]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            component.append(node)
            queue.extend(nb for nb in sorted(adjacency.get(node, ())) if nb not in visited)
        components.append(sorted(component))
    return sorted(components, key=lambda c: c[0] if c else "")


def _topo_order(graph: TaskGraph) -> list[str]:
    """Nodes ordered by ascending dependency wave, tie-broken by name — a
    valid topological order for the cut sweep below."""
    waves = _compute_waves(graph)
    return sorted(graph.nodes, key=lambda n: (waves[n], n))


def _cut_dependency_count(graph: TaskGraph, prefix: set[str], suffix: set[str]) -> int:
    return sum(1 for up, down in graph.dependencies if up in prefix and down in suffix)


def _conductance(
    prefix: set[str], suffix: set[str], adjacency: Mapping[str, Mapping[str, float]]
) -> float:
    def volume(side: set[str]) -> float:
        return sum(sum(adjacency.get(n, {}).values()) for n in side)

    cut = sum(w for n in prefix for nb, w in adjacency.get(n, {}).items() if nb in suffix)
    vol_prefix, vol_suffix = volume(prefix), volume(suffix)
    denom = min(vol_prefix, vol_suffix)
    if denom <= 0:
        return 1.0  # no internal structure to preserve on the smaller side: worst-case cut
    return cut / denom


@dataclass
class _SweepPoint:
    index: int
    prefix: list[str]
    suffix: list[str]
    dependency_cut: int
    conductance: float


def _cut_sweep(graph: TaskGraph, order: list[str], affinity_adjacency: dict) -> list[_SweepPoint]:
    points = []
    for i in range(1, len(order)):
        prefix, suffix = order[:i], order[i:]
        points.append(
            _SweepPoint(
                index=i,
                prefix=prefix,
                suffix=suffix,
                dependency_cut=_cut_dependency_count(graph, set(prefix), set(suffix)),
                conductance=_conductance(set(prefix), set(suffix), affinity_adjacency),
            )
        )
    return points


def _valleys(points: list[_SweepPoint]) -> list[_SweepPoint]:
    """Strict local minima of the cut-count sweep (R13's "cut-sweep valleys"):
    a point whose crossing-edge count is lower than both neighbors names a
    candidate phase boundary. Boundary points (the sweep's first/last split)
    qualify too when they are lower than their one neighbor."""
    valleys = []
    for i, point in enumerate(points):
        left = points[i - 1].dependency_cut if i > 0 else None
        right = points[i + 1].dependency_cut if i + 1 < len(points) else None
        lower_than_left = left is None or point.dependency_cut < left - CUT_SWEEP_VALLEY_MARGIN
        lower_than_right = right is None or point.dependency_cut < right - CUT_SWEEP_VALLEY_MARGIN
        if lower_than_left and lower_than_right and (left is not None or right is not None):
            valleys.append(point)
    return valleys


def _cohesion_diagnostics(
    graph: TaskGraph, node_work_map: Mapping[str, float]
) -> list[CohesionFinding]:
    if not graph.nodes:
        return []

    findings: list[CohesionFinding] = []

    full_adjacency = _combined_adjacency(graph)
    full_components = _weakly_connected_components(full_adjacency)
    pruned_adjacency = _combined_adjacency(
        graph, min_affinity_weight=NEAR_DISCONNECTED_EDGE_WEIGHT_THRESHOLD
    )
    pruned_components = _weakly_connected_components(pruned_adjacency)

    if len(full_components) > 1:
        findings.append(
            CohesionFinding(
                kind="disconnected",
                message=(
                    f"this reads as {len(full_components)} separate plans — no task "
                    "in one set shares a dependency or affinity edge with any task "
                    "in another"
                ),
                task_sets=full_components,
            )
        )
    elif len(pruned_components) > 1:
        findings.append(
            CohesionFinding(
                kind="disconnected",
                message=(
                    f"this reads as {len(pruned_components)} separate plans — the "
                    "only edges bridging these task sets are weaker than a single "
                    f"shared-file link (weight < {NEAR_DISCONNECTED_EDGE_WEIGHT_THRESHOLD})"
                ),
                task_sets=pruned_components,
            )
        )

    waves = _compute_waves(graph)
    critical_path_length = max(waves.values()) + 1 if waves else 0
    width_by_wave: dict[int, list[str]] = defaultdict(list)
    for node, wave in waves.items():
        width_by_wave[wave].append(node)
    max_wave_width = max((len(members) for members in width_by_wave.values()), default=0)
    seriality_ratio = (critical_path_length / max_wave_width) if max_wave_width else 0.0

    order = _topo_order(graph)
    affinity_adjacency = _undirected_affinity_graph(graph)
    points = _cut_sweep(graph, order, affinity_adjacency) if len(order) > 1 else []

    if seriality_ratio >= SERIALITY_DEPTH_WIDTH_RATIO_THRESHOLD:
        work_by_wave = {
            wave: sum(node_work_map.get(n, 0.0) for n in members)
            for wave, members in width_by_wave.items()
        }
        ordered_waves = sorted(work_by_wave)
        boundary_wave = ordered_waves[0]
        boundary_gap = -1.0
        for a, b in zip(ordered_waves, ordered_waves[1:]):
            gap = abs(work_by_wave[b] - work_by_wave[a])
            if gap > boundary_gap:
                boundary_gap = gap
                boundary_wave = a
        findings.append(
            CohesionFinding(
                kind="serial",
                message=(
                    f"this reads as serial phases — the critical path ({critical_path_length} "
                    f"waves) is {seriality_ratio:.1f}x the widest wave ({max_wave_width} tasks); "
                    f"the widest work gap falls between wave {boundary_wave} and wave "
                    f"{boundary_wave + 1}"
                ),
                boundary={
                    "critical_path_length": critical_path_length,
                    "max_wave_width": max_wave_width,
                    "boundary_after_wave": boundary_wave,
                    "tasks_before": sorted(width_by_wave.get(boundary_wave, [])),
                    "tasks_after": sorted(width_by_wave.get(boundary_wave + 1, [])),
                },
            )
        )
        valleys = _valleys(points)
        if valleys:
            findings.append(
                CohesionFinding(
                    kind="serial",
                    message=(
                        f"{len(valleys)} candidate phase boundary(ies) found by the "
                        "topological cut sweep — the fewest dependency edges cross "
                        "the plan at these splits"
                    ),
                    boundary={
                        "valleys": [
                            {
                                "after_index": v.index,
                                "dependency_cut": v.dependency_cut,
                                "tasks_before": v.prefix,
                                "tasks_after": v.suffix,
                            }
                            for v in valleys
                        ]
                    },
                )
            )

    has_affinity = any(w > 0 for w in graph.affinity.values())
    if points and has_affinity:
        # No affinity edges at all (a plan whose tasks share nothing but
        # dependency order) has no clustering structure to judge in the first
        # place — that is a pure-sequencing plan, not a monolithic one, so the
        # diagnostic is skipped rather than reporting the vacuous "cut leaves
        # everything on one side" conductance of an edgeless graph.
        best = min(points, key=lambda p: p.conductance)
        graph_modularity = _best_effort_modularity(graph)
        if (
            graph_modularity < MONOLITHIC_MODULARITY_THRESHOLD
            and best.conductance > MONOLITHIC_CONDUCTANCE_THRESHOLD
        ):
            findings.append(
                CohesionFinding(
                    kind="monolithic",
                    message=(
                        "this reads as structurally monolithic — modularity "
                        f"({graph_modularity:.3f}) is below {MONOLITHIC_MODULARITY_THRESHOLD} and "
                        f"even the best cut leaves conductance {best.conductance:.2f} "
                        f"(above {MONOLITHIC_CONDUCTANCE_THRESHOLD}); no natural split exists"
                    ),
                    boundary={
                        "modularity": graph_modularity,
                        "best_cut_conductance": best.conductance,
                        "best_cut_after_index": best.index,
                    },
                )
            )

    return findings


def _best_effort_modularity(graph: TaskGraph) -> float:
    """Modularity of the graph's own best clustering, independent of any
    chosen granularity preset — Louvain communities over the affinity graph,
    the same clustering ``partition.py`` itself would produce with no hubs
    or slices in play."""
    import networkx as nx

    from orchestrator.grouping.partition import LOUVAIN_SEED

    g = nx.Graph()
    g.add_nodes_from(graph.nodes)
    for (a, b), w in graph.affinity.items():
        if w > 0:
            g.add_edge(a, b, weight=w)
    if g.number_of_edges() == 0:
        return 0.0
    communities = nx.community.louvain_communities(g, weight="weight", seed=LOUVAIN_SEED)
    return nx.algorithms.community.quality.modularity(g, communities, weight="weight")
