"""Dependency-aware run core: asyncio state machine over the group DAG (plan U6).

Readiness updates incrementally on completion via a reverse-dependency map —
CoCoder's shared_task_list design minus the file lock, since a single orchestrator
process owns state (docs/research/cocoder-analysis.md §5, §8 point 4). Run state
persists after every transition so a crashed run resumes without relaunching
completed groups; the state file records live worker PIDs so resume can terminate
orphans before re-entering their sessions. The no-progress watchdog turns a wedged
run into a loud failure naming the blocked groups (§8 point 5).
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from orchestrator.config import ExecutionConfig
from orchestrator.execution.manifest import RunPaths, atomic_write_text, log_event
from orchestrator.execution.sessions import ReportError, SessionError
from orchestrator.model import Group


class SchedulerError(Exception):
    """The run cannot proceed."""


class NoProgressError(SchedulerError):
    """Nothing running, nothing ready, run not complete — a wedged run."""


class RunAbort(SchedulerError):
    """The operator aborted the whole run (plan Phase D). Unlike a group failure
    it is not swallowed into ``FAILED``: it propagates out of ``_run_group`` and
    ``run()`` (whose ``finally`` cancels in-flight tasks) so the CLI can report a
    clean, resumable stop rather than a wedge."""


class GroupState(StrEnum):
    """Group lifecycle (scheduler-owned; plan state diagram)."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    REVIEWING = "reviewing"
    REWRITING = "rewriting"
    MERGING = "merging"
    COMPLETED = "completed"
    FAILED = "failed"
    # Envelope failure (R1/R2): the harness died under the group, not the work.
    # Non-terminal, so a plain `resume` re-enters the group in its worktree.
    INTERRUPTED = "interrupted"


TERMINAL_STATES = frozenset({GroupState.COMPLETED, GroupState.FAILED})


class GroupRunState(BaseModel):
    state: GroupState = GroupState.PENDING
    generation: int = 1
    failure: str | None = None


class RunState(BaseModel):
    """Crash-resumable run snapshot, persisted after every transition."""

    run_id: str
    groups: dict[str, GroupRunState] = Field(default_factory=dict)
    live_pids: dict[int, str] = Field(default_factory=dict)  # pid → session context


@dataclass
class GroupContext:
    """What a group executor gets: its group plus persisted state hooks."""

    group: Group
    generation: int
    set_state: Callable[[GroupState], None]
    set_generation: Callable[[int], None]


# Runs one group to a terminal state (U7 wires the review loop in here).
Executor = Callable[[GroupContext], Awaitable[GroupState]]


