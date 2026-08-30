"""The operator's deliberate override for the four genuine work failures (plan U7).

``retry`` is the only path back from a terminally ``FAILED`` group, and the
release valve for a quarantined ``INTERRUPTED`` one (plan U1 Decisions) —
everything else in the system treats both as something a plain ``resume`` must
not touch on its own. It keeps branch, worktree, and warm session intact: the
point is to build on the work, not discard it.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.execution.driver import is_driving
from orchestrator.execution.manifest import (
    RunPaths,
    atomic_write_text,
    effective_group,
    log_event,
)
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.execution.worktrees import (
    WorktreeError,
    WorktreeRefreshConflict,
    _refresh_onto_tip,
    group_branch,
    integration_branch,
    worktree_path,
)
from orchestrator.model import GroupingResult


class RetryError(Exception):
    """``retry`` was refused; ``state.json`` is unchanged."""


class RetryConflictError(RetryError):
    """Refreshing the group's branch onto the integration tip hit a real content
    conflict. The branch is left at its pre-refresh commit and ``paths`` names
    the conflicting files, same shape as ``WorktreeRefreshConflict``."""

    def __init__(self, message: str, paths: list[str]):
        super().__init__(message)
        self.paths = paths


def _group_name(paths: RunPaths, group_id: str) -> str:
    result = GroupingResult.model_validate_json(paths.groups_path.read_text())
    for group in result.groups:
        if group.id == group_id:
            # Same overlay as resume/finish: a rewritten group's worktree lives
            # under the speccer name, not the grouper's in groups.json.
            return effective_group(paths, group).name
    raise RetryError(f"group {group_id} not found in {paths.groups_path}")


def _backup_state(repo_root: Path, run_id: str, group_id: str, paths: RunPaths) -> None:
    """Copy the pre-retry ``state.json`` under ``.orchestrator/backups/`` before
    any write (plan R15) — the one thing a bad retry must not be able to lose."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    backup_dir = repo_root / ".orchestrator" / "backups" / run_id / f"{group_id}-{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(paths.state_path, backup_dir / "state.json")


def retry_group(repo_root: Path, run_id: str, group_id: str) -> None:
    """Release a terminally ``FAILED`` group back to ``PENDING``, or clear a
    quarantined ``INTERRUPTED`` group so a following ``resume`` re-enters it
    (plan U7).

    Refuses outright — leaving ``state.json`` byte-identical — while a driver
    process holds the run's lock (plan Decisions): mutating run state under a
    live driver is exactly the race the lock exists to prevent.
    """
    paths = RunPaths(repo_root, run_id)
    if not paths.state_path.is_file():
        raise RetryError(f"no run state at {paths.state_path}")
    if is_driving(paths):
        raise RetryError(
            f"run {run_id} is currently being driven by another process — "
            "stop it before running retry"
        )

    state = RunState.model_validate_json(paths.state_path.read_text())
    if group_id not in state.groups:
        raise RetryError(f"no group {group_id} in run {run_id}")
    entry = state.groups[group_id]

    if entry.state == GroupState.FAILED:
        _retry_failed(repo_root, run_id, group_id, paths, state, entry)
    elif entry.quarantined:
        _retry_quarantined(run_id, group_id, paths, state, entry)
    else:
        raise RetryError(
            f"group {group_id} is {entry.state.value} — retry only releases a "
            "terminally failed group or a quarantined one"
        )


def _retry_failed(
    repo_root: Path,
    run_id: str,
    group_id: str,
    paths: RunPaths,
    state: RunState,
    entry: GroupRunState,
) -> None:
    group_name = _group_name(paths, group_id)
    branch = group_branch(run_id, group_id)
    integration = integration_branch(run_id)
    worktree = worktree_path(repo_root, run_id, group_id, group_name)
    if not worktree.is_dir():
        raise RetryError(f"worktree for group {group_id} not found at {worktree}")

    try:
        _refresh_onto_tip(worktree, group_id=group_id, branch=branch, tip=integration)
    except WorktreeRefreshConflict as exc:
        raise RetryConflictError(str(exc), exc.paths) from exc
    except WorktreeError as exc:
        raise RetryError(str(exc)) from exc

    _backup_state(repo_root, run_id, group_id, paths)
    entry.state = GroupState.PENDING
    entry.failure = None
    entry.holds = []
    entry.resolve_settled = False
    atomic_write_text(paths.state_path, state.model_dump_json(indent=2) + "\n")
    log_event(
        paths,
        f"group {group_id}: retried by operator — refreshed onto {integration}, reset to pending",
    )


def _retry_quarantined(
    run_id: str, group_id: str, paths: RunPaths, state: RunState, entry: GroupRunState
) -> None:
    _backup_state(paths.repo_root, run_id, group_id, paths)
    entry.quarantined = False
    entry.reentry_count = 0
    atomic_write_text(paths.state_path, state.model_dump_json(indent=2) + "\n")
    log_event(paths, f"group {group_id}: quarantine cleared by operator — resume will re-enter it")
