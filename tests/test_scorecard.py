"""Tests for orchestrator/grouping/scorecard.py (plan U5): the quality
scorecard computed once per partition and printed/recorded verbatim.
"""

from __future__ import annotations

from orchestrator.grouping.partition import TaskGraph, canonical_pair
from orchestrator.grouping.scorecard import compute_scorecard


def graph(nodes, affinity=None, dependencies=None, slices=None):
    return TaskGraph(
        nodes=frozenset(nodes),
        affinity={canonical_pair(*k): v for k, v in (affinity or {}).items()},
        dependencies=dict(dependencies or {}),
        metadata={node: {"slice": label} for node, label in (slices or {}).items()},
    )


class TestGroupCountAndCrossGroupEdges:
    def test_group_count_matches_partition(self):
        g = graph("a b c".split())
        sc = compute_scorecard(
            g, {"a": 0, "b": 0, "c": 1}, {}, budget_cap=10.0, dag={}, slice_atoms={}
        )
        assert sc.group_count == 2

    def test_cross_group_edges_counts_only_crossing_dependencies(self):
        g = graph("a b c".split(), dependencies={("a", "b"): 1.0, ("b", "c"): 1.0})
        # a,b in group 0 (edge a->b does NOT cross); b->c crosses (b:0, c:1)
        sc = compute_scorecard(
            g, {"a": 0, "b": 0, "c": 1}, {}, budget_cap=10.0, dag={0: {1}}, slice_atoms={}
        )
        assert sc.cross_group_edges == 1


class TestWorkFractions:
    def test_min_mean_max_of_cap(self):
        g = graph("a b c d".split())
        partition = {"a": 0, "b": 0, "c": 1, "d": 1}
        node_work = {"a": 1.0, "b": 1.0, "c": 5.0, "d": 5.0}
        sc = compute_scorecard(g, partition, node_work, budget_cap=10.0, dag={}, slice_atoms={})
        # group0 work=2 -> 0.2, group1 work=10 -> 1.0
        assert sc.work_fraction_min == 0.2
        assert sc.work_fraction_max == 1.0
        assert abs(sc.work_fraction_mean - 0.6) < 1e-9

    def test_zero_budget_cap_yields_zero_fractions_not_a_crash(self):
        g = graph("a".split())
        sc = compute_scorecard(g, {"a": 0}, {"a": 5.0}, budget_cap=0.0, dag={}, slice_atoms={})
        assert sc.work_fraction_min == 0.0
        assert sc.work_fraction_max == 0.0


class TestCriticalPathLength:
    def test_single_group_chain_is_length_one(self):
        g = graph("a".split())
        sc = compute_scorecard(g, {"a": 0}, {}, budget_cap=10.0, dag={}, slice_atoms={})
        assert sc.critical_path_length == 1

    def test_longest_chain_in_the_group_dag(self):
        # 0 -> 1 -> 2, plus an unrelated isolated group 3: longest chain is 3.
        g = graph("nodes".split())
        dag = {0: {1}, 1: {2}}
        sc = compute_scorecard(g, {"x": 0}, {}, budget_cap=10.0, dag=dag, slice_atoms={})
        # gids referenced by the scorecard come from the partition; extend it
        # to cover all four groups referenced by the dag.
        partition = {"a": 0, "b": 1, "c": 2, "d": 3}
        sc = compute_scorecard(g, partition, {}, budget_cap=10.0, dag=dag, slice_atoms={})
        assert sc.critical_path_length == 3


class TestModularity:
    def test_no_affinity_edges_yields_zero(self):
        g = graph("a b".split())
        sc = compute_scorecard(g, {"a": 0, "b": 1}, {}, budget_cap=10.0, dag={}, slice_atoms={})
        assert sc.modularity == 0.0

    def test_perfect_community_split_yields_positive_modularity(self):
        # Two tightly-connected pairs, no cross-pair edges: a textbook positive-
        # modularity partition that matches the affinity structure exactly.
        g = graph(
            "a1 a2 b1 b2".split(),
            affinity={("a1", "a2"): 10.0, ("b1", "b2"): 10.0},
        )
        sc = compute_scorecard(
            g, {"a1": 0, "a2": 0, "b1": 1, "b2": 1}, {}, budget_cap=10.0, dag={}, slice_atoms={}
        )
        assert sc.modularity > 0.0

    def test_splitting_a_cohesive_pair_reduces_modularity(self):
        g = graph(
            "a1 a2 b1 b2".split(),
            affinity={("a1", "a2"): 10.0, ("b1", "b2"): 10.0},
        )
        together = compute_scorecard(
            g, {"a1": 0, "a2": 0, "b1": 1, "b2": 1}, {}, budget_cap=10.0, dag={}, slice_atoms={}
        )
        split_up = compute_scorecard(
            g, {"a1": 0, "a2": 1, "b1": 2, "b2": 3}, {}, budget_cap=10.0, dag={}, slice_atoms={}
        )
        assert split_up.modularity < together.modularity


class TestSliceIntegrity:
    def test_intact_slice_passes(self):
        g = graph("a1 a2".split(), slices={"a1": "s", "a2": "s"})
        sc = compute_scorecard(
            g, {"a1": 0, "a2": 0}, {}, budget_cap=10.0, dag={}, slice_atoms={"s": ["a1", "a2"]}
        )
        assert sc.slice_integrity_ok is True

    def test_split_slice_fails(self):
        g = graph("a1 a2".split(), slices={"a1": "s", "a2": "s"})
        sc = compute_scorecard(
            g, {"a1": 0, "a2": 1}, {}, budget_cap=10.0, dag={}, slice_atoms={"s": ["a1", "a2"]}
        )
        assert sc.slice_integrity_ok is False

    def test_no_slices_declared_passes_trivially(self):
        g = graph("a".split())
        sc = compute_scorecard(g, {"a": 0}, {}, budget_cap=10.0, dag={}, slice_atoms={})
        assert sc.slice_integrity_ok is True
