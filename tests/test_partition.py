"""Tests for orchestrator/grouping/partition.py — the ported CoCoder partition core.

Scenario names track plan U1 (docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md):
hub isolation, sibling lifting, guarded merging, AE1/AE2, loud cycle detection, and the
R22 strategy seam.
"""

import ast
import statistics
from pathlib import Path

import pytest

from orchestrator.config import OrchestratorConfig
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
    repair_cycles,
    slice_atoms,
    split_over_budget,
)


def graph(nodes, affinity=None, dependencies=None, slices=None):
    return TaskGraph(
        nodes=frozenset(nodes),
        affinity={canonical_pair(*k): v for k, v in (affinity or {}).items()},
        dependencies=dict(dependencies or {}),
        metadata={node: {"slice": label} for node, label in (slices or {}).items()},
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

    def test_acyclic_merge_still_accepted(self):
        """Plan U4: the new cycle guard must not disable ordinary merging — an
        acyclic, in-budget, makespan-neutral chain still fully collapses."""
        g = graph("a b c".split(), dependencies={("a", "b"): 1.0, ("b", "c"): 1.0})
        merged = merge_small_groups(g, {"a": 0, "b": 1, "c": 2}, lambda n: 1.0, budget_cap=10.0)
        assert groups_of(merged) == {frozenset({"a", "b", "c"})}


class TestMergeAcyclicGuard:
    """Plan U4 (M2): merging an upstream hub's group with a downstream
    aggregator's group across an intermediate group inverts an edge in the
    quotient graph — the guard must refuse that specific merge."""

    def test_merge_creating_cross_group_cycle_is_rejected(self):
        """source-hub -> feature -> sink-aggregator, plus a direct
        source-hub -> sink-aggregator edge (the aggregator's real-world
        pattern of also depending on the hub directly). Merging hub and agg
        skips feature and would invert the feature->agg edge against the
        (now single-group) hub->feature edge — every candidate merge here is
        either over budget or would create that cycle, so all three groups
        stay distinct."""
        g = graph(
            "hub feature agg".split(),
            dependencies={
                ("hub", "feature"): 1.0,
                ("feature", "agg"): 1.0,
                ("hub", "agg"): 1.0,
            },
        )
        work = {"hub": 1.0, "feature": 10.0, "agg": 1.0}
        merged = merge_small_groups(
            g, {"hub": 0, "feature": 1, "agg": 2}, lambda n: work[n], budget_cap=5.0
        )
        assert groups_of(merged) == {
            frozenset({"hub"}),
            frozenset({"feature"}),
            frozenset({"agg"}),
        }


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

    def test_slice_keeps_its_members_together_when_group_splits(self):
        """Plan U3: cut candidates are between blocks — a slice atom or a lone
        node — never inside a slice. A two-task slice plus two loose tasks
        must split around the slice, not through it."""
        g = graph(
            "s1 s2 x y".split(),
            affinity={("s1", "s2"): 10.0, ("s2", "x"): 1.0, ("x", "y"): 10.0},
            slices={"s1": "sl", "s2": "sl"},
        )
        split = split_over_budget(
            g, {n: 0 for n in "s1 s2 x y".split()}, lambda n: 1.0, budget_cap=2.0
        )
        assert any({"s1", "s2"} <= members for members in groups_of(split))

    def test_single_block_over_budget_stays_whole_even_when_it_is_a_slice(self):
        """Generalizes the single-node passthrough (plan U3): a group made of
        exactly one block — even a multi-task slice atom — stays whole when
        over budget, since there is nothing to cut it against."""
        g = graph(
            "s1 s2".split(),
            affinity={("s1", "s2"): 5.0},
            slices={"s1": "sl", "s2": "sl"},
        )
        split = split_over_budget(g, {"s1": 0, "s2": 0}, lambda n: 100.0, budget_cap=1.0)
        assert groups_of(split) == {frozenset({"s1", "s2"})}


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

    def test_strategy_repairs_a_cyclic_grouping_instead_of_raising(self):
        """Plan U5: affinity pulls a+d and b+c together while dependencies
        cross (a→b, c→d) — unrelated across the groups, so the merge guard's
        chain_compatible check refuses to fold them together itself, leaving
        a 2-group cycle for repair_cycles to fix by merging the SCC. The
        strategy no longer raises for a cycle it can repair."""
        g = graph(
            "a b c d".split(),
            affinity={("a", "d"): 100.0, ("b", "c"): 100.0},
            dependencies={("a", "b"): 1.0, ("c", "d"): 1.0},
        )
        strategy = DefaultPartitionStrategy()
        partition = strategy.partition(g)
        assert groups_of(partition) == {frozenset("abcd")}
        assert strategy.last_stage == "repair"


class TestSccRepair:
    """Plan U5: repair_cycles merges every cyclic group-SCC deterministically,
    then re-splits the merged group back inside budget where an acyclic
    re-split exists."""

    def test_repair_merges_cyclic_scc_and_stays_acyclic(self):
        """A 3-group cycle (a->b->c->a) merges into one group; an unrelated
        downstream group (d) is untouched, and every task from the formerly
        cyclic groups appears exactly once in the result."""
        g = graph(
            "a b c d".split(),
            dependencies={("a", "b"): 1.0, ("b", "c"): 1.0, ("c", "a"): 1.0, ("c", "d"): 1.0},
        )
        partition = {"a": 0, "b": 1, "c": 2, "d": 3}
        repaired = repair_cycles(g, partition, lambda n: 1.0, budget_cap=None, flags=[])
        assert groups_of(repaired) == {frozenset({"a", "b", "c"}), frozenset({"d"})}
        build_group_dag(g, repaired)  # must not raise

    def test_repair_resplit_brings_merged_scc_within_cap(self):
        """A 2-group cycle whose merged work (4) exceeds the cap (2) is
        re-split into two chunks that each fit, and the quotient graph
        stays acyclic — the wave-ordered re-split (plan U5) is what makes
        this safe rather than reintroducing the same cycle."""
        g = graph(
            "a1 a2 b1 b2".split(),
            dependencies={("a1", "b1"): 1.0, ("b2", "a2"): 1.0},
        )
        partition = {"a1": 0, "a2": 0, "b1": 1, "b2": 1}
        flags: list[str] = []
        repaired = repair_cycles(g, partition, lambda n: 1.0, budget_cap=2.0, flags=flags)
        assert flags == []
        groups = groups_of(repaired)
        assert {n for members in groups for n in members} == {"a1", "a2", "b1", "b2"}
        assert all(len(members) <= 2 for members in groups)
        build_group_dag(g, repaired)  # must not raise

    def test_repair_flags_group_that_cannot_be_resplit_under_cap(self):
        """a1/a2 share a slice while a1 -> b -> a2: contracting the slice
        makes the slice block and b's block mutually dependent, so no
        acyclic re-split can separate them. The merged group is returned
        over budget with a flags[] entry naming it and the overshoot."""
        g = graph(
            "a1 a2 b".split(),
            dependencies={("a1", "b"): 1.0, ("b", "a2"): 1.0},
            slices={"a1": "s", "a2": "s"},
        )
        partition = {"a1": 0, "a2": 0, "b": 1}
        flags: list[str] = []
        repaired = repair_cycles(g, partition, lambda n: 1.0, budget_cap=2.0, flags=flags)
        assert groups_of(repaired) == {frozenset({"a1", "a2", "b"})}
        assert len(flags) == 1
        assert "a1" in flags[0] and "1" in flags[0]

    def test_repair_is_deterministic_across_runs(self):
        g = graph(
            "a1 a2 b1 b2".split(),
            dependencies={("a1", "b1"): 1.0, ("b2", "a2"): 1.0},
        )
        partition = {"a1": 0, "a2": 0, "b1": 1, "b2": 1}
        results = {
            tuple(sorted(repair_cycles(g, dict(partition), lambda n: 1.0, 2.0, []).items()))
            for _ in range(5)
        }
        assert len(results) == 1


class TestSliceContraction:
    """Task-map slice labels enter Louvain as must-link node contraction (plan U5)."""

    def test_slice_mates_co_group_even_when_affinity_pulls_apart(self):
        """The hard guarantee: contraction beats structure. Control first — the
        same graph without slice labels separates the pair."""
        shape = dict(
            affinity={("a1", "x"): 10.0, ("b1", "y"): 10.0, ("a1", "b1"): 0.1},
        )
        unsliced = DefaultPartitionStrategy().partition(graph("a1 b1 x y".split(), **shape))
        assert unsliced["a1"] != unsliced["b1"]
        sliced = DefaultPartitionStrategy().partition(
            graph("a1 b1 x y".split(), **shape, slices={"a1": "s", "b1": "s"})
        )
        assert sliced["a1"] == sliced["b1"]

    def test_slice_mates_with_no_edges_at_all_still_co_group(self):
        """Pure-greenfield regime: no structural or semantic edge survives, the
        slice label alone must keep the pair together."""
        partition = DefaultPartitionStrategy().partition(
            graph("a b z".split(), slices={"a": "s", "b": "s"})
        )
        assert partition["a"] == partition["b"]
        assert partition["z"] != partition["a"]

    def test_oversized_slice_stays_whole_instead_of_splitting(self):
        """Plan U3: a slice is one indivisible block for the splitter — the
        must-link no longer yields to the token cap the way it did pre-U3.
        An oversized slice passes through whole; reacting to the overshoot
        is U6's overflow gate, not the splitter."""
        g = graph(
            "a1 a2 a3".split(),
            affinity={("a1", "a2"): 5.0, ("a2", "a3"): 1.0},
            slices={"a1": "s", "a2": "s", "a3": "s"},
        )
        partition = DefaultPartitionStrategy(work_fn=lambda n: 3.0, budget_cap=7.0).partition(g)
        assert groups_of(partition) == {frozenset({"a1", "a2", "a3"})}

    def test_hub_role_member_joins_its_slice(self):
        """A declared slice outranks an inferred hub role (plan U2): the planner
        bound these tasks explicitly, so a hub-classified member now contracts
        with its slice-mates instead of being isolated away from them. This
        corrects the pre-U2 expectation, which had the hub excluded."""
        g = graph(
            "hub a b c".split(),
            dependencies={("hub", n): 1.0 for n in "abc"},
            slices={"hub": "s", "a": "s", "b": "s"},
        )
        partition = DefaultPartitionStrategy().partition(g)
        assert partition["hub"] == partition["a"] == partition["b"]

    def test_hub_isolation_still_applies_to_a_slice_less_task(self):
        """Slices override hub classification only for their own declared members
        (plan U2 decision); a hub carrying no slice label is isolated exactly as
        before, even with an unrelated slice present elsewhere in the graph."""
        g = graph(
            "hub a b c d".split(),
            affinity={("hub", n): 1.0 for n in "abcd"} | {("a", "b"): 5.0, ("c", "d"): 5.0},
            dependencies={("hub", n): 1.0 for n in "abcd"},
            slices={"a": "sl", "b": "sl"},
        )
        partition = DefaultPartitionStrategy(budget_cap=2.0).partition(g)
        assert frozenset({"hub"}) in groups_of(partition)
        assert partition["a"] == partition["b"]

    def test_slice_atoms_retains_a_hub_classified_member(self):
        """slice_atoms no longer filters by role (plan U2, mechanism M1): every
        declared member joins its atom even when detect_hub_roles classifies it
        as a hub."""
        g = graph(
            "agg s2 x y z".split(),
            dependencies={(n, "agg"): 1.0 for n in "xyz"},
            slices={"agg": "sl", "s2": "sl"},
        )
        roles = detect_hub_roles(g)
        assert roles["agg"] == "aggregator_hub"
        assert slice_atoms(g, roles) == {"sl": ["agg", "s2"]}

    def test_slice_survives_when_every_member_is_hub_classified(self):
        """The Observatory failure mode (plan U2): a slice whose members are all
        hub-classified used to vanish entirely (no member was 'core', so the
        label never reached the atoms dict at all). Now the whole slice
        contracts and every member lands in the same partitioned group."""
        g = graph(
            "p1 p2 x1 x2 x3".split(),
            dependencies={(x, "p1"): 1.0 for x in "x1 x2 x3".split()}
            | {(x, "p2"): 1.0 for x in "x1 x2 x3".split()},
            slices={"p1": "sl", "p2": "sl"},
        )
        roles = detect_hub_roles(g)
        assert roles["p1"] == "aggregator_hub"
        assert roles["p2"] == "aggregator_hub"
        partition = DefaultPartitionStrategy().partition(g)
        assert partition["p1"] == partition["p2"]

    def test_contraction_is_deterministic_across_runs(self):
        g = graph(
            "a1 a2 b1 b2 x".split(),
            affinity={("a1", "x"): 3.0, ("b1", "x"): 3.0, ("a1", "b1"): 0.5},
            dependencies={("a1", "b1"): 1.0},
            slices={"a1": "s1", "a2": "s1", "b1": "s2", "b2": "s2"},
        )
        results = {tuple(sorted(DefaultPartitionStrategy().partition(g).items())) for _ in range(5)}
        assert len(results) == 1

    def test_slice_induced_group_cycle_is_repaired_by_merging_the_scc(self):
        """Plan U5: contraction can close a cycle absent at task level — slice
        s1 feeds s2 through one task while s2 feeds s1 through another. The
        chain guard refuses to merge them away (unrelated node pairs across
        the two slices), leaving a 2-group cycle that repair_cycles now
        merges into one group instead of raising."""
        g = graph(
            "a1 a2 b1 b2".split(),
            dependencies={("a1", "b1"): 1.0, ("b2", "a2"): 1.0},
            slices={"a1": "s1", "a2": "s1", "b1": "s2", "b2": "s2"},
        )
        strategy = DefaultPartitionStrategy()
        partition = strategy.partition(g)
        assert groups_of(partition) == {frozenset({"a1", "a2", "b1", "b2"})}
        assert strategy.last_stage == "repair"


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


class TestStageAttribution:
    """R18: DefaultPartitionStrategy.last_stage names which internal stage
    (contraction / louvain / lift / split / merge) last changed group membership."""

    def test_last_stage_is_split_when_budget_forces_a_cut(self):
        """A slice plus a loose task survive contraction/lift clustered
        together, then split_over_budget cuts the loose task away — the
        slice's two members stay together (plan U3), and last_stage must
        name the split, not the earlier stages."""
        g = graph(
            "a1 a2 x".split(),
            affinity={("a1", "a2"): 5.0, ("a2", "x"): 1.0},
            slices={"a1": "s", "a2": "s"},
        )
        strategy = DefaultPartitionStrategy(work_fn=lambda n: 3.0, budget_cap=7.0)
        partition = strategy.partition(g)
        assert groups_of(partition) == {frozenset({"a1", "a2"}), frozenset({"x"})}
        assert strategy.last_stage == "split"

    def test_last_stage_is_merge_when_a_dependent_chain_collapses(self):
        """No budget cap (split never runs); merge_small_groups is the only stage
        that changes anything, so it must be named last."""
        g = graph("a b c".split(), dependencies={("a", "b"): 1.0, ("b", "c"): 1.0})
        strategy = DefaultPartitionStrategy()
        partition = strategy.partition(g)
        assert groups_of(partition) == {frozenset({"a", "b", "c"})}
        assert strategy.last_stage == "merge"

    def test_last_stage_is_none_for_empty_graph(self):
        strategy = DefaultPartitionStrategy()
        strategy.partition(graph([]))
        assert strategy.last_stage is None


def _granularity_ladder_graph():
    """Three branches of uneven length converging on one leaf — the same shape
    as tests/fixtures/grouping/granularity-ladder.md, at the partition level
    directly (no estimator/mapper involved). Every cross-branch pair fails
    chain_compatible (the branches are genuinely parallel), so `independent`
    keeps all three apart; `balanced` relaxes chain_compatible and accepts one
    cross-branch merge that does not regress the simulated makespan, rejecting
    a second on makespan alone; `monolithic` also drops the makespan check."""
    return graph(
        "root alpha1 alpha2 beta1 beta2 beta3 gamma1 leaf".split(),
        dependencies={
            ("root", "alpha1"): 1.0,
            ("alpha1", "alpha2"): 1.0,
            ("root", "beta1"): 1.0,
            ("beta1", "beta2"): 1.0,
            ("beta2", "beta3"): 1.0,
            ("root", "gamma1"): 1.0,
            ("alpha2", "leaf"): 1.0,
            ("beta3", "leaf"): 1.0,
            ("gamma1", "leaf"): 1.0,
        },
    )


class TestGranularityDial:
    """Plan U4: --granularity relaxes merge_small_groups' two guards in order —
    `balanced` drops chain_compatible (keeping the makespan check as the sole
    acceptance test), `monolithic` also drops the makespan check."""

    def test_independent_is_the_default_and_equals_todays_behaviour(self):
        g = _granularity_ladder_graph()
        default = DefaultPartitionStrategy().partition(g)
        explicit = DefaultPartitionStrategy(granularity="independent").partition(g)
        assert groups_of(default) == groups_of(explicit)
        assert len(groups_of(default)) == 3

    def test_balanced_strictly_reduces_group_count(self):
        g = _granularity_ladder_graph()
        independent = DefaultPartitionStrategy(granularity="independent").partition(g)
        balanced = DefaultPartitionStrategy(granularity="balanced").partition(g)
        assert len(groups_of(balanced)) < len(groups_of(independent))
        assert len(groups_of(balanced)) == 2

    def test_monolithic_is_no_more_groups_than_balanced(self):
        g = _granularity_ladder_graph()
        balanced = DefaultPartitionStrategy(granularity="balanced").partition(g)
        monolithic = DefaultPartitionStrategy(granularity="monolithic").partition(g)
        assert len(groups_of(monolithic)) <= len(groups_of(balanced))
        assert groups_of(monolithic) == {frozenset(g.nodes)}

    def test_balanced_relaxes_chain_compatible_via_merge_reject_reasons(self):
        """Direct evidence at the merge_small_groups level: independent rejects
        the cross-branch candidate as not_chain_compatible; balanced accepts one
        such merge and then rejects a further one specifically on
        makespan_regression — proving the makespan guard, not chain_compatible,
        is what still gates monolithic-only merges at the balanced level."""
        g = _granularity_ladder_graph()
        # Pre-cluster into the three branch groups DefaultPartitionStrategy would
        # form before its own merge stage, so merge_small_groups sees the same
        # input either way.
        partition = {
            "root": 1,
            "beta1": 1,
            "beta2": 1,
            "beta3": 0,
            "alpha1": 0,
            "alpha2": 0,
            "leaf": 0,
            "gamma1": 2,
        }
        work = lambda n: 1.0  # noqa: E731
        independent_recorder = _RecordingRecorder()
        merge_small_groups(
            g, dict(partition), work, None, recorder=independent_recorder, granularity="independent"
        )
        assert any(
            not m["accepted"] and m["reason"] == "not_chain_compatible"
            for m in independent_recorder.merges
        )
        balanced_recorder = _RecordingRecorder()
        merge_small_groups(
            g, dict(partition), work, None, recorder=balanced_recorder, granularity="balanced"
        )
        assert not any(m["reason"] == "not_chain_compatible" for m in balanced_recorder.merges)
        assert any(
            not m["accepted"] and m["reason"] == "makespan_regression"
            for m in balanced_recorder.merges
        )

    def test_granularity_is_deterministic_across_runs(self):
        g = _granularity_ladder_graph()
        for gran in ("independent", "balanced", "monolithic"):
            results = {
                tuple(sorted(DefaultPartitionStrategy(granularity=gran).partition(g).items()))
                for _ in range(5)
            }
            assert len(results) == 1

    def test_budget_cap_and_slices_stay_hard_at_every_level(self):
        """Slice must-link, the budget cap, and acyclicity are never relaxed by
        the dial — only chain_compatible and the makespan check are."""
        g = graph(
            "a1 a2 x".split(),
            affinity={("a1", "a2"): 10.0},
            dependencies={("a2", "x"): 1.0},
            slices={"a1": "s", "a2": "s"},
        )
        for gran in ("independent", "balanced", "monolithic"):
            strategy = DefaultPartitionStrategy(
                work_fn=lambda n: 3.0, budget_cap=7.0, granularity=gran
            )
            partition = strategy.partition(g)
            slice_members = {"a1", "a2"}
            by_gid = {}
            for node, gid in partition.items():
                by_gid.setdefault(gid, set()).add(node)
            assert any(slice_members <= members for members in by_gid.values())
            for members in by_gid.values():
                assert sum(3.0 for _ in members) <= 7.0 or slice_members <= members


class _RecordingRecorder:
    """Minimal PartitionRecorder capturing only merge candidates, as plain dicts."""

    def __init__(self):
        self.merges = []

    def record_stage(self, stage, partition):
        pass

    def record_hub_role(self, *a, **k):
        pass

    def record_louvain(self, *a, **k):
        pass

    def record_split(self, *a, **k):
        pass

    def record_merge_candidate(
        self, round_, source, target, accepted, reason, merged_work, edge_weight
    ):
        self.merges.append({"accepted": accepted, "reason": reason})

    def record_repair(self, *a, **k):
        pass


class TestFillPenalty:
    """Plan U12 (R19b): the merge key's fill/balance term.

    ``target_fill_ratio=0.0`` reproduces the pre-U12 key byte-for-byte: the
    new term becomes ``abs(merged_work - 0.0) == merged_work``, sitting right
    next to the existing ``merged_work`` field with an identical value, so it
    can never change the total order — that is how these tests get an "old
    key" oracle without duplicating the merge loop.
    """

    def _pathology_graph(self):
        """Two hubs, each independently a valid dependency for a pool of
        eight small (0.3) and one large (2.0) shared task — modelling a
        greedy first-fit pathology: whichever hub the loop happens to favour
        first can keep absorbing the cheapest remaining task every round
        (old key: plain ``merged_work`` ascending always prefers the
        numerically smallest candidate), landing two same-sized ~2.0 lumps
        while its sibling hub never receives a single merge. ``granularity=
        "balanced"`` is required for more than one small task to ever join
        the same hub (the default ``independent`` guard treats same-parent
        siblings as parallel and refuses to merge more than one in)."""
        nodes = ["hub_a", "hub_b"] + [f"s{i}" for i in range(1, 9)] + ["big"]
        deps = {}
        for n in [f"s{i}" for i in range(1, 9)] + ["big"]:
            deps[("hub_a", n)] = 1.0
            deps[("hub_b", n)] = 1.0
        work = {"hub_a": 0.2, "hub_b": 0.2, "big": 2.0}
        work.update({f"s{i}": 0.3 for i in range(1, 9)})
        g = graph(nodes, dependencies=deps)
        return g, work

    def test_fill_term_lowers_group_size_variance_without_breaking_invariants(self):
        g, work = self._pathology_graph()
        partition = {n: i for i, n in enumerate(sorted(work))}
        cap = 2.8

        def sizes_of(merged):
            groups: dict[int, list[str]] = {}
            for node, gid in merged.items():
                groups.setdefault(gid, []).append(node)
            return groups, sorted(sum(work[n] for n in members) for members in groups.values())

        old = merge_small_groups(
            g,
            dict(partition),
            lambda n: work[n],
            budget_cap=cap,
            granularity="balanced",
            target_fill_ratio=0.0,
        )
        new = merge_small_groups(
            g,
            dict(partition),
            lambda n: work[n],
            budget_cap=cap,
            granularity="balanced",
            target_fill_ratio=0.75,
        )
        old_groups, old_sizes = sizes_of(old)
        new_groups, new_sizes = sizes_of(new)

        # Old key: greedy-cheapest-first lets hub_a's group absorb tasks one
        # at a time until it happens to land right next to "big" in size —
        # two ~2.0 lumps (one a single "big" task, one a pile of "small"
        # ones) instead of one well-filled group and evenly-sized stragglers.
        assert old_sizes == pytest.approx([0.2, 0.3, 0.3, 2.0, 2.0])
        assert new_sizes == pytest.approx([0.2, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 2.2])

        assert statistics.pvariance(new_sizes) < statistics.pvariance(old_sizes)
        assert all(size <= cap for size in new_sizes)
        assert build_group_dag(g, new) is not None  # raises GroupCycleError if not acyclic

    def test_fill_term_is_deterministic_across_runs(self):
        g, work = self._pathology_graph()
        partition = {n: i for i, n in enumerate(sorted(work))}
        results = {
            tuple(
                sorted(
                    merge_small_groups(
                        g,
                        dict(partition),
                        lambda n: work[n],
                        budget_cap=2.8,
                        granularity="balanced",
                        target_fill_ratio=0.75,
                    ).items()
                )
            )
            for _ in range(5)
        }
        assert len(results) == 1

    def test_no_cap_leaves_the_term_neutral(self):
        """No budget cap means no band to aim for (docs in partition.py): the
        term must not change anything when the cap is absent."""
        g, work = self._pathology_graph()
        partition = {n: i for i, n in enumerate(sorted(work))}
        old = merge_small_groups(
            g,
            dict(partition),
            lambda n: work[n],
            budget_cap=None,
            granularity="balanced",
            target_fill_ratio=0.0,
        )
        new = merge_small_groups(
            g,
            dict(partition),
            lambda n: work[n],
            budget_cap=None,
            granularity="balanced",
            target_fill_ratio=0.75,
        )
        assert old == new

    def test_target_fill_ratio_is_configurable(self):
        assert OrchestratorConfig().partition.target_fill_ratio == pytest.approx(0.75)
        assert OrchestratorConfig.model_validate(
            {"partition": {"target_fill_ratio": 0.5}}
        ).partition.target_fill_ratio == pytest.approx(0.5)
