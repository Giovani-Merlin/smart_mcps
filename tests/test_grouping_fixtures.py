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
from orchestrator.grouping.partition import GroupCycleError
from orchestrator.grouping.pipeline import compute_partition

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "grouping"


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("fixture tests must stay zero-token")


def stub_codegraph_runner(args):
    """Zero real codegraph: every fixture plan declares no `symbols`, so the
    only call the pipeline ever issues is `codegraph files` for the base
    context. Anything else means a fixture accidentally started using symbols."""
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


class TestGreenfieldCrossStack:
    """Current behaviour: the aggregator (verify) and the utility hub (scaffold)
    end up merged with one slice (auth) by merge_small_groups, while the other
    two slices (items, profile) stay separate — leaving both a scaffold->items
    edge and an items->verify edge crossing the same group boundary in opposite
    directions. This is the D3 mechanism, not a bug introduced here."""

    def test_cycles_today(self, tmp_path):
        repo, plan = make_repo(tmp_path, "greenfield-cross-stack")
        with pytest.raises(GroupCycleError):
            compute_partition(
                plan_path=plan,
                repo_root=repo,
                llm_runner=_llm_must_not_be_called,
                client=client_for(repo),
            )


class TestBrownfieldCrossStack:
    """Same task-map shape as greenfield-cross-stack, backed by real files.
    Current behaviour: the cycle is structural (D3), not an artifact of
    greenfield file-count-driven estimation (D4) — it reproduces identically
    once the mapped files exist on disk."""

    def test_cycles_today_same_as_greenfield(self, tmp_path):
        repo, plan = make_repo(
            tmp_path, "brownfield-cross-stack", real_files=BROWNFIELD_CROSS_STACK_FILES
        )
        with pytest.raises(GroupCycleError):
            compute_partition(
                plan_path=plan,
                repo_root=repo,
                llm_runner=_llm_must_not_be_called,
                client=client_for(repo),
            )


class TestSliceOverBudget:
    """Current (soft) behaviour: split_over_budget dissolves the oversized
    slice into its two individual tasks rather than failing loudly (H1 in the
    register would make this a hard error instead)."""

    def config(self):
        config = OrchestratorConfig()
        config.estimator.token_budget = 8_000
        return config

    def test_slice_is_split_apart_not_kept_whole(self, tmp_path):
        repo, plan = make_repo(tmp_path, "slice-over-budget")
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=self.config(),
            llm_runner=_llm_must_not_be_called,
            client=client_for(repo),
        )
        groups = groups_of(outcome.partition)
        assert frozenset({"reports-api", "reports-ui"}) not in groups
        by_gid = members_by_group(outcome.partition)
        assert any(members == {"reports-api"} for members in by_gid.values())
        assert any(members == {"reports-ui"} for members in by_gid.values())


class TestHubInTheMiddle:
    """Current behaviour: gateway (hub B) merges with platform (hub A) and one
    feature (billing) into a single group; the other three features stay
    separate, each receiving a gateway-> edge and sending a ->integration edge
    across the same boundary — the D3 cycle, reproduced from a source/sink
    sandwiching a middle hub rather than a slice."""

    def test_cycles_today(self, tmp_path):
        repo, plan = make_repo(tmp_path, "hub-in-the-middle")
        with pytest.raises(GroupCycleError):
            compute_partition(
                plan_path=plan,
                repo_root=repo,
                llm_runner=_llm_must_not_be_called,
                client=client_for(repo),
            )


class TestNoAffinitySink:
    """Current behaviour: audit (the affinity-less sink) merges into whichever
    branch (billing) happens to cluster with the shared scaffold first; the
    other branch (shipping) stays separate and ends up on both sides of the
    same group boundary — the D3 cycle again, from the opposite contributing
    factor (a sink with no shared-file gravity)."""

    def test_cycles_today(self, tmp_path):
        repo, plan = make_repo(tmp_path, "no-affinity-sink")
        with pytest.raises(GroupCycleError):
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


NON_CYCLING_FIXTURES = [
    ("slice-over-budget", None, {"token_budget": 8_000}),
    ("pure-backend", None, {}),
]


class TestProperties:
    """R21: property assertions that hold regardless of which fixture cycles."""

    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", NON_CYCLING_FIXTURES)
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

    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", NON_CYCLING_FIXTURES)
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