class Scheduler:
    """Parallel independent groups, incremental readiness, resumable state."""

    def __init__(
        self,
        *,
        groups: list[Group],
        paths: RunPaths,
        executor: Executor,
        config: ExecutionConfig | None = None,
        resume: bool = False,
    ):
        self.groups = {group.id: group for group in groups}
        for group in groups:
            unknown = [dep for dep in group.dependencies if dep not in self.groups]
            if unknown:
                raise SchedulerError(f"group {group.id} depends on unknown groups: {unknown}")
        self.paths = paths
        self.executor = executor
        self.config = config or ExecutionConfig()
        self._resume = resume
        self._lock = threading.Lock()
        self._dependents: dict[str, list[str]] = {gid: [] for gid in self.groups}
        for group in groups:
            for dep in group.dependencies:
                self._dependents[dep].append(group.id)
        if resume:
            self.state = RunState.model_validate_json(self.paths.state_path.read_text())
        else:
            self.state = RunState(
                run_id=paths.run_id, groups={gid: GroupRunState() for gid in self.groups}
            )
            self._persist()
        self.tracker = _SchedulerPidTracker(self)

    # ------------------------------------------------------------- state

    def _persist(self) -> None:
        atomic_write_text(self.paths.state_path, self.state.model_dump_json(indent=2) + "\n")

    def set_state(self, group_id: str, state: GroupState, *, failure: str | None = None) -> None:
        with self._lock:
            entry = self.state.groups[group_id]
            entry.state = state
            if failure is not None:
                entry.failure = failure
            self._persist()

    def set_generation(self, group_id: str, generation: int) -> None:
        with self._lock:
            self.state.groups[group_id].generation = generation
            self._persist()

    def _record_pid(self, pid: int, context: str) -> None:
        with self._lock:
            self.state.live_pids[pid] = context
            self._persist()

    def _forget_pid(self, pid: int) -> None:
        with self._lock:
            self.state.live_pids.pop(pid, None)
            self._persist()

    # ------------------------------------------------------------- resume

    def _reap_orphans(self) -> None:
        """Terminate worker subprocesses that survived a crashed orchestrator.

        A recorded PID is killed only if its current cmdline still matches the
        recorded session context (or the claude binary) — PID reuse must never
        kill an innocent process.
        """
        for pid, context in list(self.state.live_pids.items()):
            if _cmdline_matches(pid, context):
                _terminate(pid)
        with self._lock:
            self.state.live_pids.clear()
            self._persist()

    # ------------------------------------------------------------- run

    async def run(self) -> dict[str, GroupState]:
        if self._resume:
            self._reap_orphans()
            for gid, entry in self.state.groups.items():
                # Anything mid-flight when the last process died restarts from
                # ready; its warm sessions live on in the manifest.
                if entry.state not in TERMINAL_STATES and entry.state != GroupState.PENDING:
                    self.set_state(gid, GroupState.READY)

        remaining = {
            gid: sum(
                1
                for dep in group.dependencies
                if self.state.groups[dep].state != GroupState.COMPLETED
            )
            for gid, group in self.groups.items()
        }
        ready: list[str] = sorted(
            gid
            for gid, entry in self.state.groups.items()
            if entry.state == GroupState.READY
            or (entry.state == GroupState.PENDING and remaining[gid] == 0)
        )
        for gid in ready:
            if self.state.groups[gid].state == GroupState.PENDING:
                self.set_state(gid, GroupState.READY)

        cap = 1 if self.config.sequential else self.config.concurrency
        in_flight: dict[asyncio.Task[GroupState], str] = {}

        try:
            while True:
                while ready and len(in_flight) < cap:
                    gid = ready.pop(0)
                    self.set_state(gid, GroupState.RUNNING)
                    task = asyncio.create_task(self._run_group(gid), name=f"group-{gid}")
                    in_flight[task] = gid

                if not in_flight:
                    # Interrupted groups are stopped-but-resumable, not wedged —
                    # they never count toward the no-progress watchdog.
                    blocked = [
                        gid
                        for gid, entry in self.state.groups.items()
                        if entry.state not in TERMINAL_STATES
                        and entry.state != GroupState.INTERRUPTED
                    ]
                    if not blocked:
                        return {gid: entry.state for gid, entry in self.state.groups.items()}
                    failed_reachable = self._blocked_by_failure()
                    if all(gid in failed_reachable for gid in blocked):
                        # Not a wedge: upstream failures legitimately strand dependents.
                        return {gid: entry.state for gid, entry in self.state.groups.items()}
                    raise NoProgressError(
                        "no progress possible: nothing running, nothing ready, "
                        f"blocked groups: {sorted(blocked)}"
                    )

                done, _ = await asyncio.wait(in_flight.keys(), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    gid = in_flight.pop(task)
                    # _run_group only ever raises RunAbort (operator abort); that
                    # propagates to the finally below, cancelling in-flight tasks.
                    final = task.result()
                    if final == GroupState.COMPLETED:
                        for dependent in sorted(self._dependents[gid]):
                            remaining[dependent] -= 1
                            entry = self.state.groups[dependent]
                            if remaining[dependent] == 0 and entry.state == GroupState.PENDING:
                                self.set_state(dependent, GroupState.READY)
                                ready.append(dependent)
                        ready.sort()
        finally:
            # Cancellation (ctrl-c, crash-in-test) must not leak group tasks;
            # persisted state carries the resume.
            for task in in_flight:
                task.cancel()

    async def _run_group(self, gid: str) -> GroupState:
        entry = self.state.groups[gid]
        context = GroupContext(
            group=self.groups[gid],
            generation=entry.generation,
            set_state=lambda state: self.set_state(gid, state),
            set_generation=lambda generation: self.set_generation(gid, generation),
        )
        try:
            final = await self.executor(context)
        except RunAbort:
            # An operator abort stops the run, not just this group; let it
            # propagate. The broker's abort event (set where RunAbort was raised)
            # has already released any blocked siblings so their tasks can cancel.
            raise
        except ReportError as exc:
            # A work failure despite its SessionError type (plan decision): the
            # report was judged only after the session's warm corrective nudges —
            # the harness was healthy, the agent failed.
            return self._classify(gid, GroupState.FAILED, f"{type(exc).__name__}: {exc}")
        except SessionError as exc:
            # Envelope failure (R1/R2): the claude process/API died under the
            # group, not the work — non-terminal so `resume` re-enters it.
            return self._classify(gid, GroupState.INTERRUPTED, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — a group failure must not kill the run
            return self._classify(gid, GroupState.FAILED, f"{type(exc).__name__}: {exc}")
        if final not in TERMINAL_STATES:
            return self._classify(
                gid, GroupState.FAILED, f"executor returned non-terminal state {final}"
            )
        self.set_state(gid, final)
        return final

    def _classify(self, gid: str, state: GroupState, failure: str) -> GroupState:
        """Record a failure-shaped outcome plus its lifecycle line (R1, R11)."""
        self.set_state(gid, state, failure=failure)
        log_event(self.paths, f"group {gid}: {state.value} ({failure})")
        return state

    def _blocked_by_failure(self) -> set[str]:
        """Groups with a failed or interrupted (transitive) ancestor — stranded,
        not wedged."""
        stranded: set[str] = set()
        frontier = [
            gid
            for gid, entry in self.state.groups.items()
            if entry.state in (GroupState.FAILED, GroupState.INTERRUPTED)
        ]
        while frontier:
            gid = frontier.pop()
            for dependent in self._dependents[gid]:
                if dependent not in stranded:
                    stranded.add(dependent)
                    frontier.append(dependent)
        return stranded


class _SchedulerPidTracker:
    """sessions.SubprocessTracker that persists live PIDs into the run state."""

    def __init__(self, scheduler: Scheduler):
        self._scheduler = scheduler

    def spawned(self, pid: int, context: str) -> None:
        self._scheduler._record_pid(pid, context)

    def exited(self, pid: int) -> None:
        self._scheduler._forget_pid(pid)


def _cmdline_matches(pid: int, context: str) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False  # process already gone
    cmdline = raw.replace(b"\0", b" ").decode(errors="replace")
    token = context.rsplit(" ", 1)[-1] if context else ""
    return (bool(token) and token in cmdline) or "claude" in cmdline


def _terminate(pid: int, grace_s: float = 2.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
