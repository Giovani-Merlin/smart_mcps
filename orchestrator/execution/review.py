"""Review loop: warm coder↔reviewer ferry, circuit breaker, adaptation (plan U7).

The orchestrator ferries control, not content: round triggers carry statuses and
artifact pointers; reports and verdicts persist in the run directory, and the
reviewer computes the diff itself from the shared worktree (plan Key Technical
Decisions). Intensity tiers route the loop: self-verify skips the reviewer
entirely, paired adds one, paired-plus adds a mandatory extra verification pass
(origin R15). The breaker retires a session on token or round thresholds and
respawns a fresh generation from base with a condensed handoff; the generation
cap fails the group to the operator instead of respawning forever (origin R14).
Surprises fan out through the SurpriseBoard: unfinished groups named by a
surprise are rewritten before launch — or instead of merging, when already in
review (origin R12, R16). Completed groups are never rewritten.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.config import BreakerConfig, ExecutionConfig, LivenessConfig
from orchestrator.execution.escalation import EscalationBroker, EscalationPolicy
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.liveness import (
    ActivityRegistry,
    ChildActivity,
    LivenessProbe,
    read_suspend_facts,
)
from orchestrator.execution.manifest import (
    ManifestStore,
    archive_review_scratch,
    artifact_name,
    atomic_write_text,
    completed_round_count,
    log_event,
    record_session,
)
from orchestrator.execution.preflight import (
    PreflightBaseline,
)
from orchestrator.execution.prompting import (
    REVIEW_SCRATCH_DIRNAME,
    render_coder_answer_prompt,
    render_coder_prompt,
    render_extra_pass_prompt,
    render_handoff_prompt,
    render_ladder_compact_prompt,
    render_ladder_prioritized_prompt,
    render_ladder_summary_prompt,
    render_re_review_prompt,
    render_reentry_prompt,
    render_reviewer_prompt,
    render_revision_prompt,
)
from orchestrator.execution.decisions import decision_ledger, render_decisions_section
from orchestrator.execution.denial import classify_denial, denial_remedy
from orchestrator.execution.scheduler import (
    Executor,
    GroupContext,
    GroupFailure,
    GroupState,
    RunAbort,
)
from orchestrator.execution.sessions import (
    ContentFiltered,
    RoundResult,
    SessionError,
    SessionRunner,
    SuspendCured,
    UsageLimit,
    nudge_until_report,
    session_display_name,
)
from orchestrator.execution.streaming import TurnUsage
from orchestrator.execution.merge import (
    MergeConflict as MergeConflict,
)  # transitional re-export — removed in U7
from orchestrator.execution.merge_ladder import MergeLadder, _is_retry, _operator_surprise
from orchestrator.execution.surprises import SurpriseBoard, SurpriseHandling

# transitional re-export — removed in U7
from orchestrator.execution.surprises import (
    REASON_GROUP_COMPLETED as REASON_GROUP_COMPLETED,
)
from orchestrator.execution.surprises import (
    REASON_RUN_ENDED as REASON_RUN_ENDED,
)
from orchestrator.execution.surprises import (
    REASON_UNKNOWN_GROUP as REASON_UNKNOWN_GROUP,
)
from orchestrator.execution.surprises import (
    format_residue_report as format_residue_report,
)
from orchestrator.execution.surprises import (
    surprise_residue as surprise_residue,
)
from orchestrator.execution.worktrees import (
    diff_stat,
    ensure_excluded,
)
from orchestrator.model import (
    CoderReport,
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    Group,
    HumanAction,
    PermissionDenied,
    ReviewerVerdict,
    ReviewIntensity,
    RunManifest,
    SessionEntry,
    SessionRole,
    Surprise,
    unmet_required_verification,
)


class _ContentFilterStop(Exception):
    """Loop-internal signal (plan U10): `_worker_call` converts a
    `ContentFiltered` into this before it can reach a caller's own
    `except SessionError` — deliberately *not* a `SessionError` subclass, so
    `_reenter`'s fallback (which catches `SessionError`) never mistakes a
    content-filtered session for merely unreachable. `_run_generation`
    catches it once, at the top of every generation."""

    def __init__(self, exc: ContentFiltered):
        super().__init__(str(exc))
        self.exc = exc


_logger = logging.getLogger(__name__)


@dataclass
class ReviewDeps:
    """Everything a group's review loop needs; seams injected so tests script
    sessions in-process and U8/U9 wire worktrees, merges, and the speccer."""

    run_id: str
    runner: SessionRunner
    store: ManifestStore
    manifest: RunManifest
    # The run's compiled base context, prepended to every worker's first prompt
    # (ADR 0007). ``base_session_id`` is the legacy fork path's parent session
    # and is ``None`` unless ``fork_base_session`` is on — with no fork there is
    # no base session to have an id.
    base_context: str
    base_session_id: str | None
    fork_base_session: bool
    breaker: BreakerConfig
    execution: ExecutionConfig
    board: SurpriseBoard
    workspace_for: Callable[[Group], Path]
    merge_group: Callable[[Group, Path], None]  # raises MergeConflict
    rewrite_spec: Callable[[Group, list[Surprise]], Group]
    base_ref_for: Callable[[Group], str]
    # HITL seam (plan Phase D): both None ⇒ no escalations are ever raised; the
    # lifecycle log stays on regardless (R10).
    broker: EscalationBroker | None = None
    policy: EscalationPolicy | None = None
    # What was already red on the launch branch (plan U2/U3), read back by the
    # merge gate to tell a new failure from a pre-existing one. ``None`` (no
    # baseline captured, or a resumed run with no run directory to read it
    # from) degrades to "no_baseline", which never attributes a failure as
    # new — the same conservative default `compare_to_baseline` documents.
    preflight_baseline: PreflightBaseline | None = None
    # `provision_on_failure = "warn"`: the sync failure text for a group whose
    # worktree launched without a working environment, folded into its first
    # coder prompt. None (or a None result) means the environment is fine.
    provisioning_failure_for: Callable[[Group], str | None] | None = None
    # Liveness (plan U1/U2/U5): the run's shared activity registry — the same
    # instance the runner stamps — and the `[liveness]` config. Both None ⇒ no
    # probe is installed and heartbeats carry no Sign of Life facts.
    activity: ActivityRegistry | None = None
    liveness: LivenessConfig | None = None


def make_executor(deps: ReviewDeps) -> Executor:
    """The scheduler-facing executor: runs one group to a terminal state."""

    async def executor(ctx: GroupContext) -> GroupState:
        return await _GroupExecution(deps, ctx).run()

    return executor


class _GroupExecution(MergeLadder, SurpriseHandling):
    """One group's journey: generations, rounds, verdicts, rewrites, merge."""

    def __init__(self, deps: ReviewDeps, ctx: GroupContext):
        self.deps = deps
        self.ctx = ctx
        self.group = ctx.group
        self.gid = ctx.group.id
        self.generation = ctx.generation
        self.rewrites = 0
        self.sessions_spawned = 0
        self.extra_pass_done = False
        self.handoff_prompt: str | None = None
        self.workspace: Path | None = None
        self.coder_sid = ""
        self.coder_entry: SessionEntry | None = None
        self.reviewer_sid: str | None = None
        self._questions = 0  # needs_input rounds this generation (uncounted vs the breaker)
        self._grant_notes: list[str] = []  # operator guidance for an over-cap generation
        # Informational surprises consumed at a checkpoint (plan U13): folded into
        # the next prompt this generation builds, then cleared — never spent as a
        # rewrite, never sent to the speccer.
        self._briefing_notes: list[str] = []
        # An operator `retry` note: what they fixed outside the worker. Folded
        # into the next coder prompt as an "Operator note", one-shot — never
        # spent as a rewrite, never sent to the speccer.
        self._operator_notes: list[str] = []
        self._env_failure: str | None = None
        # The merge gate's untracked ladder: a first untracked-only failure is a
        # cheap same-spec relaunch with a note; a second, consecutive one has
        # the leftovers archived out of the tree and the merge proceeds. Counts
        # per group across generations (a relaunch advances the generation).
        self._untracked_strikes = 0
        # One automatic gate re-run per generation on an attributable
        # regression in autonomous mode, before a rewrite is spent on a flake.
        self._flake_reruns = 0
        # Re-entry discovery (R4): a live coder entry at the persisted generation
        # can only pre-exist the executor on a resumed run — fresh runs start with
        # an empty group entry. One-shot: consumed by the first generation.
        self._reentry_entry: SessionEntry | None = self._find_reentry_session()
        # Evidence, not a state (plan P3): the loop only ever tells it when a
        # round started; the writing happens on its own daemon thread and nothing
        # here reads it back.
        self._heartbeat = RoundHeartbeat(
            deps.store.paths,
            self.gid,
            log=self._log,
            activity_provider=self._current_child if deps.activity is not None else None,
        )
        # Suspend Cure counts for this group, per generation (plan U5). Seeded
        # from nothing: `Scheduler.record_cure` persists the real count, and
        # `on_cure` mirrors the value it returns.
        self._cures: dict[int, int] = {}
        if deps.activity is not None and deps.liveness is not None:
            probe = LivenessProbe(
                self._heartbeat,
                deps.liveness,
                self._current_child,
                log=self._log,
                suspend_facts_provider=partial(read_suspend_facts, deps.store.paths),
                cures_provider=lambda: self._cures.get(self._heartbeat.generation_no, 0),
                on_cure=self._on_cure,
                activity=deps.activity,
            )
            self._heartbeat.add_tick_hook(probe.tick)
        # The round number `_round_tag` most recently rendered (plan U10):
        # `_on_content_filtered` needs the number of the round that was in
        # flight when `ContentFiltered` hit, and every site that starts or
        # ends a round already renders its tag through `_round_tag` — so
        # recording it there, as a side effect, is the one place that number
        # is always current with no separate bookkeeping to keep in sync.
        self._current_round_no = 0

    def _current_child(self) -> ChildActivity | None:
        """This group's live worker child, matched on its worktree — the cwd
        every worker call of the group is spawned in."""
        if self.deps.activity is None or self.workspace is None:
            return None
        return self.deps.activity.current(str(self.workspace))

    def _on_cure(self, pid: int, generation: int, session_id: str) -> None:
        self._cures[generation] = self.ctx.record_cure()

    def _decisions_text(self) -> str:
        """Binding operator decisions for this group, rendered fresh from disk
        at every prompt build (plan U9) so a later answer reaches every
        subsequent coder, handoff, reviewer and re-review prompt."""
        return render_decisions_section(decision_ledger(self.deps.store.paths, self.gid))

    async def _worker_call(self, thunk: Callable[[], object], *, recover: Callable[[str], object]):
        """Runs one worker call in a thread; on a Suspend Cure (plan U5) — the
        liveness probe having killed this child because it showed no Sign of
        Life since a detected machine wake — logs the recovery and re-issues
        ``recover(cured_session_id)`` in *this call's* place, itself routed
        back through ``_worker_call`` so a second cure re-issues again until
        the probe's own per-generation cap stops curing. Coder call sites pass
        a ``recover`` that warm-resumes with the re-entry prompt; reviewer
        sites pass one that resumes with the re-review prompt (per the U5
        decision) — the wrapper itself is role-agnostic.

        A ``ContentFiltered`` (plan U10) is converted here to a loop-internal
        ``_ContentFilterStop`` before it can reach a caller's own
        ``except SessionError`` — most importantly ``_reenter``'s fallback,
        which must never treat a content-filtered session as merely
        unreachable and fork a fresh one from it. Any other exception — a
        plain ``SessionError``, ``UsageLimit`` — propagates unchanged; this
        wrapper is transparent to everything but ``SuspendCured`` and
        ``ContentFiltered``. A ``SuspendCured`` from a first launch whose
        session never registered on disk still carries a real ``session_id``
        (read off the argv the failed call actually used, not off the
        transcript), so ``recover`` resumes it the same as any other cure;
        being a ``SessionError`` subclass, it would fall through to the
        ordinary failure handling regardless if it ever turned up somewhere
        this wrapper does not reach.
        """
        try:
            return await asyncio.to_thread(thunk)
        except ContentFiltered as exc:
            raise _ContentFilterStop(exc) from exc
        except SuspendCured as exc:
            self._log(
                f"group {self.gid} generation {self.generation}: "
                f"warm-resuming session {exc.session_id} after suspend cure"
            )
            return await self._worker_call(partial(recover, exc.session_id), recover=recover)

    async def run(self) -> GroupState:
        # interactive tier only: approve before anything is launched.
        await self._approve_gate(
            EscalationKind.GROUP_START, f"launch group {self.gid} ({self.group.name})?"
        )
        await self._handle_pending_surprises("upstream surprise named this group before launch")
        self.workspace = self.deps.workspace_for(self.group)
        self._log(f"group {self.gid}: worktree ready at {self.workspace}")
        if self.deps.provisioning_failure_for is not None:
            self._env_failure = self.deps.provisioning_failure_for(self.group)
        self._heartbeat.start()
        # A usage-limit pause is announced on this group's heartbeat for as long
        # as the group is live (see `UsageLimitGate.watch`) — a run that has
        # stopped because the account is out of budget must read as *paused*,
        # not as wedged, on the board as well as in the log.
        gate = getattr(self.deps.runner, "gate", None)
        if gate is not None:
            gate.watch(self._heartbeat)
        try:
            while True:
                merged = await self._run_generation()
                if merged:
                    self._log_coder_session_end()
                    self._log(f"group {self.gid}: completed")
                    return GroupState.COMPLETED
                # a rewrite or a retirement happened inside; loop spawns the next session
        finally:
            if gate is not None:
                gate.unwatch(self._heartbeat)
            self._heartbeat.stop()

    # ------------------------------------------------------------ generation

    async def _run_generation(self) -> bool:
        """One coder session's lifetime. True → merged; False → respawn/rewritten.

        A ``_ContentFilterStop`` (plan U10) can surface from any worker call
        this generation makes — first launch, coder nudge, needs_input
        resume, revision resume, or a reviewer call inside ``_review_round``
        — so it is caught here, at the one place that wraps every one of
        them, rather than at each call site individually.
        """
        try:
            return await self._run_generation_body()
        except _ContentFilterStop as exc:
            await self._on_content_filtered(exc.exc)
            return False

    async def _run_generation_body(self) -> bool:
        assert self.workspace is not None
        self.ctx.set_state(GroupState.RUNNING)
        first: RoundResult | None = None
        reentry, self._reentry_entry = self._reentry_entry, None  # one-shot
        is_reentry = reentry is not None
        self._flake_reruns = 0
        # Re-entry (warm-resumed or fallback-forked) continues this generation's
        # numbering rather than starting over, so round-numbered artifacts don't
        # collide with — and silently overwrite — pre-crash ones still on disk.
        #
        # Computed *before* the re-entry call, not after (plan P3): the re-entry
        # resume is itself a full round — it writes files and runs tests, for
        # twenty minutes on the run that prompted this — so it has to be able to
        # announce its own number before it blocks. `completed_round_count` reads
        # only the report artifacts already on disk and takes nothing from the
        # resume, so hoisting it changes no number.
        rounds = (
            completed_round_count(self.deps.store.paths, self.gid, self.generation)
            if is_reentry
            else 0
        )
        if reentry is not None:
            first = await self._reenter(reentry, round_no=rounds + 1)
        if first is None:
            prompt = self.handoff_prompt or render_coder_prompt(
                self.deps.run_id, self.group, decisions=self._decisions_text()
            )
            prompt = self._apply_briefing(prompt)
            prompt = self._apply_env_notice(prompt)
            prompt = self._apply_operator_note(prompt)
            self.handoff_prompt = None
            # The session id is generated and recorded *before* the blocking fork
            # call, not after (plan U7): a crash mid-call would otherwise leave no
            # manifest entry for a group interrupted during its very first round,
            # so a later resume would fork a brand new session instead of finding
            # the one already under way.
            self.coder_sid = str(uuid.uuid4())
            self.reviewer_sid = None
            self.coder_entry = self._record(SessionRole.CODER, self.coder_sid)
            self.coder_entry.spec_sha256 = _spec_hash(self.group)
            self.deps.store.save(self.deps.manifest)
            # Logged *before* the fork, not after. `start_fork` blocks for as long
            # as the base session takes to absorb the group's prompt — 21 minutes
            # on a real run — and the old "coder launched" line landed only once it
            # returned, so a group killed mid-launch left a log that never
            # mentioned it had started, and a live run showed a silent gap with no
            # indication anything was happening.
            launch = "forking base session" if self.deps.fork_base_session else "fresh session"
            self._log(
                f"group {self.gid} generation {self.generation}: "
                f"coder launching, {launch} (session {self.coder_sid})"
            )
            # `round N: started` logged and marked *before* the fork call, not
            # after — matching `_reenter`'s pattern. `start_fork` is itself round
            # N's whole first turn, so logging it once the call returns leaves the
            # round-start line trailing behind everything it is supposed to
            # cover, and a group killed mid-fork looks like it never started a
            # round at all.
            self._log(f"{self._round_tag(rounds + 1)}: started")
            self._heartbeat.mark_round(self.generation, rounds + 1)
            self._heartbeat.mark_phase(
                "forking the base session" if self.deps.fork_base_session else "starting the coder"
            )
            self._watch_transcript(self.coder_entry)
            coder_on_turn = self._make_coder_on_turn(self.coder_entry)
            first = await self._worker_call(
                partial(
                    self._launch_call(),
                    prompt=prompt,
                    name=session_display_name(self.deps.run_id, self.gid, "coder", self.generation),
                    cwd=self.workspace,
                    session_id=self.coder_sid,
                    on_turn=coder_on_turn,
                ),
                recover=lambda sid: self.deps.runner.resume(
                    session_id=sid,
                    prompt=render_reentry_prompt(self.group),
                    cwd=self.workspace,
                    on_turn=coder_on_turn,
                ),
            )
            self._adopt_actual_session_id(first)
            self._refresh_transcript(self.coder_entry)
            self._log(f"group {self.gid} generation {self.generation}: coder launched")
        result = first
        # Guarded on `is_reentry`, not on whether the resume succeeded: a
        # fallback fork *is* round N of the same generation, and `_reenter`
        # already announced N before it blocked. The fresh-fork branch above
        # announces its own round before `start_fork` too, so nothing further is
        # needed here in either case.

        verification_ids = [item.id for item in self.group.verification]
        while True:
            # F4: nothing used to supersede "forking the base session" once the
            # fork returned, so a finished round still read as mid-fork minutes
            # later. Each pass through this loop is the coder producing (or
            # being nudged toward) its report.
            self._heartbeat.mark_phase("coder working toward a report")

            def _coder_nudge(round_result: RoundResult) -> tuple[CoderReport, RoundResult]:
                return nudge_until_report(
                    self.deps.runner,
                    round_result,
                    CoderReport,
                    cwd=self.workspace,
                    verification_ids=verification_ids,
                )

            def _coder_nudge_recover(sid: str) -> tuple[CoderReport, RoundResult]:
                resumed = self.deps.runner.resume(
                    session_id=sid,
                    prompt=render_reentry_prompt(self.group),
                    cwd=self.workspace,
                    on_turn=self._make_coder_on_turn(self.coder_entry),
                )
                return _coder_nudge(resumed)

            report, result = await self._worker_call(
                partial(_coder_nudge, result), recover=_coder_nudge_recover
            )
            self._persist_coder_usage()

            if report.status == "needs_input":
                # The coder-question channel: escalate, and on an answer resume the
                # same coder warm without counting a revision round (clarifications
                # never trip the breaker; token usage still accumulates).
                resumed = await self._resolve_needs_input(report)
                if resumed is None:
                    return False  # downgraded / unescalated → a rewrite already happened
                result = resumed
                continue

            rounds += 1
            report_path = self.deps.store.save_group_artifact(
                self.gid, artifact_name("report", self.generation, rounds), report
            )
            self._spread(report.surprises)
            if report.status == "permission_denied":
                # Typed denial (plan U3): interrupted, not failed, and no rewrite
                # spent — bypasses _on_coder_stuck/_rewrite entirely.
                #
                # Attributed (plan P2), because one status covered three unrelated
                # causes with three different remedies and the last validation
                # misdiagnosed one of them. The kind rides in the exception message
                # as well as on the instance: the scheduler already writes
                # `f"{type(exc).__name__}: {exc}"` into `state.json`, so `status`
                # and the Observatory gain it with no schema change anywhere.
                kind = classify_denial(
                    denied_command=report.denied_command,
                    denial_error=report.denial_error,
                    denial_source=report.denial_source,
                    deny_rules=self.deps.runner.effective_disallowed_tools(),
                    observed=result.deny_signals,
                )
                self._log(f"{self._round_tag(rounds)}: ended (permission_denied: {kind})")
                self._log(f"group {self.gid} denial: {denial_remedy(kind)}")
                raise PermissionDenied(
                    f"group {self.gid} denied command ({kind}): {report.denied_command}",
                    kind=str(kind),
                    denied_command=report.denied_command,
                    denial_error=report.denial_error,
                    denial_source=report.denial_source,
                )
            if report.status != "completed":
                self._log(f"{self._round_tag(rounds)}: ended (coder {report.status})")
                await self._on_coder_stuck(report, report_path)
                return False

            # The verification gate (run r20260829-162627 P1): a `completed`
            # report that does not carry a passing result for every *required*
            # item never reaches a reviewer, and never reaches the merge. It
            # becomes an ordinary `changes_required` round instead — same
            # breaker, same handoff, same revision prompt — so the coder gets
            # told exactly which items it left unmet. This is the only thing
            # standing between a self_verify group and an unchecked merge:
            # `_review_round` creates no reviewer for that tier, so without it
            # the coder's own `status` field is the entire gate.
            gaps = unmet_required_verification(self.group.verification, report.verification_results)
            if gaps:
                verdict = ReviewerVerdict(
                    status="changes_required",
                    required_changes=gaps,
                    notes=(
                        "verification gate: the report claims completed but does not "
                        "report a passing result for every required verification item"
                    ),
                )
                verdict_path = self.deps.store.save_group_artifact(
                    self.gid, artifact_name("verdict", self.generation, rounds), verdict
                )
                self._log(
                    f"{self._round_tag(rounds)}: verification gate held "
                    f"({len(gaps)} required item(s) unmet)"
                )
            else:
                verdict, verdict_path = await self._review_round(report_path, rounds)
            if verdict is None or verdict.status == "approved":
                outcome = "self-verified" if verdict is None else "approved"
                self._log(f"{self._round_tag(rounds)}: ended ({outcome})")
                # A surprise named this group while it was in review: a
                # rewrite-worthy one means its pending approval is not accepted
                # (plan U7 scenario); an informational-only one is noted and the
                # approval proceeds (plan U13).
                if await self._handle_pending_surprises("surprise named this group during review"):
                    return False
                await self._approve_gate(
                    EscalationKind.MERGE_APPROVE, f"merge group {self.gid} ({self.group.name})?"
                )
                return await self._merge()
            if verdict.status in ("too_hard", "structural"):
                self._log(f"{self._round_tag(rounds)}: ended ({verdict.status})")
                await self._on_reviewer_hard(verdict, verdict_path)
                return False

            # changes_required — breaker gate before the next warm round
            self._log(f"{self._round_tag(rounds)}: ended (changes_required)")
            reason = self._breaker_reason(rounds)
            if reason:
                await self._retire(reason)
                self._prepare_handoff(report, verdict)
                return False
            assert verdict_path is not None
            self.ctx.set_state(GroupState.RUNNING)
            self._log(f"{self._round_tag(rounds + 1)}: started")
            self._heartbeat.mark_round(self.generation, rounds + 1)
            revision_on_turn = self._make_coder_on_turn(self.coder_entry)
            result = await self._worker_call(
                partial(
                    self.deps.runner.resume,
                    session_id=self.coder_sid,
                    prompt=render_revision_prompt(str(verdict_path), verdict.required_changes),
                    cwd=self.workspace,
                    on_turn=revision_on_turn,
                ),
                recover=lambda sid: self.deps.runner.resume(
                    session_id=sid,
                    prompt=render_reentry_prompt(self.group),
                    cwd=self.workspace,
                    on_turn=revision_on_turn,
                ),
            )

    # ------------------------------------------------------------ re-entry (R4–R6)

    def _find_reentry_session(self) -> SessionEntry | None:
        """The interrupted coder to warm-resume, discovered from the manifest: the
        group's latest coder entry at the persisted generation with no retirement
        reason (spec discovery rule)."""
        group_entry = self.deps.manifest.groups.get(self.gid)
        if group_entry is None:
            return None
        live = [
            entry
            for entry in group_entry.sessions
            if entry.role == SessionRole.CODER
            and entry.generation == self.generation
            and entry.retirement_reason is None
        ]
        if not live:
            return None
        entry = live[-1]
        if entry.spec_sha256 is not None and entry.spec_sha256 != _spec_hash(self.group):
            # The spec was rewritten under the session (an operator wrote a
            # spec-genN.json after it started): a warm resume would keep a coder
            # working to a spec nobody holds any more. Fresh coder, new spec.
            self._reentry_fallback(entry, "spec rewritten since the session started")
            return None
        return entry

    async def _reenter(self, entry: SessionEntry, *, round_no: int) -> RoundResult | None:
        """Warm-resume the interrupted coder in its worktree (R4). Returns the
        resumed round, or None to fall through to a fresh fork from base — when
        the persisted context already exceeds the breaker limit (R5) or the warm
        resume itself fails at the envelope. A SessionError from that fork
        propagates: the group lands interrupted again, since the envelope is
        still failing (no in-run retry loop). Exactly one re-entry lifecycle
        line is written either way (R6).

        ``round_no`` is the number this re-entry carries, announced *before* the
        blocking resume: the resume performs a whole round's work, so labelling
        the window `round 0` / `resuming the interrupted coder` for its entire
        duration hid twenty minutes of real progress from the operator."""
        assert self.workspace is not None
        limit = self.deps.breaker.context_token_limit
        if entry.last_context_tokens > limit:
            self._reentry_fallback(
                entry, f"context tokens {entry.last_context_tokens} exceed limit {limit}"
            )
            return None
        # Same reasoning as the fork line: the resume blocks while the worker
        # reloads its context (14 minutes on the run that prompted this), and
        # announcing it only on success made a resumed run look wedged.
        #
        # Deliberately not phrased as a "re-entry" line: R6 promises exactly one
        # of those per re-entry and they report an *outcome* (resumed, or forked
        # after a failure). This one announces an attempt, so it must not be
        # mistaken for the outcome by a reader counting them.
        self._log(f"group {self.gid}: resuming interrupted coder session {entry.session_id}")
        # Ordering matters and is load-bearing: `mark_round` calls
        # `mark_phase("running")` internally, so marking the round *after* the
        # phase would overwrite "resuming the interrupted coder" with "running",
        # and marking the phase after the round would undo this whole fix.
        self._log(f"{self._round_tag(round_no)}: started")
        self._heartbeat.mark_round(self.generation, round_no)
        self._heartbeat.mark_phase("resuming the interrupted coder")
        reentry_on_turn = self._make_coder_on_turn(entry)
        try:
            result = await self._worker_call(
                partial(
                    self.deps.runner.resume,
                    session_id=entry.session_id,
                    prompt=render_reentry_prompt(self.group),
                    cwd=self.workspace,
                    on_turn=reentry_on_turn,
                ),
                recover=lambda sid: self.deps.runner.resume(
                    session_id=sid,
                    prompt=render_reentry_prompt(self.group),
                    cwd=self.workspace,
                    on_turn=reentry_on_turn,
                ),
            )
        except UsageLimit:
            # Not a fallback case. The fallback exists for a session that has
            # become unreachable, where a fresh fork is a real second chance; a
            # usage limit is the account being out of budget, so the fork fails
            # identically and spends a generation of the breaker's budget on a call
            # that could not have succeeded. Propagating leaves the group
            # INTERRUPTED at this generation, which a plain `resume` re-enters
            # once the limit has reset.
            raise
        except SessionError as exc:
            self._reentry_fallback(entry, f"warm resume failed: {exc}")
            return None
        self.coder_sid = entry.session_id
        self.coder_entry = entry
        self.reviewer_sid = None
        self.sessions_spawned += 1  # live session again: a later rewrite respawns fresh
        # A warm resume can be the first time this session's transcript actually
        # exists on disk — the entry was pre-registered before any turn ran, so
        # its `transcript_path` may still be null until this resume creates the
        # file. Refreshed here rather than left to the next `_persist_coder_usage`
        # call, which never touches `transcript_path` at all.
        self._refresh_transcript(entry)
        self._log(f"group {self.gid} re-entry: resumed session {entry.session_id}")
        return result

    def _launch_call(self) -> Callable[..., RoundResult]:
        """How a *first* round is launched, with its parent context bound.

        The two launch sites (coder and reviewer) call this rather than
        duplicating the branch: a fresh session primed with the run's base
        context (the default), or the legacy fork of the run's base session
        (ADR 0007). Both take the same ``prompt``/``name``/``cwd`` keywords
        from there on.
        """
        if self.deps.fork_base_session:
            return partial(self.deps.runner.start_fork, base_id=self.deps.base_session_id)
        return partial(self.deps.runner.start_worker, base_context=self.deps.base_context)

    def _reentry_fallback(self, entry: SessionEntry, reason: str) -> None:
        """Retire the unreachable session and log the fork decision; the caller
        falls through to the fresh-fork path (existing handoff-free coder prompt)."""
        entry.retirement_reason = f"re-entry fallback: {reason}"
        self._log(f"group {self.gid} re-entry: forked generation {self.generation} ({reason})")

    def _copy_usage(self, entry: SessionEntry, session_id: str) -> None:
        """Mirror the in-memory cumulative usage onto a manifest entry.

        ``last_context_tokens`` is the breaker's input (occupancy of the latest
        round); the cumulative counters are the session's total spend, kept split
        by token class so an estimate-vs-actual view can tell a cache-heavy run
        from an genuinely expensive one. Both are written on the same save.
        """
        usage = self.deps.runner.usage_of(session_id)
        entry.last_context_tokens = usage.last_context_tokens
        entry.rounds_completed = usage.rounds
        entry.total_input_tokens = usage.total_input_tokens
        entry.total_output_tokens = usage.total_output_tokens
        entry.total_cache_read_tokens = usage.total_cache_read_tokens
        entry.total_cache_creation_tokens = usage.total_cache_creation_tokens
        entry.base_context_tokens = usage.base_context_tokens
        entry.total_cost_usd = usage.total_cost_usd

    def _persist_coder_usage(self) -> None:
        """Record the active coder's latest context size on its manifest entry
        after every round (R5): in-memory usage dies with the process, and the
        next re-entry pre-checks this value against the breaker limit."""
        if self.coder_entry is None:
            return
        self._copy_usage(self.coder_entry, self.coder_sid)
        self.deps.store.save(self.deps.manifest)

    def _make_coder_on_turn(
        self, entry: SessionEntry
    ) -> Callable[[TurnUsage, Callable[[str], None]], None]:
        """Per-turn observer for one coder round call (plan U1's seam), doing two
        unrelated things that happen to ride the same callback:

        - continuous bookkeeping (plan U4): ``last_context_tokens`` updates as
          turns stream in, not only once the round finishes and
          ``_persist_coder_usage`` runs — so a group's manifest entry reflects
          reality while it is still in flight, not just after.
        - the staged context ladder (plan U3), gated by
          ``breaker.context_ladder_enabled`` (off by default): 70%/90%/100% of
          ``context_token_limit`` each send at most one staged prompt onto the
          still-running round via ``send``. This bounds *cost* inside a round —
          a token ceiling is a proxy for cost, not for stuck, which is why R7
          rejected a wall-clock timeout here for the opposite reason.

        ``fired`` is local to this call, so a fresh round always gets its own
        clean set of thresholds — a round sitting at 99% from a prior round
        never suppresses this round's own 70% checkpoint.
        """
        fired: set[str] = set()

        def on_turn(usage: TurnUsage, send: Callable[[str], None]) -> None:
            context = (
                usage.input_tokens
                + usage.output_tokens
                + usage.cache_read_input_tokens
                + usage.cache_creation_input_tokens
            )
            entry.last_context_tokens = context
            self.deps.store.save(self.deps.manifest)
            if not self.deps.breaker.context_ladder_enabled:
                return
            limit = self.deps.breaker.context_token_limit
            if context >= limit and "100" not in fired:
                fired.update({"70", "90", "100"})
                send(render_ladder_compact_prompt())
            elif context >= limit * 0.9 and "90" not in fired:
                fired.update({"70", "90"})
                send(render_ladder_prioritized_prompt())
            elif context >= limit * 0.7 and "70" not in fired:
                fired.add("70")
                send(render_ladder_summary_prompt())

        return on_turn

    def _adopt_actual_session_id(self, result: RoundResult) -> None:
        """Reconcile the pre-registered coder id with the one the CLI really used.

        Plan U7 records the id *before* the fork, which assumes the fork uses it.
        A usage-limit retry breaks that assumption: the first attempt spends the
        id, so the retry mints a fresh one (see ``_call_with_retry``). Without
        this, the manifest keeps an id no session was ever created under, and a
        later resume would `--resume` into nothing — turning a recovered run
        into an unrecoverable one.
        """
        actual = result.session_id
        if not actual or actual == self.coder_sid:
            return
        self._log(
            f"group {self.gid} generation {self.generation}: coder session is {actual}, "
            f"not the pre-registered {self.coder_sid} (usage-limit retry minted a new id)"
        )
        self.coder_sid = actual
        self.coder_entry.session_id = actual

    def _refresh_transcript(self, entry: SessionEntry) -> None:
        """Fill in a pre-registered entry's transcript path once its session
        actually exists on disk (plan U7) — recorded before the fork call, so
        the path itself isn't known until the call returns.

        Authoritative over whatever `_watch_transcript` filled early (F9): a
        usage-limit retry can mint a fresh session id mid-fork, making the
        heartbeat's early path provisional — this overwrite, keyed on the
        adopted id, is what settles it."""
        self._heartbeat.on_tick = None
        entry.transcript_path = _transcript_str(self.deps.runner, entry.session_id)
        self.deps.store.save(self.deps.manifest)

    def _watch_transcript(self, entry: SessionEntry) -> None:
        """Fill ``transcript_path`` while the fork is still blocking (F9).

        `start_fork` blocks for the round's entire first turn — 8 to 24 minutes
        on r20260828-090936 — and until it returns the transcript endpoint 404s,
        so the operator cannot watch the very work they are waiting on. The
        transcript *file* appears as soon as the CLI starts writing; only the
        manifest pointer is missing. The group's heartbeat thread is the one
        thing awake during the fork, so it re-globs for the pre-registered id
        each tick and persists the path the moment the file exists. The hook
        self-clears once it has done its job; `_refresh_transcript` clears it
        unconditionally and overwrites the path after the fork returns."""

        def probe() -> None:
            if entry.transcript_path:
                self._heartbeat.on_tick = None
                return
            path = _transcript_str(self.deps.runner, entry.session_id)
            if path is not None:
                entry.transcript_path = path
                self.deps.store.save(self.deps.manifest)
                self._heartbeat.on_tick = None

        self._heartbeat.on_tick = probe

    def _persist_reviewer_usage(self, session_id: str) -> None:
        """Record the reviewer's latest context size on its manifest entry after
        every round (plan U7), mirroring ``_persist_coder_usage``: the reviewer
        entry otherwise carries a zero context-token count forever."""
        group_entry = self.deps.manifest.groups.get(self.gid)
        if group_entry is None:
            return
        for entry in reversed(group_entry.sessions):
            if entry.role == SessionRole.REVIEWER and entry.session_id == session_id:
                self._copy_usage(entry, session_id)
                self.deps.store.save(self.deps.manifest)
                return

    # ------------------------------------------------------------ review

    async def _review_round(
        self, report_path: Path, rounds: int
    ) -> tuple[ReviewerVerdict | None, Path | None]:
        if self.group.intensity == ReviewIntensity.SELF_VERIFY:
            return None, None  # AE7: no reviewer session is ever created
        assert self.workspace is not None
        self.ctx.set_state(GroupState.REVIEWING)
        self._heartbeat.mark_phase("reviewer verifying the report")  # F4

        def _reviewer_recover(sid: str) -> RoundResult:
            return self.deps.runner.resume(
                session_id=sid,
                prompt=render_re_review_prompt(str(report_path), decisions=self._decisions_text()),
                cwd=self.workspace,
            )

        if self.reviewer_sid is None:
            first = await self._worker_call(
                partial(
                    self._launch_call(),
                    prompt=render_reviewer_prompt(
                        self.deps.run_id,
                        self.group,
                        report_path=str(report_path),
                        base_ref=self.deps.base_ref_for(self.group),
                        scratch_dir=str(self.workspace / REVIEW_SCRATCH_DIRNAME),
                        decisions=self._decisions_text(),
                    ),
                    name=session_display_name(
                        self.deps.run_id, self.gid, "reviewer", self.generation
                    ),
                    cwd=self.workspace,
                ),
                recover=_reviewer_recover,
            )
            self.reviewer_sid = first.session_id
            self._record(SessionRole.REVIEWER, first.session_id)
            result = first
        else:
            result = await self._worker_call(
                partial(
                    self.deps.runner.resume,
                    session_id=self.reviewer_sid,
                    prompt=render_re_review_prompt(
                        str(report_path), decisions=self._decisions_text()
                    ),
                    cwd=self.workspace,
                ),
                recover=_reviewer_recover,
            )

        def _reviewer_nudge(round_result: RoundResult) -> tuple[ReviewerVerdict, RoundResult]:
            return nudge_until_report(
                self.deps.runner, round_result, ReviewerVerdict, cwd=self.workspace
            )

        def _reviewer_nudge_recover(sid: str) -> tuple[ReviewerVerdict, RoundResult]:
            return _reviewer_nudge(_reviewer_recover(sid))

        verdict, result = await self._worker_call(
            partial(_reviewer_nudge, result), recover=_reviewer_nudge_recover
        )
        self._persist_reviewer_usage(self.reviewer_sid)
        verdict_path = self.deps.store.save_group_artifact(
            self.gid, artifact_name("verdict", self.generation, rounds), verdict
        )
        self._spread(verdict.surprises)
        self._log(f"{self._round_tag(rounds)}: reviewer verdict {verdict.status}")

        if (
            verdict.status == "approved"
            and self.group.intensity == ReviewIntensity.PAIRED_PLUS
            and not self.extra_pass_done
        ):
            # Above d_hard: one mandatory extra verification round (origin R15).
            self.extra_pass_done = True
            result = await self._worker_call(
                partial(
                    self.deps.runner.resume,
                    session_id=self.reviewer_sid,
                    prompt=render_extra_pass_prompt(),
                    cwd=self.workspace,
                ),
                recover=_reviewer_recover,
            )
            verdict, result = await self._worker_call(
                partial(_reviewer_nudge, result), recover=_reviewer_nudge_recover
            )
            self._persist_reviewer_usage(self.reviewer_sid)
            verdict_path = self.deps.store.save_group_artifact(
                self.gid, f"verdict-g{self.generation}-r{rounds}-extra.json", verdict
            )
            self._spread(verdict.surprises)
            self._log(f"{self._round_tag(rounds)}: reviewer verdict {verdict.status} (extra pass)")
        self._archive_review_scratch()
        return verdict, verdict_path

    def _archive_review_scratch(self) -> None:
        """Exclude and archive the reviewer's scratch directory at round end
        (plan U6), so Preflight's cleanliness check (plan U4) sees a worktree
        whose only "dirt" was the reviewer's own litter as clean.

        A no-op when the scratch directory was never created — the common case
        for a reviewer round that never touched it, and cheap insurance against
        touching git at all in a workspace that (in some tests) isn't one.
        """
        assert self.workspace is not None
        scratch_dir = self.workspace / REVIEW_SCRATCH_DIRNAME
        if not scratch_dir.exists():
            return
        ensure_excluded(self.workspace, REVIEW_SCRATCH_DIRNAME)
        archive_review_scratch(
            scratch_dir,
            self.deps.store.paths.review_scratch_archive_dir(self.gid),
            cap_bytes=self.deps.execution.review_scratch_cap_bytes,
            log=self._log,
        )

    # ------------------------------------------------------------ outcomes

    async def _rewrite(self, why: str, extra: list[Surprise] | None = None) -> None:
        self.ctx.set_state(GroupState.REWRITING)
        extra = list(extra or [])
        if self.rewrites >= self.deps.execution.max_rewrites:
            # Terminal give-up: escalate before failing. An answer grants one more
            # (guided) rewrite; None (unescalated / autonomous timeout) fails as before.
            response = await self._escalate(
                EscalationKind.CAPS_EXHAUSTED,
                prompt=(
                    f"group {self.gid}: rewrite cap ({self.deps.execution.max_rewrites}) "
                    f"exhausted — {why}"
                ),
                want_diff=True,
            )
            if response is None:
                raise GroupFailure(
                    f"rewrite cap ({self.deps.execution.max_rewrites}) exhausted: {why}"
                )
            # `answer` and `retry` alike grant the one extra rewrite.
            extra.append(_operator_surprise(self.gid, response.answer))
        surprises = self.deps.board.consume(self.gid) + extra
        self._log(
            f"group {self.gid} generation {self.generation}: rewriting spec ({why}); "
            f"surprises consumed: {len(surprises)} "
            f"[{', '.join(surprise.kind for surprise in surprises) or 'none'}]"
        )
        self.group = await asyncio.to_thread(self.deps.rewrite_spec, self.group, surprises)
        self.rewrites += 1
        self.handoff_prompt = None  # the fresh session gets the rewritten spec
        if self.sessions_spawned:
            self._advance_generation()
        self._persist_rewritten_spec()

    async def _relaunch(self, note: str, why: str) -> None:
        """Operator ``retry``: the cause was fixed outside the worker, so relaunch a
        fresh coder on the *same* spec — no rewrite spent, no speccer call. The
        note rides along as an ``## Operator note`` in the next prompt."""
        self._log(
            f"group {self.gid} generation {self.generation}: relaunching on the same spec "
            f"(operator retry: {why})"
        )
        if note:
            self._operator_notes.append(note)
        self.handoff_prompt = None  # the fresh session gets the unchanged spec
        if self.sessions_spawned:
            self._advance_generation()

    def _breaker_reason(self, rounds: int) -> str | None:
        if rounds >= self.deps.breaker.max_rounds_per_generation:
            return f"round threshold reached ({rounds} rounds this generation)"
        context = self.deps.runner.usage_of(self.coder_sid).last_context_tokens
        if context > self.deps.breaker.context_token_limit:
            return (
                f"context tokens {context} exceeded limit {self.deps.breaker.context_token_limit}"
            )
        return None

    def _log_coder_session_end(self) -> None:
        """One line per coder session with what it cost — the run log used to
        carry no cost at all, and four of six run notes said so."""
        if self.coder_entry is None:
            return
        entry = self.coder_entry
        rounds = "round" if entry.rounds_completed == 1 else "rounds"
        self._log(
            f"group {self.gid} generation {self.generation}: coder session ended — "
            f"{entry.rounds_completed} {rounds}, ${entry.total_cost_usd:.2f}"
        )

    async def _retire(self, reason: str) -> None:
        assert self.coder_entry is not None
        self.coder_entry.retirement_reason = reason
        self.deps.store.save(self.deps.manifest)
        self._log_coder_session_end()
        self._log(f"group {self.gid} generation {self.generation}: coder retired ({reason})")
        if self.generation >= self.deps.breaker.max_generations:
            # Terminal give-up: escalate before failing. An answer grants one more
            # (guided) generation; None fails as before.
            response = await self._escalate(
                EscalationKind.CAPS_EXHAUSTED,
                prompt=(
                    f"group {self.gid}: generation cap ({self.deps.breaker.max_generations}) "
                    f"exhausted — {reason}"
                ),
                want_diff=True,
            )
            if response is None:
                raise GroupFailure(
                    f"generation cap ({self.deps.breaker.max_generations}) exhausted: {reason}"
                )
            # `answer` and `retry` alike grant the one extra generation.
            self._grant_notes.append(f"[operator] {response.answer}")
        else:
            # interactive tier only: approve the breaker respawn.
            await self._approve_gate(
                EscalationKind.RESPAWN,
                f"respawn group {self.gid} at generation {self.generation + 1}?",
            )
        self._advance_generation()

    def _prepare_handoff(self, report: CoderReport, verdict: ReviewerVerdict) -> None:
        assert self.workspace is not None
        items = [f"- {change}" for change in verdict.required_changes]
        items += [f"- {note}" for note in self._grant_notes]  # operator guidance, if any
        self._grant_notes = []
        outstanding = "\n".join(items)
        self.handoff_prompt = render_handoff_prompt(
            self.deps.run_id,
            self.group,
            generation=self.generation,
            retirement_reason=self.coder_entry.retirement_reason or "retired",
            last_report=report.model_dump_json(indent=2),
            outstanding=outstanding,
            diff_summary=diff_stat(self.workspace, self.deps.base_ref_for(self.group)),
            decisions=self._decisions_text(),
        )

    # ------------------------------------------------------------ escalation

    async def _resolve_needs_input(self, report: CoderReport) -> RoundResult | None:
        """Coder ended its turn with a question. Returns the resumed RoundResult so
        the warm loop continues, or None when the generation is abandoned (an
        operator answer folded into a rewrite, or the question treated as a block
        when no human answered)."""
        assert self.workspace is not None
        self._questions += 1
        report_path = self.deps.store.save_group_artifact(
            self.gid, f"report-g{self.generation}-q{self._questions}.json", report
        )
        self._spread(report.surprises)
        question = report.question or report.summary or "(no question text)"
        # orchestrator_only owns the human channel: downgrade the question to the
        # blocked/rewrite path instead of a warm coder resume.
        downgraded = self.deps.policy is not None and self.deps.policy.source == "orchestrator_only"
        kind = EscalationKind.CODER_BLOCKED if downgraded else EscalationKind.CODER_QUESTION
        response = await self._escalate(
            kind,
            prompt=f'coder for {self.gid} needs input: "{question}"',
            report_path=str(report_path),
        )
        if response is not None and not downgraded:
            self.ctx.set_state(GroupState.RUNNING)
            answer_on_turn = self._make_coder_on_turn(self.coder_entry)
            return await self._worker_call(
                partial(
                    self.deps.runner.resume,
                    session_id=self.coder_sid,
                    prompt=render_coder_answer_prompt(response.answer),
                    cwd=self.workspace,
                    on_turn=answer_on_turn,
                ),
                recover=lambda sid: self.deps.runner.resume(
                    session_id=sid,
                    prompt=render_reentry_prompt(self.group),
                    cwd=self.workspace,
                    on_turn=answer_on_turn,
                ),
            )
        if _is_retry(response):
            await self._relaunch(response.answer, f"coder needs input: {question}")
            return None
        extra = [_context_surprise(self.gid, f"coder needs_input: {question}")]
        if response is not None:
            extra.append(_operator_surprise(self.gid, response.answer))
        await self._rewrite(f"coder needs input: {question}", extra=extra)
        return None

    async def _on_content_filtered(self, exc: ContentFiltered) -> None:
        """The API's content filter blocked a coder or reviewer round (plan
        U10) — routed like a blocked coder, quoting the last thing the model
        actually said, and never warm-resumed: the session that produced the
        blocked output is not one to retry as-is."""
        self._log(f"{self._round_tag(self._current_round_no)}: ended (content filter)")
        text = exc.last_assistant_text[:1000]
        response = await self._escalate(
            EscalationKind.CODER_BLOCKED,
            prompt=f'coder for {self.gid} hit the API content filter — last assistant message: "{text}"',
            want_diff=True,
        )
        if _is_retry(response):
            await self._relaunch(response.answer, "API content filter")
            return
        extra = [
            _context_surprise(
                self.gid,
                f"API content filter blocked the coder's output; last message: {text}",
            )
        ]
        if response is not None:
            extra.append(_operator_surprise(self.gid, response.answer))
        await self._rewrite("content filter", extra=extra)

    async def _on_coder_stuck(self, report: CoderReport, report_path: Path) -> None:
        """A blocked/failed coder report: escalate, then rewrite (guided if answered)
        — or relaunch the same spec when the operator says ``retry``."""
        response = await self._escalate(
            EscalationKind.CODER_BLOCKED,
            prompt=f"coder for {self.gid} reported {report.status}: {report.summary}",
            report_path=str(report_path),
            want_diff=True,
        )
        if _is_retry(response):
            await self._relaunch(response.answer, f"coder reported status {report.status}")
            return
        extra = [_context_surprise(self.gid, f"coder {report.status}: {report.summary}")]
        if response is not None:
            extra.append(_operator_surprise(self.gid, response.answer))
        await self._rewrite(f"coder reported status {report.status}", extra=extra)

    async def _on_reviewer_hard(self, verdict: ReviewerVerdict, verdict_path: Path | None) -> None:
        """A too_hard/structural verdict: escalate, then rewrite (guided if answered)
        — or relaunch the same spec when the operator says ``retry``."""
        kind = (
            EscalationKind.REVIEWER_TOO_HARD
            if verdict.status == "too_hard"
            else EscalationKind.REVIEWER_STRUCTURAL
        )
        response = await self._escalate(
            kind,
            prompt=f"reviewer for {self.gid} returned {verdict.status}: {verdict.notes}",
            verdict_path=str(verdict_path) if verdict_path is not None else None,
            want_diff=True,
        )
        if _is_retry(response):
            await self._relaunch(response.answer, f"reviewer verdict: {verdict.status}")
            return
        extra = [_context_surprise(self.gid, f"reviewer {verdict.status}: {verdict.notes}")]
        if response is not None:
            extra.append(_operator_surprise(self.gid, response.answer))
        await self._rewrite(f"reviewer verdict: {verdict.status}", extra=extra)

    async def _escalate(
        self,
        kind: EscalationKind,
        *,
        prompt: str,
        report_path: str | None = None,
        verdict_path: str | None = None,
        surprises: list[Surprise] | None = None,
        want_diff: bool = False,
    ) -> EscalationResponse | None:
        """Escalate ``kind`` to the operator if the policy dictates.

        Returns the operator's ``answer`` or ``retry`` response (callers that can
        relaunch the same spec check ``_is_retry``; every other site treats the two
        alike), or ``None`` when the caller must run its autonomous path —
        escalation is off for this kind, or a timeout with ``on_timeout =
        autonomous`` fired. ``skip`` (→ GroupFailure) and ``abort`` (→ RunAbort)
        are raised here and never returned. When broker/policy are
        absent the check short-circuits with zero side effects, so an autonomous run
        is byte-identical to pre-Phase-D."""
        policy, broker = self.deps.policy, self.deps.broker
        if broker is None or policy is None or not policy.should_escalate(kind):
            return None
        context = EscalationContext(
            report_path=report_path,
            verdict_path=verdict_path,
            diff_summary=self._diff() if want_diff else "",
            surprises=list(surprises or []),
        )
        request = EscalationRequest(
            id=uuid.uuid4().hex[:12],
            run_id=self.deps.run_id,
            group_id=self.gid,
            generation=self.generation,
            kind=kind,
            prompt=prompt,
            context=context,
        )
        response = await asyncio.to_thread(broker.raise_escalation, request)
        if response is None:
            return None  # timeout → autonomous fallback
        if response.action == HumanAction.ABORT:
            broker.trigger_abort()  # release every sibling waiter before we unwind
            raise RunAbort(f"operator aborted the run at group {self.gid} ({kind.value})")
        if response.action == HumanAction.SKIP:
            raise GroupFailure(f"operator skipped group {self.gid} ({kind.value})")
        return response

    async def _approve_gate(self, kind: EscalationKind, prompt: str) -> None:
        """Interactive-tier approval gate: an ``answer`` or ``retry`` (or a
        non-escalating tier) means proceed; ``skip``/``abort`` raise inside
        ``_escalate``."""
        await self._escalate(kind, prompt=prompt)

    def _diff(self) -> str:
        if self.workspace is None:
            return ""
        return diff_stat(self.workspace, self.deps.base_ref_for(self.group))

    def _log(self, text: str) -> None:
        """Append to the run's always-on lifecycle log (R10): control-plane events
        land in ``run.log`` in every run mode, HITL or autonomous."""
        log_event(self.deps.store.paths, text)

    def _persist_rewritten_spec(self) -> None:
        """Save the rewritten spec beside the group so a post-mortem can
        reconstruct what the coder was actually told (plan U14) — the group's
        entry in ``groups.json`` stays the immutable grouper output and is
        never rewritten. These ``spec-gen<N>.json`` files are the durable
        record every restart path reads back through ``effective_group``
        (resume, ``finish``, ``retry``), so this must run before the rewritten
        group's next worktree is created — ``_rewrite`` calls it synchronously,
        before any new generation launches."""
        path = self.deps.store.paths.group_dir(self.gid) / f"spec-gen{self.generation}.json"
        atomic_write_text(path, self.group.model_dump_json(indent=2) + "\n")

    def _round_tag(self, round_no: int) -> str:
        # Side effect: records the round currently in flight (plan U10), so
        # `_on_content_filtered` can log the round a `ContentFiltered` ended
        # without a second, separate tracker to keep in sync with this one.
        self._current_round_no = round_no
        return f"group {self.gid} generation {self.generation} round {round_no}"

    # ------------------------------------------------------------ bookkeeping

    def _advance_generation(self) -> None:
        self.generation += 1
        self.ctx.set_generation(self.generation)

    def _record(self, role: SessionRole, session_id: str) -> SessionEntry:
        # started_at/model land here, at creation (plan U4) — not only once the
        # first round completes — so an in-flight group is distinguishable from
        # one that never started, and a crash before any round finishes still
        # leaves a manifest entry an operator can date.
        entry = SessionEntry(
            session_id=session_id,
            role=role,
            generation=self.generation,
            name=session_display_name(self.deps.run_id, self.gid, role.value, self.generation),
            transcript_path=_transcript_str(self.deps.runner, session_id),
            started_at=datetime.now(UTC).isoformat(),
            model=self.deps.runner.model,
        )
        record_session(
            self.deps.manifest,
            group_id=self.gid,
            group_name=self.group.name,
            summary=self.group.summary,
            entry=entry,
        )
        self.deps.store.save(self.deps.manifest)
        self.sessions_spawned += 1
        return entry


def _transcript_str(runner: SessionRunner, session_id: str) -> str | None:
    path = runner.transcript_path(session_id)
    return str(path) if path is not None else None


def _context_surprise(group_id: str, description: str) -> Surprise:
    """Escalation context handed to the speccer when the group itself triggered
    the rewrite (blocked/too_hard/structural) and no upstream surprise exists."""
    return Surprise(kind="other", description=description, affected_groups=[group_id])


def _spec_hash(group: Group) -> str:
    """Identity of the spec a coder was launched on (see ``SessionEntry.spec_sha256``)."""
    return hashlib.sha256(group.model_dump_json().encode()).hexdigest()
