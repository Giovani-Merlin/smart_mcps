"""U7 test: a group reaching terminal FAILED logs its recovery route.

Regression test for a reviewer finding: the recovery-route log line used to be
logged from individual `GroupFailure` raise sites inside review.py, which meant
the `ReportError` route (raised in sessions.py, classified terminal FAILED
directly by scheduler.py's `_run_group`) never triggered it. The logging now
lives in `Scheduler._classify`, the single point every terminal-FAILED route
passes through — so it fires uniformly.
"""

from __future__ import annotations

import pytest

from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.review import GroupFailure
from orchestrator.execution.scheduler import GroupState, Scheduler
from orchestrator.execution.sessions import ReportError
from orchestrator.execution.worktrees import group_branch, worktree_path
from orchestrator.model import Group, ReviewIntensity


def make_group(gid: str) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=[],
        files=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [ReportError("no valid report block after 2 nudges"), GroupFailure("coder blocked")],
    ids=["report_error", "group_failure"],
)
async def test_terminal_failed_logs_recovery_route_for_every_route(tmp_path, exc):
    repo_root = tmp_path / "repo"
    paths = RunPaths(repo_root, "r1")
    group = make_group("g1")

    async def executor(ctx):
        raise exc

    scheduler = Scheduler(groups=[group], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.FAILED

    log_text = paths.event_log_path.read_text()
    assert group_branch("r1", "g1") in log_text
    expected_worktree = worktree_path(repo_root, "r1", "g1", group.name)
    assert str(expected_worktree) in log_text
    assert "retry" in log_text
    assert "smart-mcps-orchestrate retry --repo" in log_text
    assert "r1" in log_text and "g1" in log_text
