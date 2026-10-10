"""Live: an untracked leftover is archived on the first strike and the group
merges in one generation — no same-spec relaunch.

**This tier spends real tokens.** Opt in with `uv run pytest -m llm`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.execution.manifest import RunPaths
from orchestrator.grouping.pipeline import serialize_grouping
from orchestrator.model import Group, GroupingResult, ReviewIntensity, VerificationItem

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

RUN_TIMEOUT_S = 900.0


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


def test_untracked_probe_is_archived_and_the_group_merges_in_one_generation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "greeting.py").write_text('def greet():\n    return "hello"\n')
    (repo / "plan.md").write_text("# plan\n\n## Tasks\n\n- T1: add farewell()\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(
        "[session]\nconfine = true\n\n[escalation]\nenabled = false\n"
    )
    group = Group(
        id="g1",
        name="farewell",
        summary="Add a farewell function.",
        spec=(
            "In greeting.py, add `farewell()` returning \"goodbye\" after `greet()` and "
            "commit that change. ALSO, on purpose, create a file `probe.txt` in the "
            "repository root containing `probe` and do NOT add, commit, move or delete "
            "it — leave it untracked."
        ),
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["greeting.py"],
        verification=[VerificationItem(id="v1", description="farewell() returns goodbye")],
    )
    grouping = repo / ".orchestrator" / "groupings" / "plan"
    grouping.mkdir(parents=True)
    (grouping / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=[group]))
    )
    (grouping / "base-context.md").write_text("Tiny scratch repo. Keep changes minimal.\n")

    run_id = "untracked1"
    log_path = repo / "run.log"
    started = time.time()
    with log_path.open("w") as sink:
        saved, sys.stdout = sys.stdout, sink
        try:
            exit_code = main(
                ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"]
            )
        finally:
            sys.stdout = saved
    output = log_path.read_text()
    assert time.time() - started < RUN_TIMEOUT_S, output
    assert exit_code == 0, output

    paths = RunPaths(repo, run_id)
    assert (paths.untracked_archive_dir("g1") / "probe.txt").is_file(), output
    run_log = paths.event_log_path.read_text()
    assert "UNTRACKED FILES ARCHIVED" in run_log
    assert "relaunching on the same spec" not in run_log
    state = json.loads(paths.state_path.read_text())
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g1"].get("generation", 1) == 1
