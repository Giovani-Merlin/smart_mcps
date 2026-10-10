"""A real two-group confined run held at a driver item (plan U2, live tier).

**This tier spends real tokens.** Excluded from a plain `pytest` by
`addopts = -m "not llm"`; opt in with `uv run pytest -m llm`.

g1 is one line of code plus a required `Run (driver):` item the coder cannot
run; g2 depends on it. The run is a subprocess so this test can act *during*
it: wait for the hold line in `logs/run.log`, record the item from its own
process, and watch g2 launch.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.execution.driver_items import record_driver_item
from orchestrator.execution.manifest import RunPaths
from orchestrator.grouping.pipeline import serialize_grouping
from orchestrator.model import Group, GroupingResult, ReviewIntensity, VerificationItem

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

RUN_TIMEOUT_S = 900.0
RUN_ID = "driver-hold-live1"

_CONFIG_TOML = """\
[session]
confine = true

[escalation]
enabled = false
"""


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


def _groups() -> list[Group]:
    g1 = Group(
        id="g1",
        name="farewell",
        summary="Add a farewell function beside greet().",
        spec=(
            "In greeting.py, add a function `farewell()` returning the string "
            '"goodbye", right after `greet()`. Then commit the change. '
            "Do not change anything else."
        ),
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["greeting.py"],
        verification=[
            VerificationItem(
                id="v1",
                description="Run (driver): python -c 'import greeting' Pass: exits 0",
            )
        ],
    )
    g2 = Group(
        id="g2",
        name="note",
        summary="Add a NOTES.md file.",
        spec="Create NOTES.md containing the single line `done`. Commit it. Change nothing else.",
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=["g1"],
        files=["NOTES.md"],
        verification=[VerificationItem(id="v2", description="NOTES.md exists")],
    )
    return [g1, g2]


def _build_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "live-repo-driver-hold"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "README.md").write_text("# driver hold fixture\n")
    (repo / "greeting.py").write_text('def greet():\n    return "hello"\n')
    (repo / "plan.md").write_text(
        "# plan\n\n## Tasks\n\n- u1-farewell: add farewell()\n- u2-note: add NOTES.md\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(_CONFIG_TOML)
    grouping_dir = repo / ".orchestrator" / "groupings" / "plan"
    grouping_dir.mkdir(parents=True)
    (grouping_dir / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=_groups()))
    )
    (grouping_dir / "base-context.md").write_text(
        "A tiny scratch repo. Keep every change minimal.\n"
    )
    return repo


def test_dependent_group_launches_only_after_the_driver_records_the_item(tmp_path):
    repo = _build_repo(tmp_path)
    paths = RunPaths(repo, RUN_ID)
    log_path = paths.run_dir / "logs" / "run.log"
    out = (tmp_path / "stdout.txt").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "orchestrator.cli",
            "run",
            "--repo",
            str(repo),
            "--run-id",
            RUN_ID,
            "--intensity",
            "autonomous",
        ],
        stdout=out,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        held = "group g2: held — dependency g1"
        while time.monotonic() < deadline:
            if log_path.is_file() and held in log_path.read_text():
                break
            assert proc.poll() is None, f"run exited before holding g2: {proc.returncode}"
            time.sleep(2)
        else:
            pytest.fail("g2 was never held on g1's driver item")

        state = json.loads(paths.state_path.read_text())
        assert state["groups"]["g1"]["state"] == "completed"
        assert state["groups"]["g2"]["state"] == "pending"

        record_driver_item(paths, _groups()[0], "v1", "pass", "recorded by the live test")
        returncode = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
    finally:
        if proc.poll() is None:
            proc.kill()
        out.close()

    assert returncode == 0, (tmp_path / "stdout.txt").read_text()
    state = json.loads(paths.state_path.read_text())
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g2"]["state"] == "completed"
