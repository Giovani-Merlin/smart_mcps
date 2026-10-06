"""launch_env points TMPDIR/TMP at the worktree scratch; ground rules ban refused shell forms."""

import shutil
from pathlib import Path

import pytest

from orchestrator.execution.sessions import launch_env
from orchestrator.prompts import load_template


def test_launch_env_sets_tmpdir_and_tmp(tmp_path: Path) -> None:
    base = {"PATH": "/usr/bin"}
    env = launch_env(base, tmp_path)
    scratch = str(tmp_path / ".coder-scratch")
    assert env["TMPDIR"] == env["TMP"] == scratch
    assert (tmp_path / ".coder-scratch").is_dir()
    assert base == {"PATH": "/usr/bin"}


def test_launch_env_without_cwd_is_unchanged() -> None:
    base = {"PATH": "/usr/bin"}
    assert launch_env(base, None) == base


def test_ground_rules_ban_refused_forms() -> None:
    text = load_template("worker_ground_rules")
    assert "git -C" in text
    assert "simple_expansion" in text
    assert "never from a diff" in text


@pytest.mark.llm
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_a_confined_real_worker_mktemp_lands_in_the_worktree_scratch(tmp_path: Path) -> None:
    """Run r20261006-115802, item g2-2: through the real runner (Landlock on,
    `launch_env` applied) a bare `mktemp -d && echo ok` is not refused and the
    directory it makes is under the worktree's `.coder-scratch/`, not `/tmp`."""
    from orchestrator.config import DEFAULT_ALLOWED_TOOLS
    from orchestrator.execution.sessions import WORKER_PROJECT_DIRNAME, SessionRunner

    worktree = tmp_path / "wt"
    worktree.mkdir()
    runner = SessionRunner(
        claude_bin="claude",
        model="sonnet",
        confine=True,
        allowed_tools=[*DEFAULT_ALLOWED_TOOLS, "Bash(mktemp)", "Bash(mktemp *)", "Bash(echo *)"],
    )
    result = runner.start_worker(
        base_context="",
        prompt=(
            "Run the shell command `mktemp -d && echo ok` with the Bash tool, then reply "
            "with the exact directory path it printed and nothing else."
        ),
        name="live-tmpdir-probe",
        cwd=worktree,
    )
    scratch = worktree / WORKER_PROJECT_DIRNAME
    made = [p for p in scratch.glob("tmp.*") if p.is_dir()]
    assert made, f"mktemp made nothing under {scratch}: {result.text[:300]!r}"
    assert str(scratch) in result.text, result.text[:300]
