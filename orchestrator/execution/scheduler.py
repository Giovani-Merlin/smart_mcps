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
import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.escalation import EscalationBroker, EscalationPolicy
from orchestrator.execution.manifest import RunPaths, atomic_write_text, log_event
from orchestrator.execution.sessions import ReportError
from orchestrator.execution.worktrees import group_branch, worktree_path
from orchestrator.model import (
    EscalationKind,
    EscalationRequest,
    Group,
    HumanAction,
)

#: Bump when RunState's shape changes in a way an older orchestrator's
#: state.json cannot be safely re-interpreted by (plan U1, R38-adjacent):
#: fields added with a default are forward-compatible, but silently coercing
#: is exactly the failure mode this version check exists to turn loud.
RUN_STATE_SCHEMA_VERSION = 1


class RunStateVersionError(Exception):
    """``state.json``'s schema_version is not one this orchestrator supports."""

    def __init__(self, found: int, supported: int):
        super().__init__(
            f"state.json schema_version {found} is not supported by this orchestrator "
            f"(supports {supported}) — do not resume with a mismatched orchestrator version"
        )
        self.found = found
        self.supported = supported


class GroupFailure(Exception):
    """The group exhausted its bounds; surfaced to the operator, never retried.

    Lives here (plan U1 Decisions) rather than in ``review.py``, which raises
    it: ``review.py`` already imports ``scheduler.py`` for ``Executor`` and
    friends, so the reverse import would cycle. ``review.py`` imports this
    class from here.
    """


class SchedulerError(Exception):
    """The run cannot proceed."""


class NoProgressError(SchedulerError):
    """Nothing running, nothing ready, run not complete — a wedged run."""


class RunAbort(SchedulerError):
    """The operator aborted the whole run (plan Phase D). Unlike a group failure
    it is not swallowed into ``FAILED``: it propagates out of ``_run_group`` and
    ``run()`` (whose ``finally`` cancels in-flight tasks) so the CLI can report a
    clean, resumable stop rather than a wedge."""


class ResolveConflict(SchedulerError):
    """A FAILED group's resolve merge hit a real content conflict (plan U2).

    The run stops rather than silently dropping the group's stranded work or
    releasing overlapping successors onto a hole — U1's merge gate has already
    left the integration branch at its pre-merge SHA by the time this reaches
    ``run()``.
    """


class ResolvePreflightFailed(Exception):
    """A FAILED group's resolve merge was declined by Preflight (plan U5) —
    not a conflict, so unlike ``ResolveConflict`` this never stops the run:
    the group's committed work stays on its own branch, unmerged, and the
    group stays FAILED. Caught only inside ``_resolve_autonomously``, so it
    never needs to be a ``SchedulerError``."""


@dataclass
class ResolveDeps:
    """What the scheduler needs to resolve a FAILED group's stranded work (plan
    U2), fully injected so the scheduler never imports git/merge machinery
    directly — ``review.py`` already imports this module for ``Executor`` and
    friends, so a reverse import here would cycle.
    """

    commit_stranded: Callable[[Group], bool]
    commits_ahead: Callable[[Group], int]
    merge_group: Callable[[Group], None]  # raises ResolveConflict on a real conflict


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
    # A FAILED group's stranded work landed on the integration branch via the
    # resolve routine (plan U2) — distinct from COMPLETED because it never
    # claimed a review verdict; distinct from plain FAILED because the work is
    # not lost. Its own DAG dependents release, same as COMPLETED.
    RESOLVED = "resolved"
    # Envelope failure (R1/R2): the harness died under the group, not the work.
    # Non-terminal, so a plain `resume` re-enters the group in its worktree.
    INTERRUPTED = "interrupted"


TERMINAL_STATES = frozenset({GroupState.COMPLETED, GroupState.FAILED, GroupState.RESOLVED})

# A group with a live worktree it is still writing to (plan U9). Anything here
# excludes every not-yet-started group declaring a file in common: the two would
# otherwise edit the same file concurrently and collide at merge. INTERRUPTED and
# unsettled-FAILED are deliberately absent — U2's failure gate already holds
# overlapping groups against those, with different release semantics.
ACTIVE_STATES = frozenset(
    {GroupState.RUNNING, GroupState.REVIEWING, GroupState.REWRITING, GroupState.MERGING}
)


