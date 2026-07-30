"""Golden partition baselines (plan U5): tests/fixtures/grouping/golden/. Each
fixture's partition is recorded as a committed baseline, and this test fails,
printing both sides, when a fresh recompute differs from it — the three drift
directions a byte-stability test (which only compares two runs of the same
code) cannot see: a fixture's group count changing, a task moving between
groups, or a group's summed work crossing the budget cap. Regenerate with
`uv run python tests/regenerate_golden_partitions.py` and review the diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.pipeline import compute_partition
from tests.test_grouping_fixtures import ALL_FIXTURES, client_for
from tests.test_grouping_fixtures import make_repo as make_fixture_repo

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "grouping" / "golden"

# slice-over-budget is excluded here on purpose (plan U3/U6 decision, mirrored
# from test_grouping_fixtures.py's WITHIN_CAP_FIXTURES): its reports slice is a
# declared, intentional cap overshoot, so the cap-crossing check below does not
# apply to it — that fixture's own overflow-gate behaviour is covered by
# TestSliceOverBudget in test_grouping_fixtures.py.
WITHIN_CAP_FIXTURES = [f for f in ALL_FIXTURES if f[0] != "slice-over-budget"]


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("fixture tests must stay zero-token")


def _current(tmp_path, fixture_name, real_files, config_overrides):
    config = OrchestratorConfig()
    for key, value in config_overrides.items():
        setattr(config.estimator, key, value)
    if fixture_name == "slice-over-budget":
        config.partition.allow_oversized_slice = True
    repo, plan = make_fixture_repo(tmp_path, fixture_name, real_files=real_files)
    outcome = compute_partition(
        plan_path=plan,
        repo_root=repo,
        config=config,
        llm_runner=_llm_must_not_be_called,
        client=client_for(repo, fixture_name),
    )
    group_work: dict[str, float] = {}
    for node, gid in outcome.partition.items():
        group_work[str(gid)] = group_work.get(str(gid), 0.0) + outcome.node_work[node]
    return {
        "group_count": len({*outcome.partition.values()}),
        "budget_cap": outcome.budget_cap,
        "partition": dict(sorted(outcome.partition.items())),
        "group_work": dict(sorted(group_work.items())),
    }


class TestGoldenPartitions:
    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", ALL_FIXTURES)
    def test_matches_committed_baseline(self, tmp_path, fixture_name, real_files, config_overrides):
        baseline_path = GOLDEN_DIR / f"{fixture_name}.json"
        assert baseline_path.is_file(), f"no golden baseline for {fixture_name!r}: {baseline_path}"
        baseline = json.loads(baseline_path.read_text())
        current = _current(tmp_path, fixture_name, real_files, config_overrides)

        assert current["partition"] == baseline["partition"], (
            f"{fixture_name}: partition drifted from baseline\n"
            f"  baseline: {baseline['partition']}\n"
            f"  current:  {current['partition']}"
        )
        assert current["group_count"] == baseline["group_count"], (
            f"{fixture_name}: group count drifted "
            f"(baseline {baseline['group_count']} -> current {current['group_count']})"
        )

    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", WITHIN_CAP_FIXTURES)
    def test_no_group_work_crosses_the_cap(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        """Independent of membership drift: even if the partition matches its
        baseline exactly, a change to work estimation could still push a
        group's summed work over the (also-current) budget cap."""
        current = _current(tmp_path, fixture_name, real_files, config_overrides)
        for gid, work in current["group_work"].items():
            assert work <= current["budget_cap"], (
                f"{fixture_name}: group {gid} work {work:.1f} exceeds budget cap "
                f"{current['budget_cap']:.1f}"
            )

    def test_symbol_bearing_fixture_is_covered(self):
        """R5/limitation-4 regression coverage: the codegraph-derived edge
        layer, not just the plan-declared layer, has a golden baseline too."""
        assert (GOLDEN_DIR / "hub-file-symbols.json").is_file()
