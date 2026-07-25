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
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.review import GroupFailure
from orchestrator.execution.scheduler import (
    TERMINAL_STATES,
    GroupRunState,
    GroupState,
    NoProgressError,
    RunState,
    Scheduler,
    SchedulerError,
)
from orchestrator.execution.sessions import ReportError, SessionError
from orchestrator.model import Group, ReviewIntensity


def make_group(gid: str, deps: list[str] | None = None) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=deps or [],
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
    async def executor(ctx):
        raise RuntimeError("coder produced no diff")

    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=RunPaths(tmp_path, "r1"),
        executor=executor,
    )
    states = await scheduler.run()  # returns: stranded dependents are not a wedge
    assert states["g1"] == GroupState.FAILED
    assert states["g2"] == GroupState.PENDING
    entry = scheduler.state.groups["g1"]
    assert entry.failure == "RuntimeError: coder produced no diff"


# ------------------------------------------------------- interrupted (R1–R3)


def test_interrupted_is_a_known_non_terminal_state():
    assert GroupState.INTERRUPTED.value == "interrupted"
    assert GroupState.INTERRUPTED not in TERMINAL_STATES
    assert TERMINAL_STATES == frozenset({GroupState.COMPLETED, GroupState.FAILED})


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
@pytest.mark.parametrize(
    "exc",
    [
        ReportError("no valid report block after 2 nudges"),
        GroupFailure("coder blocked: missing dependency"),
    ],
    ids=["report_error_is_a_work_failure", "group_failure_is_a_work_failure"],
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
