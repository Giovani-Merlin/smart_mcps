"""U7 tests: the operator's deliberate override for terminally FAILED and
quarantined groups — `retry`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.execution.driver import DriverLock
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.retry import RetryConflictError, RetryError, retry_group
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState, Scheduler
from orchestrator.execution.worktrees import (
    create_worktree,
    group_branch,
    integration_branch,
)
from orchestrator.model import (
    Group,
    GroupingResult,
    GroupManifestEntry,
    ReviewIntensity,
    RunManifest,
    SessionEntry,
    SessionRole,
)


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


def make_group(gid: str) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
    )


def write_grouping(paths: RunPaths, group: Group) -> None:
    grouping = GroupingResult(plan_path="plan.md", groups=[group])
    atomic_write_text(paths.groups_path, grouping.model_dump_json())


def write_state(paths: RunPaths, run_id: str, group_id: str, entry: GroupRunState) -> RunState:
    state = RunState(run_id=run_id, groups={group_id: entry})
    atomic_write_text(paths.state_path, state.model_dump_json(indent=2) + "\n")
    return state


def make_integration_branch(repo: Path, run_id: str) -> str:
    branch = integration_branch(run_id)
    git(repo, "branch", branch)
    return branch


def make_group_worktree(repo: Path, run_id: str, group: Group, start_point: str) -> Path:
    return create_worktree(
        repo,
        run_id=run_id,
        group_id=group.id,
        name=group.name,
        branch=group_branch(run_id, group.id),
        start_point=start_point,
    )


def write_manifest(paths: RunPaths, run_id: str, group: Group) -> RunManifest:
    manifest = RunManifest(
        run_id=run_id,
        plan_path="plan.md",
        base_session_id="base-1",
        groups={
            group.id: GroupManifestEntry(
                group_id=group.id,
                group_name=group.name,
                summary="summary",
                sessions=[SessionEntry(session_id="coder-1", role=SessionRole.CODER)],
            )
        },
    )
    ManifestStore(paths).save(manifest)
    return manifest


# ------------------------------------------------------------------- reset


def test_retry_resets_failed_group_to_pending(repo):
    run_id = "r1"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    make_integration_branch(repo, run_id)
    wt = make_group_worktree(repo, run_id, group, integration_branch(run_id))
    (wt / "g1.txt").write_text("coder work\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "g1 work")
    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.FAILED, failure="boom"))
    manifest_before = write_manifest(paths, run_id, group)

    retry_group(repo, run_id, group.id)

    persisted = RunState.model_validate_json(paths.state_path.read_text())
    entry = persisted.groups[group.id]
    assert entry.state == GroupState.PENDING
    assert entry.failure is None

    # branch and worktree survive — the whole point is to build on the work
    assert wt.is_dir()
    assert (wt / "g1.txt").is_file()

    manifest_after = ManifestStore(paths).load()
    assert manifest_after == manifest_before
    session = manifest_after.groups[group.id].sessions[0]
    assert session.retirement_reason is None


def test_retry_refreshes_branch_onto_integration_tip(repo):
    run_id = "r2"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    integration = make_integration_branch(repo, run_id)
    wt = make_group_worktree(repo, run_id, group, integration)
    (wt / "g1.txt").write_text("coder work\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "g1 work")

    # move the integration branch forward with an unrelated change
    integration_wt = repo.parent / "integration-checkout"
    git(repo, "worktree", "add", str(integration_wt), integration)
    (integration_wt / "new.txt").write_text("new\n")
    git(integration_wt, "add", ".")
    git(integration_wt, "commit", "-m", "integration moved on")

    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.FAILED, failure="boom"))

    retry_group(repo, run_id, group.id)

    branch = group_branch(run_id, group.id)
    result = subprocess.run(["git", "merge-base", "--is-ancestor", integration, branch], cwd=repo)
    assert result.returncode == 0


def test_retry_rejects_a_group_that_is_not_terminally_failed(repo):
    run_id = "r3"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.PENDING))
    before = paths.state_path.read_bytes()

    with pytest.raises(RetryError):
        retry_group(repo, run_id, group.id)

    assert paths.state_path.read_bytes() == before


def test_retry_conflict_leaves_state_and_branch_untouched(repo):
    run_id = "r4"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    initial_sha = git(repo, "rev-parse", "HEAD").strip()
    integration = make_integration_branch(repo, run_id)

    wt = make_group_worktree(repo, run_id, group, initial_sha)
    (wt / "base.txt").write_text("group change\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "group conflicting change")
    group_sha_before = git(wt, "rev-parse", "HEAD").strip()

    integration_wt = repo.parent / "integration-checkout"
    git(repo, "worktree", "add", str(integration_wt), integration)
    (integration_wt / "base.txt").write_text("integration change\n")
    git(integration_wt, "add", ".")
    git(integration_wt, "commit", "-m", "integration conflicting change")

    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.FAILED, failure="boom"))
    before = paths.state_path.read_bytes()

    with pytest.raises(RetryConflictError) as excinfo:
        retry_group(repo, run_id, group.id)
    assert "base.txt" in excinfo.value.paths

    assert paths.state_path.read_bytes() == before
    branch = group_branch(run_id, group.id)
    assert git(repo, "rev-parse", branch).strip() == group_sha_before


def test_retry_backs_up_state_before_writing(repo):
    run_id = "r5"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    integration = make_integration_branch(repo, run_id)
    make_group_worktree(repo, run_id, group, integration)
    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.FAILED, failure="boom"))
    pre_retry = paths.state_path.read_bytes()

    retry_group(repo, run_id, group.id)

    backups_dir = repo / ".orchestrator" / "backups"
    matches = list(backups_dir.rglob("state.json"))
    assert matches, "expected a state.json backup"
    assert matches[0].read_bytes() == pre_retry


# --------------------------------------------------------------- quarantine


def test_retry_clears_quarantine_and_a_following_resume_reenters_it(repo):
    run_id = "r6"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    integration = make_integration_branch(repo, run_id)
    make_group_worktree(repo, run_id, group, integration)
    write_state(
        paths,
        run_id,
        group.id,
        GroupRunState(
            state=GroupState.INTERRUPTED, quarantined=True, reentry_count=9, failure="quarantined"
        ),
    )

    retry_group(repo, run_id, group.id)

    persisted = RunState.model_validate_json(paths.state_path.read_text())
    entry = persisted.groups[group.id]
    assert entry.quarantined is False
    assert entry.reentry_count == 0
    assert entry.state == GroupState.INTERRUPTED  # retry itself does not re-enter it

    async def completing(ctx):
        return GroupState.COMPLETED

    import asyncio

    scheduler = Scheduler(groups=[group], paths=paths, executor=completing, resume=True)
    states = asyncio.run(scheduler.run())
    assert states == {group.id: GroupState.COMPLETED}


def test_retry_rejects_non_quarantined_interrupted_group(repo):
    run_id = "r7"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.INTERRUPTED))
    before = paths.state_path.read_bytes()

    with pytest.raises(RetryError):
        retry_group(repo, run_id, group.id)
    assert paths.state_path.read_bytes() == before


# ------------------------------------------------------------------ liveness


def test_retry_refuses_while_a_driver_holds_the_lock(repo):
    run_id = "r8"
    group = make_group("g1")
    paths = RunPaths(repo, run_id)
    write_grouping(paths, group)
    integration = make_integration_branch(repo, run_id)
    make_group_worktree(repo, run_id, group, integration)
    write_state(paths, run_id, group.id, GroupRunState(state=GroupState.FAILED, failure="boom"))
    before = paths.state_path.read_bytes()

    lock = DriverLock(paths)
    lock.acquire()
    try:
        with pytest.raises(RetryError, match=run_id):
            retry_group(repo, run_id, group.id)
    finally:
        lock.release()

    assert paths.state_path.read_bytes() == before
