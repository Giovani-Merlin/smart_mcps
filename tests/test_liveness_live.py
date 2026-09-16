"""Plan U14 — Not Live end to end against a real `claude` child.

Everything below this tier (`tests/test_liveness.py`) proves the Sign of Life
mechanism against a synthetic `activity_provider` and a fake `/proc` tree. This
test is the one place a *real* worker child is frozen with `SIGSTOP` and the
whole stack — the heartbeat tick, the run log, and `status` — is asked to
notice, in that order, without any of it knowing the freeze was deliberate.

**This tier spends real tokens** (one sonnet coder round). Excluded from a
plain `pytest` by `addopts = -m "not llm"`; opt in with `uv run pytest -m llm`.
"""

from __future__ import annotations

import json
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

#: `window_seconds = 20` in the fixture's own config; polled well past both the
#: heartbeat's 15s tick and the window, short of "something is actually wrong".
NOT_LIVE_POLL_DEADLINE_S = 60.0
RUN_TIMEOUT_S = 900.0


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


def _orchestrate_bin() -> Path:
    """This worktree's own console script — never the operator's globally
    installed `smart-mcps-orchestrate`, which may be a different version."""
    return Path(sys.executable).parent / "smart-mcps-orchestrate"


@pytest.fixture(scope="session")
def liveness_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("liveness-repo")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "README.md").write_text("# liveness live fixture\n")
    (repo / "greeting.py").write_text('def greet():\n    return "hello"\n')
    (repo / "plan.md").write_text(
        "# live plan\n\n## Tasks\n\n- T1: add a farewell() function beside greet()\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(
        '[session]\nconfine = true\nmodel = "sonnet"\n\n'
        "[escalation]\nenabled = false\n\n"
        "[liveness]\nwindow_seconds = 20\n"
    )

    group = Group(
        id="g1",
        name="farewell",
        summary="Add a farewell function.",
        spec=(
            "In greeting.py, add a function `farewell()` returning the string "
            '"goodbye", right after `greet()`. Then commit the change. '
            "Do not change anything else."
        ),
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["greeting.py"],
        verification=[
            VerificationItem(id="v1", description="farewell() exists and returns goodbye")
        ],
    )
    grouping = repo / ".orchestrator" / "groupings" / "plan"
    grouping.mkdir(parents=True)
    (grouping / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=[group]))
    )
    (grouping / "base-context.md").write_text(
        "This is a tiny scratch repository used to verify Not Live end to end. "
        "Keep every change minimal.\n"
    )
    return repo


def _run_status(orchestrate: Path, repo: Path, run_id: str) -> str:
    done = subprocess.run(
        [str(orchestrate), "status", run_id, "--repo", str(repo)],
        capture_output=True,
        text=True,
    )
    return done.stdout


def test_not_live_detected_and_cleared_on_a_real_sigstopped_child(liveness_repo: Path) -> None:
    repo = liveness_repo
    run_id = "liveness1"
    orchestrate = _orchestrate_bin()
    paths = RunPaths(repo, run_id)
    log_path = repo / "run.log"

    with log_path.open("w") as sink:
        proc = subprocess.Popen(
            [
                str(orchestrate),
                "run",
                "--repo",
                str(repo),
                "--run-id",
                run_id,
                "--intensity",
                "autonomous",
            ],
            stdout=sink,
            stderr=sink,
        )

    child_pid: int | None = None
    try:
        # 1. Wait for a real worker child to be registered. Polls `proc.poll()`
        # too: a run that fails before ever spawning a worker (a bad config, a
        # denied write) should be reported immediately, not after the full
        # timeout — the failure text is already sitting in `run.log`.
        deadline = time.time() + RUN_TIMEOUT_S
        while time.time() < deadline and child_pid is None:
            if paths.state_path.is_file():
                try:
                    state = json.loads(paths.state_path.read_text())
                except json.JSONDecodeError:
                    state = {}
                live_pids = state.get("live_pids") or {}
                if live_pids:
                    child_pid = int(next(iter(live_pids)))
            if child_pid is None:
                if proc.poll() is not None:
                    break
                time.sleep(1)
        assert child_pid is not None, (
            f"no worker child ever registered (run exited {proc.poll()})\n{log_path.read_text()}"
        )

        # 2. Freeze it. No suspend was ever recorded, so this must read as
        # Not Live via the plain Sign of Life path, never a Suspend Cure.
        os.kill(child_pid, signal.SIGSTOP)

        # 3. Poll `status` until it reports Not Live with cpu-flat evidence.
        not_live_deadline = time.time() + NOT_LIVE_POLL_DEADLINE_S
        status_out = ""
        while time.time() < not_live_deadline:
            status_out = _run_status(orchestrate, repo, run_id)
            if "liveness: NOT LIVE for" in status_out:
                break
            time.sleep(5)
        assert "liveness: NOT LIVE for" in status_out, (
            f"status never reported Not Live within {NOT_LIVE_POLL_DEADLINE_S:.0f}s\n{status_out}"
        )
        assert "cpu flat" in status_out, status_out
        assert "suspend cure" not in log_path.read_text(), (
            "no machine suspend was ever recorded — a cure firing here means the "
            "cure predicate no longer requires a recorded wake"
        )

        # 4. `status` derives Not Live from the heartbeat's facts at read time;
        # the probe logs the transition only on its own next tick (≤ 15 s
        # later). Thawing before that tick would leave nothing to be "live
        # again" from, so wait for the entering line first — it is asserted too.
        entered_deadline = time.time() + NOT_LIVE_POLL_DEADLINE_S
        while time.time() < entered_deadline:
            if (
                paths.event_log_path.is_file()
                and "not live for" in paths.event_log_path.read_text()
            ):
                break
            time.sleep(1)
        assert paths.event_log_path.is_file() and (
            "not live for" in paths.event_log_path.read_text()
        ), f"the probe never logged entering Not Live\n{log_path.read_text()}"

        # 5. Thaw it and confirm the run notices and finishes.
        os.kill(child_pid, signal.SIGCONT)
        live_again_deadline = time.time() + RUN_TIMEOUT_S
        while time.time() < live_again_deadline:
            if paths.event_log_path.is_file() and "live again" in paths.event_log_path.read_text():
                break
            time.sleep(2)
        assert (
            paths.event_log_path.is_file() and "live again" in paths.event_log_path.read_text()
        ), f"no 'live again' line ever appeared\n{log_path.read_text()}"

        exit_code = proc.wait(timeout=RUN_TIMEOUT_S)
        output = log_path.read_text()
        assert exit_code == 0, f"run exited {exit_code}\n{output}"
        state = json.loads(paths.state_path.read_text())
        assert state["groups"]["g1"]["state"] == "completed"
    finally:
        if proc.poll() is None:
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            proc.kill()
            proc.wait(timeout=30)