class HoldReason(StrEnum):
    """Why a group is not admissible right now (plan U9 keeps these distinct)."""

    DAG_DEPENDENCY = "dag_dependency"  # an upstream group has not completed
    FAILURE_GATE = "failure_gate"  # U2: overlaps a failed/interrupted group
    FILE_OVERLAP = "file_overlap"  # U9: overlaps a *healthy* in-flight group
    RUN_HALTED = "run_halted"  # U3/R41: on_group_failure=halt, admission paused


class GroupHold(BaseModel):
    """One reason one group is held, recorded at the moment the scheduler
    declined to admit it so ``status`` can report it verbatim rather than
    re-deriving the DAG (``status`` reads only the run state, not groups.json).
    """

    reason: HoldReason
    group_id: str  # the group holding the lock
    files: list[str] = Field(default_factory=list)  # shared files; overlap holds only


class GroupRunState(BaseModel):
    state: GroupState = GroupState.PENDING
    generation: int = 1
    failure: str | None = None
    # Why this group did not launch on the scheduler's last admission pass
    # (plan U9). Advisory reporting only — never an input to admission, which is
    # always recomputed fresh — and empty for anything already running or terminal.
    holds: list[GroupHold] = Field(default_factory=list)
    # plan U2: a FAILED group's resolve routine has completed (any outcome) —
    # its file-overlap hold on other groups clears. Persisted (not computed from
    # ``state`` alone) because a FAILED group is terminal: it never re-enters
    # ``_run_group`` on resume, so this flag is the only record that resolve
    # already ran and settled it.
    resolve_settled: bool = False
    # plan U1 Decisions: how many times this group has been re-entered after a
    # non-terminal (INTERRUPTED, or mid-flight-at-crash) outcome. Incremented on
    # each `resume` that re-enters it; a group that has never been re-entered
    # reads zero. Bounds the R1/R2 inversion — nothing counted this before.
    reentry_count: int = 0
    # Set once ``reentry_count`` reaches ``breaker.max_reentries`` (plan U1
    # Decisions): a plain `resume` stops re-entering this group automatically —
    # only the operator's `retry` clears it. Deliberately a flag, not a
    # ``GroupState`` member: quarantine is orthogonal to lifecycle (the group
    # stays INTERRUPTED) and every new GroupState has to be mirrored in the
    # Observatory's own enums, which has drifted before.
    quarantined: bool = False


