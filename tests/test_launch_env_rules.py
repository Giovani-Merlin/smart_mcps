"""launch_env points TMPDIR/TMP at the worktree scratch; ground rules ban refused shell forms."""

from pathlib import Path

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
