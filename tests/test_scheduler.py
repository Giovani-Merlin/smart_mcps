"""U6 tests: dependency-aware scheduling, crash resume, watchdogs (plan Phase B).

Executors are in-process stubs — the scheduler never touches the CLI here.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import sys
import time

import pytest

from orchestrator.config import ExecutionConfig
from orchestrator.execution.escalation import EscalationPolicy
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.review import GroupFailure
from orchestrator.execution.scheduler import (
    TERMINAL_STATES,
    GroupRunState,
    GroupState,
    HoldReason,
    NoProgressError,
    ResolveConflict,
    ResolveDeps,
    RunAbort,
    RunState,
    Scheduler,
    SchedulerError,
)
from orchestrator.execution.sessions import ReportError, SessionError
from orchestrator.execution.worktrees import WorktreeError, WorktreeRefreshConflict
from orchestrator.grouping.llm import LlmProcessError
from orchestrator.model import (
    EscalationRequest,
    EscalationResponse,
    Group,
    HumanAction,
    PermissionDenied,
    ReviewIntensity,
)


def make_group(gid: str, deps: list[str] | None = None, files: list[str] | None = None) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=deps or [],
        files=files or [],
    )


async def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


def completing_executor(started: list[str] | None = None):
    async def executor(ctx):
        if started is not None:
            started.append(ctx.group.id)
        return GroupState.COMPLETED

    return executor


@pytest.mark.asyncio
async def test_independent_groups_run_concurrently_up_to_the_cap(tmp_path):
    started: list[str] = []
    peak = 0
    concurrent = 0
    gate = asyncio.Event()

    async def executor(ctx):
        nonlocal peak, concurrent
        started.append(ctx.group.id)
        concurrent += 1
        peak = max(peak, concurrent)
        await gate.wait()
        concurrent -= 1
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[make_group(f"g{i}") for i in range(1, 5)],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        config=ExecutionConfig(concurrency=3),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: len(started) == 3)
    await asyncio.sleep(0.05)
    assert len(started) == 3  # the fourth waits for a slot
    gate.set()
    states = await run
    assert peak == 3
    assert set(states.values()) == {GroupState.COMPLETED}


@pytest.mark.asyncio
async def test_pending_group_ids_names_groups_not_yet_started(tmp_path):
    """Plan U7: what an escalation's stdout line names as blocked — a group's
    dependents held by the DAG, or a not-yet-launched sibling under
    concurrency=1, are still PENDING while the running group is in flight."""
    gate = asyncio.Event()

    async def executor(ctx):
        if ctx.group.id == "g1":
            await gate.wait()
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"]), make_group("g3")],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        config=ExecutionConfig(concurrency=1),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: scheduler.state.groups["g1"].state == GroupState.RUNNING)
    assert scheduler.pending_group_ids() == ["g2", "g3"]
    gate.set()
    await run
    assert scheduler.pending_group_ids() == []


@pytest.mark.asyncio
async def test_dependent_group_launches_only_after_upstream_completes(tmp_path):
    events: list[tuple[str, str]] = []

    async def executor(ctx):
        events.append((ctx.group.id, "start"))
        await asyncio.sleep(0.02)
        events.append((ctx.group.id, "end"))
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        config=ExecutionConfig(concurrency=3),
    )
    await scheduler.run()
    assert events.index(("g1", "end")) < events.index(("g2", "start"))


@pytest.mark.asyncio
async def test_sequential_mode_runs_strictly_one_at_a_time_in_topo_order(tmp_path):
    events: list[tuple[str, str]] = []

    async def executor(ctx):
        events.append((ctx.group.id, "start"))
        await asyncio.sleep(0.01)
        events.append((ctx.group.id, "end"))
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[make_group("g3"), make_group("g1"), make_group("g2")],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        config=ExecutionConfig(concurrency=3, sequential=True),  # R25
    )
    await scheduler.run()
    assert events == [
        ("g1", "start"),
        ("g1", "end"),
        ("g2", "start"),
        ("g2", "end"),
        ("g3", "start"),
        ("g3", "end"),
    ]


@pytest.mark.asyncio
async def test_crash_and_resume_continues_without_relaunching_completed_groups(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    runs: dict[str, int] = {"g1": 0, "g2": 0}

    async def hanging_executor(ctx):
        runs[ctx.group.id] += 1
        if ctx.group.id == "g1":
            return GroupState.COMPLETED
        await asyncio.Event().wait()  # g2 hangs until the "crash"
        return GroupState.COMPLETED

    first = Scheduler(
        groups=[make_group("g1"), make_group("g2")],
        paths=paths,
        executor=hanging_executor,
        config=ExecutionConfig(concurrency=2),
    )
    run = asyncio.create_task(first.run())
    await wait_until(
        lambda: (
            first.state.groups["g1"].state == GroupState.COMPLETED
            and first.state.groups["g2"].state == GroupState.RUNNING
        )
    )
    run.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run

    # The state file alone must carry the resume.
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.COMPLETED
    assert persisted.groups["g2"].state == GroupState.RUNNING

    second = Scheduler(
        groups=[make_group("g1"), make_group("g2")],
        paths=paths,
        executor=completing_executor(),
        config=ExecutionConfig(concurrency=2),
        resume=True,
    )
    states = await second.run()
    assert states == {"g1": GroupState.COMPLETED, "g2": GroupState.COMPLETED}
    assert runs["g1"] == 1  # completed groups are never relaunched


@pytest.mark.asyncio
async def test_resume_terminates_a_matching_orphaned_subprocess(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "sess-abc123"])
    state = RunState(
        run_id="r1",
        groups={"g1": GroupRunState(state=GroupState.RUNNING)},
        live_pids={orphan.pid: "--resume sess-abc123"},
    )
    atomic_write_text(paths.state_path, state.model_dump_json() + "\n")

    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=paths,
        executor=completing_executor(),
        resume=True,
    )
    states = await scheduler.run()
    assert states == {"g1": GroupState.COMPLETED}
    assert orphan.poll() is not None  # terminated before re-entering the session
    assert scheduler.state.live_pids == {}


@pytest.mark.asyncio
async def test_resume_never_kills_a_reused_pid_with_a_different_cmdline(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    state = RunState(
        run_id="r1",
        groups={"g1": GroupRunState(state=GroupState.RUNNING)},
        live_pids={bystander.pid: "--resume sess-zzz999"},
    )
    atomic_write_text(paths.state_path, state.model_dump_json() + "\n")
    try:
        scheduler = Scheduler(
            groups=[make_group("g1")],
            paths=paths,
            executor=completing_executor(),
            resume=True,
        )
        await scheduler.run()
        assert bystander.poll() is None  # cmdline matched neither session nor claude
    finally:
        bystander.kill()
        bystander.wait()


@pytest.mark.asyncio
async def test_executor_exception_fails_the_group_and_records_the_failure(tmp_path):
    """An unrecognised exception is INTERRUPTED, not terminal FAILED (plan U1,
    R1/R2 inverted): the orchestrator has no basis for judging the work when it
    does not recognise what broke — only GroupFailure/ReportError are work
    judgements."""

    async def executor(ctx):
        raise RuntimeError("coder produced no diff")

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
    )
    states = await scheduler.run()  # returns: stranded dependents are not a wedge
    assert states["g1"] == GroupState.INTERRUPTED
    assert states["g2"] == GroupState.PENDING
    entry = scheduler.state.groups["g1"]
    assert entry.failure == "RuntimeError: coder produced no diff"


# ------------------------------------------------------- interrupted (R1–R3)


def test_interrupted_is_a_known_non_terminal_state():
    assert GroupState.INTERRUPTED.value == "interrupted"
    assert GroupState.INTERRUPTED not in TERMINAL_STATES
    assert TERMINAL_STATES == frozenset(
        {GroupState.COMPLETED, GroupState.FAILED, GroupState.RESOLVED}
    )


@pytest.mark.asyncio
async def test_session_error_marks_the_group_interrupted_with_failure_text(tmp_path):
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise SessionError("claude exited 1 (--resume sess-1)")

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()  # returns cleanly: interrupted is not a wedge
    assert states["g1"] == GroupState.INTERRUPTED
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.INTERRUPTED
    assert persisted.groups["g1"].failure == "SessionError: claude exited 1 (--resume sess-1)"


@pytest.mark.asyncio
async def test_llm_process_error_marks_the_group_interrupted_not_failed(tmp_path):
    """A usage limit on the one-shot `claude -p` path must resume like one on the
    session path. On run r20260726-grouping a single limit interrupted g5/g7 via
    SessionError but wedged g6 in terminal FAILED via LlmError, where `resume`
    could never reach it again."""
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise LlmProcessError("claude -p failed (1): ")

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.INTERRUPTED
    assert persisted.groups["g1"].failure == "LlmProcessError: claude -p failed (1): "


@pytest.mark.asyncio
async def test_permission_denied_marks_the_group_interrupted_with_command_verbatim(tmp_path):
    """Plan U3: a typed denial is an envelope failure like SessionError/
    LlmProcessError above, not a work failure — `resume` must reach it again,
    and the denied command must survive verbatim in the failure text."""
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise PermissionDenied("group g1 denied command: rm -rf /some/protected/path")

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.INTERRUPTED
    assert (
        persisted.groups["g1"].failure
        == "PermissionDenied: group g1 denied command: rm -rf /some/protected/path"
    )


@pytest.mark.asyncio
async def test_refresh_conflict_marks_the_group_interrupted_naming_paths_and_is_resumable(
    tmp_path,
):
    """Plan U6: a real content conflict on a resumed group's refresh is not lost
    work — it must stay reachable by `resume`, unlike a terminal WorktreeError."""
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise WorktreeRefreshConflict(
            "refreshing group g1's worktree onto main conflicted on: README.md"
        )

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.INTERRUPTED
    assert "README.md" in persisted.groups["g1"].failure
    assert GroupState.INTERRUPTED not in TERMINAL_STATES

    async def resumed_executor(ctx):
        return GroupState.COMPLETED

    resumed = Scheduler(
        groups=[make_group("g1")], paths=paths, executor=resumed_executor, resume=True
    )
    final_states = await resumed.run()
    assert final_states["g1"] == GroupState.COMPLETED


@pytest.mark.asyncio
async def test_other_worktree_error_still_marks_the_group_interrupted(tmp_path):
    """A plain WorktreeError (e.g. path exists but is not a worktree) is an
    envelope failure like any other unrecognised exception (plan U1 inversion):
    INTERRUPTED, not terminal FAILED, so a plain `resume` re-enters it."""
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise WorktreeError("/some/path exists but is not a worktree on branch-x")

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED
    entry = scheduler.state.groups["g1"]
    assert entry.failure == "WorktreeError: /some/path exists but is not a worktree on branch-x"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        ReportError("no valid report block after 2 nudges"),
        GroupFailure("coder blocked: missing dependency"),
    ],
    ids=[
        "report_error_is_a_work_failure",
        "group_failure_is_a_work_failure",
    ],
)
async def test_work_failures_still_mark_the_group_failed(tmp_path, exc):
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise exc

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.FAILED
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.FAILED
    assert persisted.groups["g1"].failure == f"{type(exc).__name__}: {exc}"


@pytest.mark.asyncio
async def test_llm_validation_exhaustion_is_now_interrupted_not_failed(tmp_path):
    """Under the plan U1 inversion only GroupFailure/ReportError are terminal
    work judgements — a plain LlmError (mapper validation exhaustion) is an
    exception the orchestrator does not otherwise recognise, so it is
    INTERRUPTED like everything else in the envelope, not FAILED."""
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        raise LlmError("mapper output failed validation after 3 attempts: bad JSON")

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED


@pytest.mark.asyncio
async def test_dependent_of_interrupted_group_stays_pending_and_run_returns(tmp_path):
    async def executor(ctx):
        raise SessionError("API connection dropped")

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
    )
    states = await scheduler.run()  # no NoProgressError: stranded, not wedged
    assert states["g1"] == GroupState.INTERRUPTED
    assert states["g2"] == GroupState.PENDING


@pytest.mark.asyncio
async def test_classification_writes_lifecycle_log_lines(tmp_path):
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        if ctx.group.id == "g1":
            raise SessionError("claude exited 1")
        raise GroupFailure("coder blocked")

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2")],
        paths=paths,
        executor=executor,
        config=ExecutionConfig(concurrency=2),
    )
    await scheduler.run()
    lines = paths.event_log_path.read_text().splitlines()
    assert any(
        line.endswith("group g1: interrupted (SessionError: claude exited 1)") for line in lines
    )
    assert any(line.endswith("group g2: failed (GroupFailure: coder blocked)") for line in lines)
    # Plain timestamped append-lines, the existing log_event format (R12).
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}T[^ ]+  ", line) for line in lines)


@pytest.mark.asyncio
async def test_resume_relaunches_an_interrupted_group(tmp_path):
    # `resume` marks non-terminal, non-pending groups ready; INTERRUPTED rides
    # that path — the re-entry group (u2) counts on it.
    paths = RunPaths(tmp_path, "r1")
    state = RunState(
        run_id="r1",
        groups={"g1": GroupRunState(state=GroupState.INTERRUPTED, failure="SessionError: x")},
    )
    atomic_write_text(paths.state_path, state.model_dump_json() + "\n")

    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=paths,
        executor=completing_executor(),
        resume=True,
    )
    states = await scheduler.run()
    assert states == {"g1": GroupState.COMPLETED}


@pytest.mark.asyncio
async def test_completing_after_a_resume_clears_the_stale_failure_text(tmp_path):
    # Plan U8: set_state wrote `failure` only when non-None, so the text from an
    # earlier interrupted attempt survived into a later successful completion —
    # `status` would print a failure line for a group recorded as completed.
    paths = RunPaths(tmp_path, "r1")
    state = RunState(
        run_id="r1",
        groups={"g1": GroupRunState(state=GroupState.INTERRUPTED, failure="SessionError: x")},
    )
    atomic_write_text(paths.state_path, state.model_dump_json() + "\n")

    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=paths,
        executor=completing_executor(),
        resume=True,
    )
    states = await scheduler.run()
    assert states == {"g1": GroupState.COMPLETED}
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].failure is None


@pytest.mark.asyncio
async def test_no_progress_watchdog_aborts_a_wedged_run_naming_blocked_groups(tmp_path):
    # A dependency cycle sneaking past partition-time detection must fail loudly.
    scheduler = Scheduler(
        groups=[make_group("g1", deps=["g2"]), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=completing_executor(),
    )
    with pytest.raises(NoProgressError, match=r"g1.*g2"):
        await scheduler.run()


@pytest.mark.asyncio
async def test_dependent_stays_pending_while_upstream_is_rewriting(tmp_path):
    # AE5 (wait half): rewriting is not completion; downstream must not launch.
    started: list[str] = []
    release = asyncio.Event()

    async def executor(ctx):
        started.append(ctx.group.id)
        if ctx.group.id == "g1":
            ctx.set_state(GroupState.REWRITING)
            await release.wait()
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        config=ExecutionConfig(concurrency=3),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: scheduler.state.groups["g1"].state == GroupState.REWRITING)
    await asyncio.sleep(0.05)
    assert scheduler.state.groups["g2"].state == GroupState.PENDING
    assert started == ["g1"]
    release.set()
    states = await run
    assert set(states.values()) == {GroupState.COMPLETED}


@pytest.mark.asyncio
async def test_executor_generation_updates_persist_to_the_state_file(tmp_path):
    paths = RunPaths(tmp_path, "r1")

    async def executor(ctx):
        ctx.set_generation(ctx.generation + 1)
        return GroupState.COMPLETED

    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=executor)
    await scheduler.run()
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.groups["g1"].generation == 2


@pytest.mark.asyncio
async def test_state_file_shows_transitions_in_order(tmp_path):
    transitions: list[tuple[str, GroupState]] = []
    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=completing_executor(),
    )
    original = scheduler.set_state

    def recording(gid, state, **kwargs):
        transitions.append((gid, state))
        original(gid, state, **kwargs)

    scheduler.set_state = recording
    await scheduler.run()
    assert transitions == [
        ("g1", GroupState.READY),
        ("g1", GroupState.RUNNING),
        ("g1", GroupState.COMPLETED),
        ("g2", GroupState.READY),
        ("g2", GroupState.RUNNING),
        ("g2", GroupState.COMPLETED),
    ]


def test_unknown_dependency_is_rejected_at_construction(tmp_path):
    with pytest.raises(SchedulerError, match="unknown"):
        Scheduler(
            groups=[make_group("g1", deps=["nope"])],
            paths=RunPaths(tmp_path, "r1"),
            executor=completing_executor(),
        )


# --------------------------------------------------- U2: resolve + overlap gate


class StubResolve:
    """Records what the resolve routine did instead of touching real git."""

    def __init__(self, *, commits_ahead: int = 1, conflict: bool = False):
        self.commits_ahead_value = commits_ahead
        self.conflict = conflict
        self.committed: list[str] = []
        self.merged: list[str] = []

    def commit_stranded(self, group: Group) -> bool:
        self.committed.append(group.id)
        return True

    def commits_ahead(self, group: Group) -> int:
        return self.commits_ahead_value

    def merge_group(self, group: Group) -> None:
        if self.conflict:
            raise ResolveConflict(f"conflict merging {group.id}")
        self.merged.append(group.id)

    def deps(self) -> ResolveDeps:
        return ResolveDeps(
            commit_stranded=self.commit_stranded,
            commits_ahead=self.commits_ahead,
            merge_group=self.merge_group,
        )


class StubBroker:
    """Canned operator: returns a scripted response and records every request."""

    def __init__(self, response: EscalationResponse | None):
        self.response = response
        self.raised: list[EscalationRequest] = []
        self.aborted = False

    def raise_escalation(self, request: EscalationRequest) -> EscalationResponse | None:
        self.raised.append(request)
        return self.response

    def trigger_abort(self) -> None:
        self.aborted = True


def failing_executor(exc: Exception):
    async def executor(ctx):
        raise exc

    return executor


def _seed_state(paths: RunPaths, groups: dict[str, GroupRunState]) -> None:
    atomic_write_text(
        paths.state_path, RunState(run_id=paths.run_id, groups=groups).model_dump_json()
    )


@pytest.mark.asyncio
async def test_failed_group_with_stranded_commits_resolves_autonomously(tmp_path):
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("coder crashed mid-round")),
        resolve=resolve.deps(),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.RESOLVED  # never completed — no review verdict
    assert resolve.committed == ["g1"]
    assert resolve.merged == ["g1"]
    persisted = RunState.model_validate_json(scheduler.paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.RESOLVED
    assert persisted.groups["g1"].resolve_settled is True
    assert "GroupFailure" in persisted.groups["g1"].failure


@pytest.mark.asyncio
async def test_failed_group_with_nothing_lost_stays_failed_and_settles(tmp_path):
    resolve = StubResolve(commits_ahead=0)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.FAILED
    assert resolve.committed == ["g1"]  # commit is still attempted
    assert resolve.merged == []  # nothing ahead — merge never attempted
    persisted = RunState.model_validate_json(scheduler.paths.state_path.read_text())
    assert persisted.groups["g1"].state == GroupState.FAILED
    assert persisted.groups["g1"].resolve_settled is True


@pytest.mark.asyncio
async def test_resolve_conflict_stops_the_run(tmp_path):
    resolve = StubResolve(commits_ahead=1, conflict=True)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
    )
    with pytest.raises(ResolveConflict):
        await scheduler.run()


@pytest.mark.asyncio
async def test_scheduler_without_resolve_deps_leaves_failed_group_unchanged(tmp_path):
    # Byte-identical to pre-U2 behaviour when resolve isn't wired at all.
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.FAILED


@pytest.mark.asyncio
async def test_interrupted_group_is_never_resolved(tmp_path):
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(SessionError("claude exited 1")),
        resolve=resolve.deps(),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED
    assert resolve.committed == []
    assert resolve.merged == []


# ------------------------------------------------- U2: escalation-driven resolve


@pytest.mark.asyncio
async def test_escalation_names_the_failed_group_overlap_and_successors(tmp_path):
    broker = StubBroker(EscalationResponse(id="x", action=HumanAction.SKIP))
    resolve = StubResolve(commits_ahead=1)

    async def executor(ctx):
        if ctx.group.id == "g1":
            raise GroupFailure("boom")
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[
            make_group("g1", files=["shared.py"]),
            make_group("g2", files=["shared.py", "g2.py"]),
            make_group("g3", files=["unrelated.py"]),
        ],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        # U2's overlap-gate semantics under test here, not R41's halt (plan U3):
        # g3 shares no declared file with g1 and must complete regardless.
        config=ExecutionConfig(on_group_failure="overlap"),
        resolve=resolve.deps(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "workers_via_orchestrator"),
    )
    states = await scheduler.run()
    assert states == {
        "g1": GroupState.FAILED,
        "g2": GroupState.COMPLETED,
        "g3": GroupState.COMPLETED,
    }
    assert len(broker.raised) == 1
    request = broker.raised[0]
    assert request.kind.value == "group_resolve"
    assert request.group_id == "g1"
    assert "g1" in request.prompt
    assert "g2" in request.prompt
    assert "shared.py" in request.prompt
    assert "g3" not in request.prompt  # no overlap with g3 — not named
    assert resolve.merged == []  # operator declined — SKIP never merges


@pytest.mark.asyncio
async def test_escalation_skip_leaves_the_group_failed_and_settled(tmp_path):
    broker = StubBroker(EscalationResponse(id="x", action=HumanAction.SKIP))
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "workers_via_orchestrator"),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.FAILED
    persisted = RunState.model_validate_json(scheduler.paths.state_path.read_text())
    assert persisted.groups["g1"].resolve_settled is True


@pytest.mark.asyncio
async def test_escalation_answer_delegates_to_autonomous_resolve(tmp_path):
    broker = StubBroker(EscalationResponse(id="x", action=HumanAction.ANSWER, answer="go ahead"))
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "workers_via_orchestrator"),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.RESOLVED
    assert resolve.merged == ["g1"]


@pytest.mark.asyncio
async def test_escalation_abort_raises_run_abort(tmp_path):
    broker = StubBroker(EscalationResponse(id="x", action=HumanAction.ABORT))
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "workers_via_orchestrator"),
    )
    with pytest.raises(RunAbort):
        await scheduler.run()
    assert broker.aborted is True


@pytest.mark.asyncio
async def test_escalation_timeout_falls_back_to_autonomous_resolve(tmp_path):
    broker = StubBroker(None)  # mirrors on_timeout=autonomous
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "workers_via_orchestrator"),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.RESOLVED


@pytest.mark.asyncio
async def test_policy_not_covering_group_resolve_falls_back_to_autonomous(tmp_path):
    broker = StubBroker(EscalationResponse(id="x", action=HumanAction.SKIP))
    resolve = StubResolve(commits_ahead=1)
    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=RunPaths(tmp_path, "r1"),
        executor=failing_executor(GroupFailure("boom")),
        resolve=resolve.deps(),
        broker=broker,
        policy=EscalationPolicy("autonomous", "workers_via_orchestrator"),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.RESOLVED  # autonomous tier never escalates
    assert broker.raised == []


# ---------------------------------------------------------- U2: overlap holds


@pytest.mark.asyncio
async def test_overlap_holds_a_pending_successor_until_the_failed_group_settles(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    _seed_state(
        paths,
        {
            "g1": GroupRunState(state=GroupState.FAILED, failure="boom"),
            "g2": GroupRunState(state=GroupState.PENDING),
        },
    )
    started: list[str] = []
    scheduler = Scheduler(
        groups=[make_group("g1", files=["shared.py"]), make_group("g2", files=["shared.py"])],
        paths=paths,
        executor=completing_executor(started),
        resume=True,
    )
    states = await scheduler.run()
    assert started == []  # g2 never ran — held by g1's unsettled failure
    assert states["g1"] == GroupState.FAILED
    assert states["g2"] == GroupState.PENDING


@pytest.mark.asyncio
async def test_settled_failed_group_no_longer_holds_its_overlap(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    _seed_state(
        paths,
        {
            "g1": GroupRunState(state=GroupState.FAILED, failure="boom", resolve_settled=True),
            "g2": GroupRunState(state=GroupState.PENDING),
        },
    )
    started: list[str] = []
    scheduler = Scheduler(
        groups=[make_group("g1", files=["shared.py"]), make_group("g2", files=["shared.py"])],
        paths=paths,
        executor=completing_executor(started),
        # U2's overlap-gate semantics under test here, not R41's halt (plan U3):
        # halt does not consult resolve_settled, so it would keep g2 held.
        config=ExecutionConfig(on_group_failure="overlap"),
        resume=True,
    )
    states = await scheduler.run()
    assert states["g2"] == GroupState.COMPLETED
    assert started == ["g2"]


@pytest.mark.asyncio
async def test_interrupted_group_holds_only_overlapping_successors(tmp_path):
    started: list[str] = []

    async def executor(ctx):
        if ctx.group.id == "g1":
            raise SessionError("claude exited 1")
        started.append(ctx.group.id)
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[
            make_group("g1", files=["shared.py"]),
            make_group("g2", files=["shared.py"]),
            make_group("g3", files=["unrelated.py"]),
        ],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        # g3 shares no declared file with g1 and must complete regardless — the
        # U9 exclusion under test here, not R41's halt (plan U3).
        config=ExecutionConfig(concurrency=1, on_group_failure="overlap"),
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.INTERRUPTED
    assert states["g3"] == GroupState.COMPLETED
    assert states["g2"] == GroupState.PENDING  # held, never ran
    assert "g2" not in started
    assert "g3" in started


@pytest.mark.asyncio
async def test_groups_with_no_overlap_are_unaffected_by_a_sibling_failure(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    _seed_state(
        paths,
        {
            "g1": GroupRunState(state=GroupState.FAILED, failure="boom"),
            "g2": GroupRunState(state=GroupState.PENDING),
        },
    )
    started: list[str] = []
    scheduler = Scheduler(
        groups=[make_group("g1", files=["a.py"]), make_group("g2", files=["b.py"])],
        paths=paths,
        executor=completing_executor(started),
        config=ExecutionConfig(on_group_failure="overlap"),
        resume=True,
    )
    states = await scheduler.run()
    assert states["g2"] == GroupState.COMPLETED
    assert started == ["g2"]


# --------------------------------------------------- U9: conflict exclusion


def _tracking_executor(gate: asyncio.Event, live: set[str], overlaps: list[frozenset[str]]):
    """Holds every group open on ``gate`` and records the set of groups live at
    the moment each one starts, so an illegal concurrent pair is caught even if
    it lasts microseconds."""

    async def executor(ctx):
        live.add(ctx.group.id)
        overlaps.append(frozenset(live))
        await gate.wait()
        live.discard(ctx.group.id)
        return GroupState.COMPLETED

    return executor


@pytest.mark.asyncio
async def test_overlapping_groups_never_run_concurrently_at_high_concurrency(tmp_path):
    """The core U9 invariant: two groups sharing a declared file are never both
    running, even with slots free and the DAG leaving them unordered."""
    live: set[str] = set()
    overlaps: list[frozenset[str]] = []
    gate = asyncio.Event()

    scheduler = Scheduler(
        groups=[
            make_group("g1", files=["shared.py", "one.py"]),
            make_group("g2", files=["shared.py", "two.py"]),
            make_group("g3", files=["three.py"]),
        ],
        paths=RunPaths(tmp_path, "r1"),
        executor=_tracking_executor(gate, live, overlaps),
        config=ExecutionConfig(concurrency=4),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: len(live) == 2)  # g1 and g3; g2 is excluded by g1
    await asyncio.sleep(0.05)
    assert live == {"g1", "g3"}
    gate.set()
    states = await run

    assert set(states.values()) == {GroupState.COMPLETED}
    assert not any({"g1", "g2"} <= snapshot for snapshot in overlaps)


@pytest.mark.asyncio
async def test_groups_sharing_no_file_still_run_concurrently(tmp_path):
    """The exclusion holds back real overlaps only — it must not serialize a run."""
    peak = 0
    concurrent = 0
    gate = asyncio.Event()

    async def executor(ctx):
        nonlocal peak, concurrent
        concurrent += 1
        peak = max(peak, concurrent)
        await gate.wait()
        concurrent -= 1
        return GroupState.COMPLETED

    scheduler = Scheduler(
        groups=[make_group(f"g{i}", files=[f"{i}.py"]) for i in range(1, 5)],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
        config=ExecutionConfig(concurrency=4),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: concurrent == 4)
    gate.set()
    await run
    assert peak == 4


@pytest.mark.asyncio
async def test_either_admission_order_completes_both_overlapping_groups(tmp_path):
    """No ordering is required between two overlapping groups: whichever the
    scheduler admits first, both run exactly once and both reach completed."""
    for groups in (
        [make_group("g1", files=["shared.py"]), make_group("g2", files=["shared.py"])],
        [make_group("g2", files=["shared.py"]), make_group("g1", files=["shared.py"])],
    ):
        started: list[str] = []
        scheduler = Scheduler(
            groups=groups,
            paths=RunPaths(tmp_path / "".join(g.id for g in groups), "r1"),
            executor=completing_executor(started),
            config=ExecutionConfig(concurrency=4),
        )
        states = await scheduler.run()
        assert states == {"g1": GroupState.COMPLETED, "g2": GroupState.COMPLETED}
        assert sorted(started) == ["g1", "g2"]


@pytest.mark.asyncio
async def test_file_overlap_creates_no_dependency_edge(tmp_path):
    """Exclusion is symmetric and transient — it must never become a DAG edge."""
    groups = [
        make_group("g1", files=["shared.py"]),
        make_group("g2", files=["shared.py"]),
    ]
    scheduler = Scheduler(
        groups=groups,
        paths=RunPaths(tmp_path, "r1"),
        executor=completing_executor(),
        config=ExecutionConfig(concurrency=4),
    )
    await scheduler.run()
    assert all(group.dependencies == [] for group in scheduler.groups.values())
    assert scheduler._dependents == {"g1": [], "g2": []}
    assert scheduler._unmet_deps("g1") == [] and scheduler._unmet_deps("g2") == []


@pytest.mark.asyncio
async def test_an_overlap_hold_is_reported_distinctly_and_names_the_shared_files(tmp_path):
    """U9's hold reads differently from a DAG block and from U2's failure gate,
    and it names both the shared file(s) and the group holding the lock."""
    paths = RunPaths(tmp_path, "r1")
    live: set[str] = set()
    gate = asyncio.Event()

    scheduler = Scheduler(
        groups=[
            make_group("g1", files=["shared.py"]),
            make_group("g2", files=["shared.py", "extra.py"]),
            make_group("g3", deps=["g1"], files=["other.py"]),
        ],
        paths=paths,
        executor=_tracking_executor(gate, live, []),
        config=ExecutionConfig(concurrency=4),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: bool(scheduler.state.groups["g2"].holds))

    # Persisted, so `status` reads it straight out of the state file.
    persisted = RunState.model_validate_json(paths.state_path.read_text())
    overlap = persisted.groups["g2"].holds
    assert [hold.reason for hold in overlap] == [HoldReason.FILE_OVERLAP]
    assert overlap[0].group_id == "g1"
    assert overlap[0].files == ["shared.py"]  # only the shared one, not extra.py

    dag = persisted.groups["g3"].holds
    assert [hold.reason for hold in dag] == [HoldReason.DAG_DEPENDENCY]
    assert dag[0].group_id == "g1" and dag[0].files == []

    gate.set()
    await run
    # A group that has started or finished carries no stale hold.
    assert all(not entry.holds for entry in scheduler.state.groups.values())


@pytest.mark.asyncio
async def test_failure_gate_and_overlap_holds_are_distinguishable(tmp_path):
    """U2's gate (overlapping a *failed* group) and U9's exclusion (overlapping a
    *healthy in-flight* one) are different situations with different fixes."""
    paths = RunPaths(tmp_path, "r1")
    _seed_state(
        paths,
        {
            "g1": GroupRunState(state=GroupState.FAILED, failure="boom"),
            "g2": GroupRunState(state=GroupState.PENDING),
        },
    )
    scheduler = Scheduler(
        groups=[make_group("g1", files=["shared.py"]), make_group("g2", files=["shared.py"])],
        paths=paths,
        executor=completing_executor(),
        resume=True,
    )
    holds = scheduler._holds_on("g2")
    assert [hold.reason for hold in holds] == [HoldReason.FAILURE_GATE]
    assert holds[0].group_id == "g1" and holds[0].files == ["shared.py"]


@pytest.mark.asyncio
async def test_two_groups_creating_the_same_new_file_exclude_each_other(tmp_path):
    """Overlap is the union of existing and prospective files: a declared file
    that does not exist yet still excludes, with no filesystem lookup."""
    live: set[str] = set()
    overlaps: list[frozenset[str]] = []
    gate = asyncio.Event()

    scheduler = Scheduler(
        groups=[
            make_group("g1", files=["does/not/exist/yet.py"]),
            make_group("g2", files=["does/not/exist/yet.py"]),
        ],
        paths=RunPaths(tmp_path, "r1"),
        executor=_tracking_executor(gate, live, overlaps),
        config=ExecutionConfig(concurrency=4),
    )
    run = asyncio.create_task(scheduler.run())
    await wait_until(lambda: len(live) == 1)
    await asyncio.sleep(0.05)
    assert len(live) == 1
    gate.set()
    await run
    assert not any(len(snapshot) > 1 for snapshot in overlaps)


@pytest.mark.asyncio
async def test_serial_default_admits_exactly_as_before(tmp_path):
    """At concurrency 1 this unit changes nothing: the cap already excludes
    everything, so no group is ever held for overlap."""
    started: list[str] = []
    scheduler = Scheduler(
        groups=[
            make_group("g1", files=["shared.py"]),
            make_group("g2", files=["shared.py"]),
            make_group("g3", files=["shared.py"]),
        ],
        paths=RunPaths(tmp_path, "r1"),
        executor=completing_executor(started),
        config=ExecutionConfig(concurrency=1),
    )
    states = await scheduler.run()
    assert started == ["g1", "g2", "g3"]  # unchanged topo/id order
    assert set(states.values()) == {GroupState.COMPLETED}
    assert all(
        hold.reason != HoldReason.FILE_OVERLAP
        for entry in scheduler.state.groups.values()
        for hold in entry.holds
    )


@pytest.mark.asyncio
async def test_exclusion_survives_a_resume(tmp_path):
    """A run interrupted with one of two overlapping groups in flight must not
    admit the other before the first is re-entered and finished."""
    paths = RunPaths(tmp_path, "r1")
    groups = [make_group("g1", files=["shared.py"]), make_group("g2", files=["shared.py"])]

    hang = asyncio.Event()

    async def hanging_executor(ctx):
        await hang.wait()
        return GroupState.COMPLETED

    first = Scheduler(
        groups=groups,
        paths=paths,
        executor=hanging_executor,
        config=ExecutionConfig(concurrency=4),
    )
    run = asyncio.create_task(first.run())
    await wait_until(lambda: first.state.groups["g1"].state == GroupState.RUNNING)
    assert first.state.groups["g2"].state == GroupState.PENDING
    run.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run

    # Both come back READY on resume; the exclusion has to be re-derived from
    # live state, not from anything the crashed process left behind.
    live: set[str] = set()
    overlaps: list[frozenset[str]] = []
    gate = asyncio.Event()
    gate.set()
    second = Scheduler(
        groups=groups,
        paths=paths,
        executor=_tracking_executor(gate, live, overlaps),
        config=ExecutionConfig(concurrency=4),
        resume=True,
    )
    states = await second.run()
    assert states == {"g1": GroupState.COMPLETED, "g2": GroupState.COMPLETED}
    assert not any(len(snapshot) > 1 for snapshot in overlaps)


# ------------------------------------------------------- interrupt visibility


def test_mark_interrupted_stamps_the_run_and_survives_a_reread(tmp_path):
    """A killed run must not read like a live one.

    `live_pids` is empty in both cases (pids are registered only for a
    subprocess's lifetime), and mid-flight groups stay RUNNING, so before this
    marker the only way to tell a Ctrl-C from a healthy run was to diff
    `state.json`'s mtime against a worker transcript.
    """
    paths = RunPaths(tmp_path, "r1")
    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=completing_executor())
    assert scheduler.state.interrupted_at is None

    scheduler.set_state("g1", GroupState.RUNNING)
    scheduler.mark_interrupted()

    persisted = RunState.model_validate_json(paths.state_path.read_text())
    assert persisted.interrupted_at is not None
    assert persisted.groups["g1"].state is GroupState.RUNNING  # the group really was running
    assert persisted.live_pids == {}  # and this alone never distinguished the two


@pytest.mark.asyncio
async def test_resuming_clears_the_interrupt_marker(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    state = RunState(
        run_id="r1",
        groups={"g1": GroupRunState(state=GroupState.RUNNING)},
        interrupted_at="2026-08-12T06:00:00+00:00",
    )
    atomic_write_text(paths.state_path, state.model_dump_json() + "\n")

    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=paths,
        executor=completing_executor(),
        resume=True,
    )
    # Cleared as soon as a driver attaches, and persisted immediately — a reader
    # during the run must not still see the previous interrupt.
    assert scheduler.state.interrupted_at is None
    assert RunState.model_validate_json(paths.state_path.read_text()).interrupted_at is None
    assert await scheduler.run() == {"g1": GroupState.COMPLETED}


def test_a_broken_state_write_never_replaces_the_interrupt_with_a_traceback(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path, "r1")
    scheduler = Scheduler(groups=[make_group("g1")], paths=paths, executor=completing_executor())
    monkeypatch.setattr(scheduler, "_persist", lambda: (_ for _ in ()).throw(OSError("disk gone")))
    scheduler.mark_interrupted()  # must not raise