class RunState(BaseModel):
    """Crash-resumable run snapshot, persisted after every transition."""

    run_id: str
    # Bumped when this model's shape changes incompatibly (plan U1, RunStateVersionError);
    # a resume across a schema break must fail loudly rather than coerce or drop fields.
    schema_version: int = RUN_STATE_SCHEMA_VERSION
    groups: dict[str, GroupRunState] = Field(default_factory=dict)
    live_pids: dict[int, str] = Field(default_factory=dict)  # pid → session context
    # Set when the process driving this run was interrupted, cleared when a
    # driver picks it up again. Without it a Ctrl-C leaves `state.json` reading
    # exactly like a healthy run — groups RUNNING, `live_pids` empty, because
    # pids are only registered for a subprocess's lifetime — and the only way to
    # tell a killed run from a live one is to diff file mtimes against a worker
    # transcript. This is deliberately a timestamp on the run, not a group state:
    # the groups really were running, and inventing an INTERRUPTED state for them
    # would collide with the classification the scheduler already owns.
    interrupted_at: str | None = None


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
        breaker: BreakerConfig | None = None,
        resume: bool = False,
        broker: EscalationBroker | None = None,
        policy: EscalationPolicy | None = None,
        resolve: ResolveDeps | None = None,
    ):
        self.groups = {group.id: group for group in groups}
        for group in groups:
            unknown = [dep for dep in group.dependencies if dep not in self.groups]
            if unknown:
                raise SchedulerError(f"group {group.id} depends on unknown groups: {unknown}")
        self.paths = paths
        self.executor = executor
        self.config = config or ExecutionConfig()
        self.breaker = breaker or BreakerConfig()
        self._resume = resume
        # HITL seam (plan U2), mirroring ReviewDeps: both None ⇒ a FAILED group's
        # resolve runs autonomously with no escalation, byte-identical to a run
        # with escalation disabled.
        self._broker = broker
        self._policy = policy
        self._resolve = resolve
        self._lock = threading.Lock()
        self._dependents: dict[str, list[str]] = {gid: [] for gid in self.groups}
        for group in groups:
            for dep in group.dependencies:
                self._dependents[dep].append(group.id)
        if resume:
            raw = json.loads(self.paths.state_path.read_text())
            found_version = raw.get("schema_version")
            if found_version != RUN_STATE_SCHEMA_VERSION:
                raise RunStateVersionError(found_version, RUN_STATE_SCHEMA_VERSION)
            self.state = RunState.model_validate(raw)
            # A driver is attached again, so the run is no longer interrupted —
            # whatever happens next writes its own marker.
            self.state.interrupted_at = None
            self._persist()
        else:
            self.state = RunState(
                run_id=paths.run_id, groups={gid: GroupRunState() for gid in self.groups}
            )
            self._persist()
        self.tracker = _SchedulerPidTracker(self)

    # ------------------------------------------------------------- state

    def _persist(self) -> None:
        atomic_write_text(self.paths.state_path, self.state.model_dump_json(indent=2) + "\n")

    def mark_interrupted(self) -> None:
        """Stamp the run as no longer driven. Best-effort: this runs on the way
        out after a Ctrl-C, and failing to record the fact must not replace the
        operator's interrupt with a traceback."""
        try:
            with self._lock:
                self.state.interrupted_at = datetime.now(UTC).isoformat(timespec="seconds")
            self._persist()
        except Exception:  # noqa: BLE001 - never obscure the interrupt itself
            pass

    def set_state(self, group_id: str, state: GroupState, *, failure: str | None = None) -> None:
        """``failure`` always overwrites (plan U8): a transition made with no
        failure text — READY on resume, RUNNING at the top of a fresh attempt —
        clears whatever an earlier attempt left behind, so a group that later
        reaches COMPLETED never carries a stale failure line into ``status``.
        Only ``_classify`` ever passes an explicit ``failure``.
        """
        with self._lock:
            entry = self.state.groups[group_id]
            entry.state = state
            entry.failure = failure
            if state not in (GroupState.PENDING, GroupState.READY):
                # A group that has started (or finished) is not waiting on
                # anything — a stale hold list would misreport it in `status`.
                entry.holds = []
            self._persist()

    def set_generation(self, group_id: str, generation: int) -> None:
        with self._lock:
            self.state.groups[group_id].generation = generation
            self._persist()

    def pending_group_ids(self) -> list[str]:
        """Groups not yet started or finished (plan U7): what a blocking
        escalation is holding up, named on the stdout line so an operator
        watching the run sees more than silence."""
        with self._lock:
            return sorted(
                gid
                for gid, entry in self.state.groups.items()
                if entry.state in (GroupState.PENDING, GroupState.READY)
            )

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

    def _reenter_on_resume(self) -> None:
        """Move anything the last process left mid-flight back to READY — unless
        it has already burned through ``breaker.max_reentries`` re-entries, in
        which case it is quarantined instead (plan U1 Decisions): a plain
        `resume` stops picking it up automatically and `retry` is what releases
        it. Already-quarantined groups are left untouched — the count does not
        keep climbing every idle `resume`.
        """
        with self._lock:
            for gid, entry in self.state.groups.items():
                if entry.state in TERMINAL_STATES or entry.state == GroupState.PENDING:
                    continue
                if entry.quarantined:
                    continue
                entry.reentry_count += 1
                if entry.reentry_count > self.breaker.max_reentries:
                    entry.quarantined = True
                    entry.state = GroupState.INTERRUPTED
                    entry.failure = (
                        entry.failure
                        or f"quarantined after {entry.reentry_count} re-entries "
                        f"(max_reentries={self.breaker.max_reentries}) — run `retry` to release it"
                    )
                else:
                    entry.state = GroupState.READY
            self._persist()

    def _halted_by(self) -> str | None:
        """The (lowest-sorted) group id whose FAILED/INTERRUPTED outcome halts
        admission under ``on_group_failure = "halt"`` (plan U3/R41), or None if
        the policy is ``"overlap"`` or every group is still healthy."""
        if self.config.on_group_failure != "halt":
            return None
        unsuccessful = sorted(
            gid
            for gid, entry in self.state.groups.items()
            if entry.state in (GroupState.FAILED, GroupState.INTERRUPTED)
        )
        return unsuccessful[0] if unsuccessful else None

    # ------------------------------------------------------------- run

    async def run(self) -> dict[str, GroupState]:
        if self._resume:
            self._reap_orphans()
            self._reenter_on_resume()

        cap = 1 if self.config.sequential else self.config.concurrency
        in_flight: dict[asyncio.Task[GroupState], str] = {}

        try:
            while True:
                # Admissibility is recomputed fresh every cycle, never cached
                # (plan U2): a group already sitting in a "ready" queue from an
                # earlier cycle must still be re-checked, because a sibling that
                # just failed or was interrupted can newly hold it — a queue
                # populated once and only ever appended to would let it launch
                # anyway.
                for gid in self._admissible():
                    if len(in_flight) >= cap:
                        break
                    # Re-check against groups admitted earlier in *this* pass:
                    # _admissible() was computed before any of them transitioned
                    # to RUNNING, so two overlapping groups both appear in it.
                    # Without this, the U9 exclusion would hold only across
                    # cycles and let a same-pass pair launch together.
                    holds = self._holds_on(gid)
                    if holds:
                        self._record_holds(gid, holds)
                        continue
                    self.set_state(gid, GroupState.READY)
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
                    if self._halted_by() is not None:
                        # Deliberate stop (plan U3/R41), not a wedge: admission is
                        # paused because a group ended unsuccessfully, not because
                        # nothing can be scheduled. Un-admitted groups may lie
                        # outside _blocked_by_failure()'s DAG/overlap reachability
                        # entirely, so that check must not gate this return.
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
                    in_flight.pop(task)
                    # _run_group only ever raises RunAbort or ResolveConflict
                    # (plan U2); both propagate to the finally below, cancelling
                    # in-flight tasks. Any other outcome is already persisted by
                    # _run_group/_classify/_resolve_failure — the next cycle's
                    # _admissible() picks up whatever it unblocked.
                    task.result()
        finally:
            # Cancellation (ctrl-c, crash-in-test) must not leak group tasks;
            # persisted state carries the resume.
            for task in in_flight:
                task.cancel()

    def _unmet_deps(self, gid: str) -> list[str]:
        # RESOLVED releases dependents the same as COMPLETED (plan U2): its
        # work genuinely landed on the integration branch.
        return sorted(
            dep
            for dep in self.groups[gid].dependencies
            if self.state.groups[dep].state not in (GroupState.COMPLETED, GroupState.RESOLVED)
        )

    def _admissible(self) -> list[str]:
        """Every group launchable right now: PENDING or (resumed) READY, with no
        hold against it — DAG dependencies met, no U2 failure-gate hold, and no
        U9 exclusion against a healthy in-flight group sharing a file.

        Records each held group's reasons as a side effect so ``status`` can
        report them; admission itself is always recomputed from live state.
        """
        halted_by = self._halted_by()
        launchable: list[str] = []
        for gid in sorted(self.state.groups):
            if self.state.groups[gid].state not in (GroupState.PENDING, GroupState.READY):
                continue
            if halted_by is not None:
                # plan U3/R41: a group already FAILED/INTERRUPTED halts further
                # admission under the default policy — in-flight groups (not
                # touched here) still finish, but nothing new joins them.
                self._record_holds(
                    gid, [GroupHold(reason=HoldReason.RUN_HALTED, group_id=halted_by)]
                )
                continue
            holds = self._holds_on(gid)
            self._record_holds(gid, holds)
            if not holds:
                launchable.append(gid)
        return launchable

    def _holds_on(self, gid: str) -> list[GroupHold]:
        """Every distinct reason ``gid`` cannot start right now (plan U9)."""
        holds = [
            GroupHold(reason=HoldReason.DAG_DEPENDENCY, group_id=dep)
            for dep in self._unmet_deps(gid)
        ]
        holds += [
            GroupHold(
                reason=HoldReason.FAILURE_GATE,
                group_id=other,
                files=self._shared_files(gid, other),
            )
            for other in sorted(self._held_by(gid))
        ]
        holds += [
            GroupHold(
                reason=HoldReason.FILE_OVERLAP,
                group_id=other,
                files=self._shared_files(gid, other),
            )
            for other in sorted(self._excluded_by(gid))
        ]
        return holds

    def _excluded_by(self, gid: str) -> set[str]:
        """U9's mutual exclusion: healthy in-flight groups sharing a declared
        file with ``gid``. Symmetric and stateless — whichever of the two the
        scheduler admits first holds the other for as long as it is active, and
        no ordering between them is created or recorded.
        """
        return {
            other_gid
            for other_gid, entry in self.state.groups.items()
            if other_gid != gid
            and entry.state in ACTIVE_STATES
            and self._files_overlap(gid, other_gid)
        }

    def _record_holds(self, gid: str, holds: list[GroupHold]) -> None:
        """Persist ``gid``'s hold reasons, and log the ones that are new.

        Only on change, so a group held across many scheduling cycles produces
        one line per hold rather than one per cycle — a held group is otherwise
        completely silent, indistinguishable from one nobody scheduled.
        """
        with self._lock:
            entry = self.state.groups[gid]
            if entry.holds == holds:
                return
            fresh = [hold for hold in holds if hold not in entry.holds]
            entry.holds = holds
            self._persist()
        for hold in fresh:
            shared = f" on {', '.join(hold.files)}" if hold.files else ""
            log_event(
                self.paths, f"group {gid}: held ({hold.reason.value}) by {hold.group_id}{shared}"
            )

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
            final = self._classify(gid, GroupState.FAILED, f"{type(exc).__name__}: {exc}")
        except GroupFailure as exc:
            # The group exhausted its bounds (rewrite cap, generation cap,
            # operator skip) — a judgement about the work, not the harness.
            final = self._classify(gid, GroupState.FAILED, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 — a group failure must not kill the run
            # Envelope failure (R1/R2, inverted): an exception the orchestrator
            # does not recognise as a work judgement is by definition not one —
            # the harness (claude process/API, git, the filesystem) died under
            # the group, not the work. Non-terminal, so a plain `resume`
            # re-enters it. Only GroupFailure and ReportError above are judged
            # failures of the work and go to terminal FAILED. This used to be a
            # narrow allowlist (SessionError, LlmProcessError, PermissionDenied,
            # WorktreeRefreshConflict) that silently missed anything else —
            # WorktreeError included, which is exactly what wedged a real run in
            # terminal FAILED, unreachable by `resume` (run r20260726-grouping).
            return self._classify(gid, GroupState.INTERRUPTED, f"{type(exc).__name__}: {exc}")
        else:
            if final not in TERMINAL_STATES:
                final = self._classify(
                    gid, GroupState.FAILED, f"executor returned non-terminal state {final}"
                )
            else:
                self.set_state(gid, final)
        if final == GroupState.FAILED:
            # A FAILED group's stranded work must not be silently lost, and no
            # overlapping successor may build on the hole it might have left
            # (plan U2). May raise RunAbort (operator abort mid-escalation) or
            # ResolveConflict (a real merge conflict) — both propagate.
            final = await self._resolve_failure(gid)
        return final

    def _classify(self, gid: str, state: GroupState, failure: str) -> GroupState:
        """Record a failure-shaped outcome plus its lifecycle line (R1, R11)."""
        self.set_state(gid, state, failure=failure)
        log_event(self.paths, f"group {gid}: {state.value} ({failure})")
        if state == GroupState.FAILED:
            self._log_recovery_route(gid)
        return state

    def _log_recovery_route(self, gid: str) -> None:
        """Logged the moment a group reaches terminal FAILED (plan U7/R16), from
        the single classification choke point so every route that lands here —
        GroupFailure, ReportError, or an executor returning a bad terminal state —
        is covered, not just the three that happen to raise GroupFailure from
        inside review.py. Gives an operator reading ``run.log`` the branch, the
        worktree, and the literal `retry` command line without diffing
        state.json or re-deriving the worktree path."""
        repo_root = self.paths.repo_root
        run_id = self.paths.run_id
        branch = group_branch(run_id, gid)
        worktree = worktree_path(repo_root, run_id, gid, self.groups[gid].name)
        retry_cmd = f"smart-mcps-orchestrate retry --repo {repo_root} {run_id} {gid}"
        log_event(
            self.paths,
            f"group {gid}: terminal failed — branch {branch}, worktree {worktree}, "
            f"retry with: {retry_cmd}",
        )

    # --------------------------------------------------------- resolve (U2)

    async def _resolve_failure(self, gid: str) -> GroupState:
        """Resolve a FAILED group's stranded work: escalate when HITL is wired,
        resolve autonomously otherwise. Both None (no HITL, no resolve deps)
        leaves the group FAILED unchanged — the pre-U2 behaviour, and what every
        ``Scheduler`` built without the new constructor args still gets.

        Persists whatever the resolve decided: RESOLVED must land in
        ``self.state``, not just the return value dependents key off, and
        ``resolve_settled`` must land too — a FAILED group never re-enters
        ``_run_group`` on resume, so that flag is the only record that resolve
        already ran (any outcome propagates it — this only returns without
        raising once resolve has actually concluded; RunAbort/ResolveConflict
        stop the whole run instead, so there is no "still unsettled but the run
        continues" case to persist).
        """
        if self._resolve is None:
            return GroupState.FAILED
        if self._broker is not None and self._policy is not None:
            final = await self._resolve_via_escalation(gid)
        else:
            final = await self._resolve_autonomously(gid)
        with self._lock:
            if final == GroupState.RESOLVED:
                self.state.groups[gid].state = GroupState.RESOLVED
            self.state.groups[gid].resolve_settled = True
            self._persist()
        return final

    async def _resolve_autonomously(self, gid: str) -> GroupState:
        """Commit any stranded uncommitted work, then merge through U1's gate.
        A zero commit-count (nothing to commit and nothing already on the
        branch — including a branch already merged by hand, since its commits
        are then reachable from the tip too) means nothing was lost.

        ``merge_group`` is awaited off the event loop (plan U5 Decisions): its
        real wiring makes up to ``max_conflict_resolve_attempts`` in-place
        conflict-resolution attempts by warm-resuming the group's coder, which
        blocks for as long as that session takes — running it inline here
        would freeze every other group's progress for the duration.
        """
        group = self.groups[gid]
        assert self._resolve is not None
        self._resolve.commit_stranded(group)
        if self._resolve.commits_ahead(group) == 0:
            log_event(self.paths, f"group {gid}: resolve found nothing lost")
            return GroupState.FAILED
        try:
            # raises ResolveConflict on a real conflict (stops the run) or
            # ResolvePreflightFailed when Preflight declined the merge (does not).
            await asyncio.to_thread(self._resolve.merge_group, group)
        except ResolvePreflightFailed as exc:
            log_event(self.paths, f"group {gid}: {exc}")
            return GroupState.FAILED
        log_event(self.paths, f"group {gid}: resolved (stranded work merged)")
        return GroupState.RESOLVED

    async def _resolve_via_escalation(self, gid: str) -> GroupState:
        assert self._broker is not None and self._policy is not None
        if not self._policy.should_escalate(EscalationKind.GROUP_RESOLVE):
            return await self._resolve_autonomously(gid)
        request = EscalationRequest(
            id=uuid.uuid4().hex[:12],
            run_id=self.paths.run_id,
            group_id=gid,
            generation=self.state.groups[gid].generation,
            kind=EscalationKind.GROUP_RESOLVE,
            prompt=self._resolve_prompt(gid),
        )
        response = await asyncio.to_thread(self._broker.raise_escalation, request)
        if response is None:
            return await self._resolve_autonomously(gid)  # timeout → autonomous fallback
        if response.action == HumanAction.ABORT:
            self._broker.trigger_abort()
            raise RunAbort(f"operator aborted the run while resolving group {gid}")
        if response.action == HumanAction.SKIP:
            log_event(self.paths, f"group {gid}: operator declined to resolve — left failed")
            return GroupState.FAILED
        # ANSWER: the operator says proceed — either they fixed and merged by
        # hand (commits_ahead already reads 0, so this is a no-op that reports
        # nothing lost) or they are delegating the resolve; either way
        # _resolve_autonomously verifies containment via the same commit-count
        # gate U1 uses, never taking the operator's word for it. A real
        # conflict still raises ResolveConflict and stops the run rather than
        # silently releasing successors onto an unfixed branch.
        return await self._resolve_autonomously(gid)

    def _resolve_prompt(self, gid: str) -> str:
        overlap = self._overlap_report(gid)
        if not overlap:
            return f"group {gid} failed — resolve its stranded work before the run continues"
        named = "; ".join(f"{other} ({', '.join(files)})" for other, files in overlap)
        return f"group {gid} failed and overlaps pending group(s) {named} — resolve to release them"

    # ----------------------------------------------------- overlap holds (U2)

    def _files_overlap(self, gid: str, other_gid: str) -> bool:
        # ``Group.files`` is what the group *declares* it will touch, which is
        # already the union of existing and prospective files — two groups that
        # both plan to create the same not-yet-existing file overlap here, with
        # no filesystem lookup involved (plan U9).
        return bool(set(self.groups[gid].files) & set(self.groups[other_gid].files))

    def _shared_files(self, gid: str, other_gid: str) -> list[str]:
        return sorted(set(self.groups[gid].files) & set(self.groups[other_gid].files))

    def _held_by(self, gid: str) -> set[str]:
        """Source group ids currently holding ``gid`` from starting: a FAILED
        group whose resolve has not (yet) settled (``resolve_settled`` — plan
        U2), or any INTERRUPTED group, sharing at least one declared file.
        Concurrent overlap between two healthy running groups is a separate
        concern (plan U9), out of scope here.
        """
        held: set[str] = set()
        for other_gid, entry in self.state.groups.items():
            if other_gid == gid or not self._files_overlap(gid, other_gid):
                continue
            if entry.state == GroupState.INTERRUPTED:
                held.add(other_gid)
            elif entry.state == GroupState.FAILED and not entry.resolve_settled:
                held.add(other_gid)
        return held

    def _overlap_report(self, gid: str) -> list[tuple[str, list[str]]]:
        """(other group id, shared files) for every not-yet-started group whose
        declared files overlap ``gid`` — the successors a FAILED gid's
        escalation must name."""
        mine = set(self.groups[gid].files)
        report = [
            (other_gid, sorted(mine & set(self.groups[other_gid].files)))
            for other_gid, entry in self.state.groups.items()
            if other_gid != gid and entry.state in (GroupState.PENDING, GroupState.READY)
        ]
        return sorted((other, files) for other, files in report if files)

    def _blocked_by_failure(self) -> set[str]:
        """Groups stranded, not wedged: a failed/interrupted ancestor (DAG,
        transitive), or a file-overlap hold (plan U2) against a failed/interrupted
        group whose resolve has not released it — also transitive, since a held
        group's own DAG dependents are stranded right along with it."""
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
            for other_gid, entry in self.state.groups.items():
                if (
                    other_gid not in stranded
                    and entry.state not in TERMINAL_STATES
                    and entry.state != GroupState.INTERRUPTED
                    and gid in self._held_by(other_gid)
                ):
                    stranded.add(other_gid)
                    frontier.append(other_gid)
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
