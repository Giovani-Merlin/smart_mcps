"""Tests for orchestrator/grouping/partition.py — the ported CoCoder partition core.

Scenario names track plan U1 (docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md):
hub isolation, sibling lifting, guarded merging, AE1/AE2, loud cycle detection, and the
R22 strategy seam.
"""

import ast
from pathlib import Path

import pytest

from orchestrator.grouping.partition import (
    DefaultPartitionStrategy,
    GroupCycleError,
    SingleGroupStrategy,
    TaskGraph,
    build_group_dag,
    canonical_pair,
    detect_hub_roles,
    lift_independent,
    merge_small_groups,
    split_over_budget,
)


def graph(nodes, affinity=None, dependencies=None):
    return TaskGraph(
        nodes=frozenset(nodes),
        affinity={canonical_pair(*k): v for k, v in (affinity or {}).items()},
        dependencies=dict(dependencies or {}),
    )


def groups_of(partition):
    """Set of frozensets — group membership independent of gid numbering."""
    by_gid = {}
    for node, gid in partition.items():
        by_gid.setdefault(gid, set()).add(node)
    return {frozenset(members) for members in by_gid.values()}


class TestGraphShapes:
    def test_rejects_non_canonical_affinity_key(self):
        with pytest.raises(ValueError, match="not canonical"):
            TaskGraph(nodes=frozenset({"a", "b"}), affinity={("b", "a"): 1.0})

    def test_rejects_edges_to_unknown_nodes(self):
        with pytest.raises(ValueError, match="unknown nodes"):
            TaskGraph(nodes=frozenset({"a"}), dependencies={("a", "ghost"): 1.0})

    def test_rejects_self_loops(self):
        with pytest.raises(ValueError, match="self-loop"):
            TaskGraph(nodes=frozenset({"a"}), dependencies={("a", "a"): 1.0})


class TestHubRoles:
    def test_widely_depended_upon_node_is_utility_hub(self):
        """Corrected naming: many dependents → utility_hub (CoCoder misnames it in_hub)."""
        g = graph(
            "hub a b c d".split(),
            dependencies={("hub", n): 1.0 for n in "abcd"},
        )
        roles = detect_hub_roles(g)
        assert roles["hub"] == "utility_hub"
        assert all(roles[n] == "core" for n in "abcd")

    def test_node_depending_on_many_is_aggregator_hub(self):
        g = graph(
            "agg a b c d".split(),
            dependencies={(n, "agg"): 1.0 for n in "abcd"},
        )
        roles = detect_hub_roles(g)
        assert roles["agg"] == "aggregator_hub"

    def test_aggregator_classification_wins_when_both(self):
        """CoCoder checks the aggregator condition first — behavior ported."""
        g = graph(
            "both a b c d e".split(),
            dependencies={(n, "both"): 1.0 for n in "abc"} | {("both", n): 1.0 for n in "de"},
        )
        assert detect_hub_roles(g)["both"] == "aggregator_hub"

    def test_hub_is_isolated_as_its_own_group(self):
        """Plan U1 scenario: hub touched by most tasks is isolated and reattached alone."""
        g = graph(
            "hub a b c d".split(),
            affinity={("hub", n): 1.0 for n in "abcd"} | {("a", "b"): 5.0, ("c", "d"): 5.0},
            dependencies={("hub", n): 1.0 for n in "abcd"},
        )
        partition = DefaultPartitionStrategy(budget_cap=2.0).partition(g)
        assert frozenset({"hub"}) in groups_of(partition)


