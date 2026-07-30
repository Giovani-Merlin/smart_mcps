"""Regenerate tests/fixtures/grouping/golden/*.json (plan U5).

Run with: uv run python tests/regenerate_golden_partitions.py

Then review the diff under tests/fixtures/grouping/golden/ before committing —
a deliberate partitioner behaviour change should land as a reviewable diff,
not silently.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.pipeline import compute_partition

from tests.test_grouping_fixtures import ALL_FIXTURES, client_for
from tests.test_grouping_fixtures import make_repo as make_fixture_repo

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "grouping" / "golden"


def _llm_must_not_be_called(prompt, schema):
    raise AssertionError("fixture tests must stay zero-token")


def _compute_baseline(tmp_path: Path, fixture_name: str, real_files, config_overrides: dict):
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


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for fixture_name, real_files, config_overrides in ALL_FIXTURES:
            baseline = _compute_baseline(
                tmp_path / fixture_name, fixture_name, real_files, config_overrides
            )
            out_path = GOLDEN_DIR / f"{fixture_name}.json"
            out_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
            print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
