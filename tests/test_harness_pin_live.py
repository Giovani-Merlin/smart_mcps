"""A confined `evaluate` run whose smoke fails, is fixed by the operator, then
`retry` + `resume` complete it — no harness pin was ever written, so none is
reset (plan 2026-10-10-001, U3, g5).

**This tier spends real tokens** (the failed smoke goes through real triage).
Excluded from a plain `pytest` by `addopts = -m "not llm"`; run by the driver
with `uv run pytest tests/test_harness_pin_live.py -q -m llm`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.execution.manifest import RunPaths
from orchestrator.grouping.pipeline import serialize_grouping
from orchestrator.model import Group, GroupingResult, ReviewIntensity, VerificationItem

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

RUN_TIMEOUT_S = 900.0
RUN_ID = "harness-pin-live1"

_CONFIG_TOML = """\
[session]
confine = true

[escalation]
enabled = false

[workspace]
data_dirs = ["data"]

[recipes]
enabled = ["evaluate"]
"""


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


def _cli(repo: Path, *argv: str, expected_exit: int | None = 0) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "orchestrator.cli", *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )
    if expected_exit is not None:
        assert done.returncode == expected_exit, (
            f"{argv[0]} exited {done.returncode}\n{done.stdout}\n{done.stderr}"
        )
    return done.stdout + done.stderr


def _evaluate_group() -> Group:
    return Group(
        id="g1",
        name="score",
        summary="Score a fixture with a smoke gate that needs data/ok.",
        spec="(evaluate recipe — no coder prompt)",
        difficulty=0.0,
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="evaluate",
        recipe_args={
            "commands": [{"cmd": "sh scripts/score.sh", "wall_clock_min": 2.0}],
            "measurements": "measurements.json",
            "commit_paths": ["measurements.json"],
            "kpi": {
                "key": "score",
                "direction": "max",
                "harness_paths": ["scripts/score.sh"],
                "smoke": "test -f data/ok",
            },
        },
        verification=[VerificationItem(id="v1", description="the KPI is measured")],
    )


@pytest.fixture(scope="module")
def live_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("live-repo-harness-pin")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "score.sh").write_text(
        "#!/bin/sh\nprintf '{\"score\": 3}' > measurements.json\n"
    )
    (repo / "plan.md").write_text("# plan\n\n## Tasks\n\n- u1-score (recipe: evaluate): score\n")
    (repo / ".gitignore").write_text("data/\n.orchestrator/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "data").mkdir()
    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(_CONFIG_TOML)
    grouping_dir = repo / ".orchestrator" / "groupings" / "plan"
    grouping_dir.mkdir(parents=True)
    (grouping_dir / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=[_evaluate_group()]))
    )
    (grouping_dir / "base-context.md").write_text("A tiny scratch repo for an evaluate recipe.\n")
    return repo


def test_failed_smoke_then_retry_resume_completes_without_a_pin_reset(live_repo: Path):
    repo = live_repo
    # the smoke exits non-zero (no data/ok yet): the group ends FAILED
    _cli(
        repo,
        "run",
        "--repo",
        str(repo),
        "--run-id",
        RUN_ID,
        "--intensity",
        "autonomous",
        expected_exit=None,
    )
    paths = RunPaths(repo, RUN_ID)
    state = json.loads(paths.state_path.read_text())
    assert state["groups"]["g1"]["state"] == "failed", state
    assert not (paths.group_dir("g1") / "run" / "eval" / "harness.sha256").exists()

    (repo / "data" / "ok").write_text("ok\n")
    _cli(repo, "retry", RUN_ID, "g1", "--repo", str(repo))
    _cli(repo, "resume", RUN_ID, "--repo", str(repo), "--intensity", "autonomous")

    state = json.loads(paths.state_path.read_text())
    assert state["groups"]["g1"]["state"] == "completed", state
    # The artifact manifest carries the record's ``measurements`` (the KPI key
    # among them), not ``EvaluationRecord.kpi_value`` itself — ``_register_artifact``
    # never wrote that field (pre-existing; found by the driver on r20261010-134127).
    manifest = paths.run_dir.joinpath("artifacts.json")
    if manifest.is_file():
        entry = json.loads(manifest.read_text())["entries"]["g1"]
        assert entry["schema"] == "EvaluationRecord", entry
        assert entry["measurements"].get("score") == 3, entry
    log = (paths.run_dir / "logs" / "run.log").read_text()
    assert "harness pin reset by retry" not in log
