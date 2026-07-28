"""U8: deterministic fixture plans for the grouping register's shapes, asserted
through the partition-only path (compute_partition) as a baseline of *current*
partitioner behaviour — including which shapes cycle today
(docs/orchestrators_improvements.md). None of this is desired behaviour; it is
what the next grouper-quality session starts from. Zero LLM calls, zero real
codegraph: the stub runner below only ever answers `codegraph files`.
"""

import json
from pathlib import Path

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.pipeline import compute_partition

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "grouping"


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("fixture tests must stay zero-token")


def stub_codegraph_runner(args):
    """Zero real codegraph: every fixture plan declares no `symbols`, so the
    only calls the pipeline ever issues are `codegraph sync` (R13) and
    `codegraph files` for the base context. Anything else means a fixture
    accidentally started using symbols."""
    if args[0] == "sync":
        return ""
    if args[0] == "files":
        return "stub repo (fixture test — no queries expected)\n"
    raise AssertionError(f"unexpected codegraph call in a fixture test: {args}")


def make_repo(tmp_path, fixture_name, real_files=None):
    """A repo backed by one fixture plan. ``real_files`` (brownfield variants
    only) writes real file content so the mapped paths are not prospective."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for rel, content in (real_files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    plan = repo / "plan.md"
    plan.write_text((FIXTURES_DIR / f"{fixture_name}.md").read_text())
    return repo, plan


def client_for(repo):
    return CodegraphClient(repo_root=repo, runner=stub_codegraph_runner)


def members_by_group(partition):
    by_gid: dict[int, set[str]] = {}
    for node, gid in partition.items():
        by_gid.setdefault(gid, set()).add(node)
    return by_gid


def groups_of(partition):
    """Group membership independent of gid numbering."""
    return {frozenset(members) for members in members_by_group(partition).values()}


def serialize_partition(partition) -> str:
    """Canonical bytes for the byte-stability property test."""
    return json.dumps(dict(sorted(partition.items())), sort_keys=True)


BROWNFIELD_CROSS_STACK_FILES = {
    "app/main.py": "def main():\n    pass\n" * 5,
    "app/auth.py": "def auth():\n    pass\n" * 5,
    "web/auth.tsx": "export const Auth = () => null;\n" * 5,
    "app/items.py": "def items():\n    pass\n" * 5,
    "web/items.tsx": "export const Items = () => null;\n" * 5,
    "app/profile.py": "def profile():\n    pass\n" * 5,
    "web/profile.tsx": "export const Profile = () => null;\n" * 5,
    "docs/usage.md": "# usage\n" * 5,
    "tests/e2e.py": "def test_e2e():\n    pass\n" * 5,
}


def _assert_cross_stack_slices_intact(partition):
    by_gid = members_by_group(partition)
    for members in (
        {"auth-api", "auth-ui"},
        {"items-api", "items-ui"},
        {"profile-api", "profile-ui"},
    ):
        assert any(members <= group for group in by_gid.values()), members


class TestGreenfieldCrossStack:
    """Plan U4 (M2): the acyclic merge guard now refuses to fold the
    aggregator (verify) and the utility hub (scaffold) into one group across
    an intact slice — the scaffold->items and items->verify edges that used
    to cross the same group boundary in opposite directions can no longer
    both exist. This shape partitions cleanly with every slice intact."""

    def test_partitions_without_cycle_and_slices_intact(self, tmp_path):
        repo, plan = make_repo(tmp_path, "greenfield-cross-stack")
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        _assert_cross_stack_slices_intact(outcome.partition)


class TestBrownfieldCrossStack:
    """Same task-map shape as greenfield-cross-stack, backed by real files —
    the merge guard's fix reproduces identically once the mapped files exist
    on disk, confirming it is not an artifact of greenfield estimation."""

    def test_partitions_without_cycle_same_as_greenfield(self, tmp_path):
        repo, plan = make_repo(
            tmp_path, "brownfield-cross-stack", real_files=BROWNFIELD_CROSS_STACK_FILES
        )
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        _assert_cross_stack_slices_intact(outcome.partition)


class TestSliceOverBudget:
    """Plan U3: split_over_budget cuts between blocks, never inside a slice —
    the oversized reports slice now stays whole, over budget, rather than
    being dissolved into its two individual tasks (U6's overflow gate is
    what reacts to the overshoot, not the splitter)."""

    def config(self):
        config = OrchestratorConfig()
        config.estimator.token_budget = 8_000
        return config

    def test_slice_stays_whole_even_over_budget(self, tmp_path):
        repo, plan = make_repo(tmp_path, "slice-over-budget")
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=self.config(),
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        by_gid = members_by_group(outcome.partition)
        assert any({"reports-api", "reports-ui"} <= members for members in by_gid.values())


class TestHubInTheMiddle:
    """Plan U4: gateway (hub B) merging with platform (hub A) and one feature
    (billing), while the other three features stay separate, used to leave a
    gateway-> edge and a ->integration edge crossing the same boundary in
    opposite directions — the merge guard now refuses whichever merge would
    close that cycle."""

    def test_partitions_without_cycle(self, tmp_path):
        repo, plan = make_repo(tmp_path, "hub-in-the-middle")
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )


class TestNoAffinitySink:
    """Plan U4: audit (the affinity-less sink) used to merge into whichever
    branch (billing) happened to cluster with the shared scaffold first,
    leaving the other branch (shipping) on both sides of the same group
    boundary — the merge guard now refuses that merge."""

    def test_partitions_without_cycle(self, tmp_path):
        repo, plan = make_repo(tmp_path, "no-affinity-sink")
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )


class TestPureBackend:
    """Control case: a single one-directional utility hub and no aggregator
    means there is no source/sink sandwich to trigger D3. Current behaviour:
    no cycle, and both slices survive intact (real shared-file affinity, not
    just a route tag, holds each one together through the merge)."""

    def test_no_cycle_and_both_slices_survive(self, tmp_path):
        repo, plan = make_repo(tmp_path, "pure-backend")
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        by_gid = members_by_group(outcome.partition)
        assert any({"billing-api", "billing-worker"} <= members for members in by_gid.values())
        assert any({"shipping-api", "shipping-worker"} <= members for members in by_gid.values())


class TestObservatoryRoundA:
    """Plan U5: minimized reproduction of the real Observatory plan's "Round A"
    group-DAG cycle — an SPA hub depending on a backend hub, three two-task
    cross-stack slices split across the two hubs, and a verification task
    converging on all three. Unlike greenfield-cross-stack.md (one hub), the
    merge that would recreate the historical cycle here is the one where
    verify's slice would fold into the backend hub across the still-separate
    SPA hub — the U4 guard refuses exactly that merge (chain_compatible would
    otherwise let it through, since every node pair is dependency-ordered);
    if a cycle ever survives prevention on a shape like this, U5's repair is
    what this fixture is here to keep covered."""

    def test_partitions_without_cycle_and_slices_intact(self, tmp_path):
        repo, plan = make_repo(tmp_path, "observatory-round-a")
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        _assert_cross_stack_slices_intact(outcome.partition)


ALL_FIXTURES = [
    ("greenfield-cross-stack", None, {}),
    ("brownfield-cross-stack", BROWNFIELD_CROSS_STACK_FILES, {}),
    ("hub-in-the-middle", None, {}),
    ("no-affinity-sink", None, {}),
    ("slice-over-budget", None, {"token_budget": 8_000}),
    ("pure-backend", None, {}),
    ("observatory-round-a", None, {}),
]

# slice-over-budget is excluded here on purpose (plan U3 decision): its
# reports slice is a declared, intentional exception to the cap — U6's
# overflow gate is what will react to that, not this generic property.
WITHIN_CAP_FIXTURES = [f for f in ALL_FIXTURES if f[0] != "slice-over-budget"]


class TestProperties:
    """R21: property assertions that hold regardless of which fixture cycles."""

    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", WITHIN_CAP_FIXTURES)
    def test_no_group_summed_work_exceeds_budget_cap(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        repo, plan = make_repo(tmp_path, fixture_name, real_files=real_files)
        config = OrchestratorConfig()
        for key, value in config_overrides.items():
            setattr(config.estimator, key, value)
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=config,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        for members in members_by_group(outcome.partition).values():
            total = sum(outcome.node_work[n] for n in members)
            assert total <= outcome.budget_cap

    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", ALL_FIXTURES)
    def test_partitioning_is_byte_stable_across_runs(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        config = OrchestratorConfig()
        for key, value in config_overrides.items():
            setattr(config.estimator, key, value)

        repo_a, plan_a = make_repo(tmp_path / "a", fixture_name, real_files=real_files)
        outcome_a = compute_partition(
            plan_path=plan_a,
            repo_root=repo_a,
            config=config,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo_a),
        )
        repo_b, plan_b = make_repo(tmp_path / "b", fixture_name, real_files=real_files)
        outcome_b = compute_partition(
            plan_path=plan_b,
            repo_root=repo_b,
            config=config,
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo_b),
        )
        assert serialize_partition(outcome_a.partition) == serialize_partition(outcome_b.partition)