class TestLiftIndependent:
    def test_siblings_sharing_only_a_hub_are_lifted_apart(self):
        """Plan U1 scenario: siblings that only depend on an internal hub split off."""
        g = graph(
            "hub s1 s2".split(),
            dependencies={("hub", "s1"): 1.0, ("hub", "s2"): 1.0},
        )
        lifted = lift_independent(g, {"hub": 0, "s1": 0, "s2": 0})
        assert groups_of(lifted) == {
            frozenset({"hub"}),
            frozenset({"s1"}),
            frozenset({"s2"}),
        }

    def test_hubless_group_splits_by_connected_components(self):
        g = graph(
            "a b c d".split(),
            dependencies={("a", "b"): 1.0, ("c", "d"): 1.0},
        )
        lifted = lift_independent(g, {n: 0 for n in "abcd"})
        assert groups_of(lifted) == {frozenset({"a", "b"}), frozenset({"c", "d"})}

    def test_small_groups_pass_through(self):
        g = graph("a b".split())
        assert groups_of(lift_independent(g, {"a": 0, "b": 0})) == {frozenset({"a", "b"})}


class TestMerge:
    def test_merge_requires_dependency_direction(self):
        """Plan U1 scenario: no dependency between groups → no merge, however small."""
        g = graph("a b".split(), affinity={("a", "b"): 10.0})
        merged = merge_small_groups(g, {"a": 0, "b": 1}, lambda n: 1.0, budget_cap=100.0)
        assert groups_of(merged) == {frozenset({"a"}), frozenset({"b"})}

    def test_merge_collapses_dependent_chain_under_budget(self):
        g = graph("a b c".split(), dependencies={("a", "b"): 1.0, ("b", "c"): 1.0})
        merged = merge_small_groups(g, {"a": 0, "b": 1, "c": 2}, lambda n: 1.0, budget_cap=10.0)
        assert groups_of(merged) == {frozenset({"a", "b", "c"})}

    def test_merge_respects_budget_cap(self):
        g = graph("a b".split(), dependencies={("a", "b"): 1.0})
        merged = merge_small_groups(g, {"a": 0, "b": 1}, lambda n: 3.0, budget_cap=5.0)
        assert groups_of(merged) == {frozenset({"a"}), frozenset({"b"})}

    def test_merge_never_serializes_parallel_branches(self):
        """Plan U1 scenario: a merge that would regress the simulated makespan is refused.

        Two parallel branches off a shared root: serializing b1 and b2 into one
        group would push the zero-communication makespan from 6 to 11, so no merge
        may ever combine them (the dependency-direction and chain guards refuse it).
        """
        g = graph(
            "root b1 b2".split(),
            dependencies={("root", "b1"): 1.0, ("root", "b2"): 1.0},
        )
        merged = merge_small_groups(
            g, {"root": 0, "b1": 1, "b2": 2}, lambda n: 5.0 if n != "root" else 1.0, None
        )
        for members in groups_of(merged):
            assert not {"b1", "b2"} <= members


class TestSplitOverBudget:
    def test_ae2_split_at_lowest_affinity_boundary(self):
        """AE2: an over-budget group splits at its weakest edge; every part fits."""
        g = graph(
            "a b c d".split(),
            affinity={("a", "b"): 10.0, ("b", "c"): 1.0, ("c", "d"): 10.0},
        )
        split = split_over_budget(g, {n: 0 for n in "abcd"}, lambda n: 1.0, budget_cap=2.0)
        assert groups_of(split) == {frozenset({"a", "b"}), frozenset({"c", "d"})}

    def test_single_over_budget_node_stays_singleton(self):
        g = graph(["big"])
        split = split_over_budget(g, {"big": 0}, lambda n: 100.0, budget_cap=1.0)
        assert groups_of(split) == {frozenset({"big"})}

    def test_fully_cohesive_over_budget_group_still_splits(self):
        """Equal weights everywhere: the group must still be forced under budget."""
        g = graph(
            "a b c".split(),
            affinity={("a", "b"): 1.0, ("b", "c"): 1.0, ("a", "c"): 1.0},
        )
        split = split_over_budget(g, {n: 0 for n in "abc"}, lambda n: 1.0, budget_cap=2.0)
        assert all(len(members) <= 2 for members in groups_of(split))


