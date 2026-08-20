"""U7 test: a group reaching terminal FAILED logs its recovery route."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.review import _GroupExecution
from orchestrator.execution.worktrees import group_branch


def test_log_recovery_route_names_branch_worktree_and_retry_command(tmp_path: Path):
    repo_root = tmp_path / "repo"
    paths = RunPaths(repo_root, "r1")
    execution = _GroupExecution.__new__(_GroupExecution)
    execution.deps = SimpleNamespace(run_id="r1", store=SimpleNamespace(paths=paths))
    execution.gid = "g1"
    execution.workspace = repo_root / ".worktrees" / "r1" / "g1-group-g1"

    execution._log_recovery_route()

    log_text = paths.event_log_path.read_text()
    assert group_branch("r1", "g1") in log_text
    assert str(execution.workspace) in log_text
    assert "retry" in log_text
    assert "r1" in log_text and "g1" in log_text
