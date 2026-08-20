"""Worktree path resolution across the pre/post-U2 layouts."""

from __future__ import annotations

import pytest


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


def test_git_rejects_a_missing_working_directory_with_context(tmp_path):
    from orchestrator.execution.worktrees import WorktreeError, _git

    with pytest.raises(WorktreeError, match="working directory does not exist"):
        _git(tmp_path / "absent", "status")