class TestGroupDag:
    def test_cycle_raises_naming_groups_and_edges(self):
        """Plan U1 scenario: a dependency cycle fails loudly with the members named."""
        g = graph(
            "a b c d".split(),
            dependencies={("a", "b"): 1.0, ("c", "d"): 1.0},
        )
        partition = {"a": 0, "d": 0, "b": 1, "c": 1}
        with pytest.raises(GroupCycleError) as exc:
            build_group_dag(g, partition)
        assert exc.value.cycle_groups == [0, 1]
        assert ("a", "b") in exc.value.offending_edges
        assert ("c", "d") in exc.value.offending_edges

    def test_acyclic_dag_returned(self):
        g = graph("a b c".split(), dependencies={("a", "b"): 1.0, ("b", "c"): 1.0})
        dag = build_group_dag(g, {"a": 0, "b": 1, "c": 2})
        assert dag == {0: {1}, 1: {2}}

    def test_strategy_fails_loudly_on_cyclic_grouping(self):
        """Affinity pulls a+d and b+c together while dependencies cross: a→b, c→d."""
        g = graph(
            "a b c d".split(),
            affinity={("a", "d"): 100.0, ("b", "c"): 100.0},
            dependencies={("a", "b"): 1.0, ("c", "d"): 1.0},
        )
        with pytest.raises(GroupCycleError):
            DefaultPartitionStrategy().partition(g)


class TestDefaultStrategyEndToEnd:
    def test_ae1_cohesive_under_budget_yields_one_group(self):
        """AE1: a fully cohesive graph under budget is exactly one group."""
        g = graph(
            "a b c d".split(),
            affinity={
                ("a", "b"): 5.0,
                ("b", "c"): 5.0,
                ("c", "d"): 5.0,
                ("a", "c"): 5.0,
                ("b", "d"): 5.0,
                ("a", "d"): 5.0,
            },
            dependencies={("a", "b"): 1.0, ("b", "c"): 1.0, ("c", "d"): 1.0},
        )
        partition = DefaultPartitionStrategy(budget_cap=10.0).partition(g)
        assert groups_of(partition) == {frozenset("abcd")}

    def test_ae2_over_budget_splits_and_every_group_fits(self):
        """AE2 end to end: budget forces a split at the lowest-affinity boundary."""
        g = graph(
            "a b c d".split(),
            affinity={("a", "b"): 10.0, ("b", "c"): 1.0, ("c", "d"): 10.0},
        )
        partition = DefaultPartitionStrategy(budget_cap=2.0).partition(g)
        assert groups_of(partition) == {frozenset({"a", "b"}), frozenset({"c", "d"})}

    def test_empty_graph(self):
        assert DefaultPartitionStrategy().partition(graph([])) == {}

    def test_deterministic_across_runs(self):
        g = graph(
            "a b c d e f".split(),
            affinity={
                ("a", "b"): 3.0,
                ("b", "c"): 2.0,
                ("d", "e"): 3.0,
                ("e", "f"): 2.0,
                ("c", "d"): 0.5,
            },
            dependencies={("a", "b"): 1.0, ("d", "e"): 1.0},
        )
        results = {tuple(sorted(DefaultPartitionStrategy().partition(g).items())) for _ in range(5)}
        assert len(results) == 1


class TestStrategySeam:
    def test_single_group_passthrough_plugs_in(self):
        """R22: a second trivial strategy works through the same protocol."""
        g = graph("a b c".split(), dependencies={("a", "b"): 1.0})
        for strategy in (SingleGroupStrategy(), DefaultPartitionStrategy()):
            partition = strategy.partition(g)
            assert set(partition) == {"a", "b", "c"}
        assert groups_of(SingleGroupStrategy().partition(g)) == {frozenset("abc")}

    def test_module_imports_stay_pure(self):
        """Verification: the module imports without networkx-unrelated dependencies.

        networkx itself is imported lazily inside the Louvain step, so module-level
        imports must be stdlib only.
        """
        source = Path("orchestrator/grouping/partition.py").read_text()
        allowed = {"collections", "collections.abc", "dataclasses", "typing", "__future__"}
        # Only top-level statements: lazy in-function imports (networkx) are the
        # sanctioned escape hatch and stay out of module import time.
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed for alias in node.names), ast.dump(node)
            elif isinstance(node, ast.ImportFrom):
                assert node.module in allowed, node.module
