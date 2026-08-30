"""Worktree path resolution across the pre/post-U2 layouts, and the
`worktree add` collision diagnostic born from the r20260830-163212 P0."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


# ------------------------------------------ existing_worktree_path (U2 legacy)


def test_existing_worktree_path_prefers_the_run_scoped_layout(tmp_path):
    from orchestrator.execution.worktrees import existing_worktree_path

    current = tmp_path / ".worktrees" / "r1" / "g1-alpha"
    current.mkdir(parents=True)
    legacy = tmp_path / ".worktrees" / "g1-alpha"
    legacy.mkdir(parents=True)
    assert existing_worktree_path(tmp_path, "r1", "g1", "alpha") == current


def test_existing_worktree_path_falls_back_to_legacy(tmp_path):
    from orchestrator.execution.worktrees import existing_worktree_path

    legacy = tmp_path / ".worktrees" / "g1-alpha"
    legacy.mkdir(parents=True)
    assert existing_worktree_path(tmp_path, "r1", "g1", "alpha") == legacy


def test_existing_worktree_path_knows_the_integration_legacy_name(tmp_path):
    """The integration worktree's legacy path is `run-<run_id>-integration`, not
    `integration-integration` — it was created with group_id=f"run-{run_id}"."""
    from orchestrator.execution.worktrees import existing_worktree_path

    legacy = tmp_path / ".worktrees" / "run-r1-integration"
    legacy.mkdir(parents=True)
    assert existing_worktree_path(tmp_path, "r1", "integration", "integration") == legacy


def test_existing_worktree_path_is_none_when_neither_exists(tmp_path):
    from orchestrator.execution.worktrees import existing_worktree_path

    assert existing_worktree_path(tmp_path, "r1", "g1", "alpha") is None


# ------------------------------------- worktree add collision diagnostic (P0)


def test_failed_worktree_add_names_the_directory_holding_the_branch(repo):
    """The r20260830-163212 resume died on a bare git error; the diagnostic must
    name the worktree that actually has the branch checked out, so a residual
    name desync is actionable instead of a mystery."""
    from orchestrator.execution.worktrees import WorktreeError, create_worktree

    holder = create_worktree(
        repo,
        run_id="r1",
        group_id="g1",
        name="rewritten slice",
        branch="orchestrator/r1-g1",
        start_point="main",
    )
    # the stale-name path: same branch, different slug — the pre-fix P0 shape
    with pytest.raises(WorktreeError, match=f"already checked out at .*{holder.name}"):
        create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name="grouper original name",
            branch="orchestrator/r1-g1",
            start_point="main",
        )


def test_failed_worktree_add_without_a_registered_holder_still_reports(repo, monkeypatch):
    """The holder-is-None fallback: the error still carries path, branch and
    git's stderr even when no registered worktree explains the failure."""
    import orchestrator.execution.worktrees as wt_mod
    from orchestrator.execution.worktrees import WorktreeError, create_worktree

    git(repo, "branch", "orchestrator/r1-g1")
    real_git = wt_mod._git

    def failing_git(cwd, *args):
        if args[:2] == ("worktree", "add"):
            done = subprocess.CompletedProcess(args, 128, stdout="", stderr="boom from git")
            return done
        return real_git(cwd, *args)

    monkeypatch.setattr(wt_mod, "_git", failing_git)
    with pytest.raises(WorktreeError) as excinfo:
        create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name="alpha",
            branch="orchestrator/r1-g1",
            start_point="main",
        )
    message = str(excinfo.value)
    assert "orchestrator/r1-g1" in message and "boom from git" in message
    assert "already checked out" not in message


def test_worktree_of_branch_parses_the_porcelain_listing(repo):
    from orchestrator.execution.worktrees import _worktree_of_branch, create_worktree

    path = create_worktree(
        repo,
        run_id="r1",
        group_id="g1",
        name="alpha",
        branch="orchestrator/r1-g1",
        start_point="main",
    )
    assert _worktree_of_branch(repo, "orchestrator/r1-g1") == path
    assert _worktree_of_branch(repo, "no-such-branch") is None
    # main is checked out at the repo root itself
    assert _worktree_of_branch(repo, "main") == repo


def test_git_rejects_a_missing_working_directory_with_context(tmp_path):
    from orchestrator.execution.worktrees import WorktreeError, _git

    with pytest.raises(WorktreeError, match="working directory does not exist"):
        _git(tmp_path / "absent", "status")
