"""A real confined run whose merge gate meets a driver data step.

**This tier spends real tokens** (one tiny coder group) and is excluded from a
plain `pytest` by `addopts = -m "not llm"`; opt in with `-m llm`. The run is a
subprocess so the test can act *during* it: once the group's worktree exists it
starts a data step that holds `data-step.lock`, waits for the gate to report it
is blocked, then kills the step — the kernel drops the flock and the group must
merge.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.execution.manifest import RunPaths
from orchestrator.grouping.pipeline import serialize_grouping
from orchestrator.model import Group, GroupingResult, ReviewIntensity, VerificationItem

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

RUN_ID = "live-ds"
RUN_TIMEOUT_S = 900.0
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
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
            'In greeting.py, add `farewell()` returning "goodbye" right after `greet()`. '
            "Commit the change. Do not change anything else."
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
    (grouping / "base-context.md").write_text("A tiny scratch repo. Keep every change minimal.\n")
    return repo


def _wait_for_log(log: Path, needle: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        if log.is_file() and needle in log.read_text():
            return
        time.sleep(0.5)
    tail = log.read_text()[-3000:] if log.is_file() else "(no run.log)"
    raise AssertionError(f"never saw {needle!r} in run.log\n{tail}")


def test_the_gate_waits_for_a_driver_data_step_then_merges(tmp_path):
    repo = _build_repo(tmp_path)
    paths = RunPaths(repo, RUN_ID)
    env = {
        **os.environ,
        "PYTHONPATH": f"{PROJECT_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    deadline = time.monotonic() + RUN_TIMEOUT_S
    run = subprocess.Popen(
        [
            sys.executable, "-m", "orchestrator.cli", "run",
            "--repo", str(repo), "--run-id", RUN_ID, "--intensity", "autonomous",
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    step = None
    try:
        _wait_for_log(paths.event_log_path, "worktree ready", deadline)
        step_code = (
            "import sys; from pathlib import Path; "
            "from orchestrator.execution.data_step import run_data_step; "
            f"sys.exit(run_data_step(Path(sys.argv[1]), {RUN_ID!r}, 'sleep 600'))"
        )
        step = subprocess.Popen(
            [sys.executable, "-c", step_code, str(repo)],
            env=env,
            start_new_session=True,
        )
        _wait_for_log(
            paths.event_log_path,
            "gate waiting for driver data step (sleep 600)",
            deadline,
        )
        os.killpg(step.pid, signal.SIGKILL)  # the sleep child goes too
        step.wait()
        remaining = max(deadline - time.monotonic(), 1.0)
        assert run.wait(timeout=remaining) == 0, paths.event_log_path.read_text()[-3000:]
    finally:
        if step is not None and step.poll() is None:
            os.killpg(step.pid, signal.SIGKILL)
        if run.poll() is None:
            run.kill()
    assert f"merge({RUN_ID}): g1" in _git(repo, "log", "--oneline", f"orchestrator/run-{RUN_ID}")
