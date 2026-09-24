"""The generation lifecycle: one coder session's whole run from launch (or
warm re-entry) through rounds, the breaker, and the terminal handoff into the
next generation (plan U5 of the review-loop split).

`GenerationLoop` is mixed onto `_GroupExecution`, whose `__init__` is the
only place the attributes below are born (see `ExecutionHost`).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.execution.artifacts import ArtifactEntry, render_artifact_inputs_block
from orchestrator.execution.denial import classify_denial, denial_remedy
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.manifest import artifact_name, completed_round_count
from orchestrator.execution.prompting import (
    render_coder_prompt,
    render_handoff_prompt,
    render_reentry_prompt,
    render_revision_prompt,
)
from orchestrator.execution.records import _spec_hash
from orchestrator.execution.scheduler import GroupContext, GroupFailure, GroupState
from orchestrator.execution.sessions import (
    ContentFiltered,
    RoundResult,
    SessionError,
    SuspendCured,
    UsageLimit,
    nudge_until_report,
    session_display_name,
)
from orchestrator.execution.worktrees import diff_stat
from orchestrator.model import (
    CoderReport,
    EscalationKind,
    Group,
    PermissionDenied,
    ReviewerVerdict,
    SessionEntry,
    SessionRole,
    unmet_required_verification,
)

if TYPE_CHECKING:
    from orchestrator.execution.review import ReviewDeps

_logger = logging.getLogger(__name__)


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


class GenerationLoop:
    """One group's generations: launch/re-entry, the coder-reviewer round
    loop, the breaker, and the handoff into the next generation."""

    deps: ReviewDeps
    ctx: GroupContext
    group: Group
    gid: str
    generation: int
    sessions_spawned: int
    handoff_prompt: str | None
    workspace: Path | None
    coder_sid: str
    coder_entry: SessionEntry | None
    reviewer_sid: str | None
    _env_failure: str | None
    _grant_notes: list[str]
    _flake_reruns: int
    _reentry_entry: SessionEntry | None
    _heartbeat: RoundHeartbeat
    _log: Callable[[str], None]
    _round_tag: Callable[[int], str]
    _record: Callable[..., SessionEntry]
    _spread: Callable[..., None]
    _persist_coder_usage: Callable[[], None]
    _make_coder_on_turn: Callable[[SessionEntry | None], object]
    _adopt_actual_session_id: Callable[[RoundResult], None]
    _refresh_transcript: Callable[[SessionEntry], None]
    _watch_transcript: Callable[[SessionEntry], None]
    _log_coder_session_end: Callable[[], None]
    _decisions_text: Callable[[], str]
    _resolve_needs_input: Callable[[CoderReport], Awaitable[RoundResult | None]]
    _on_content_filtered: Callable[[ContentFiltered], Awaitable[None]]
    _on_coder_stuck: Callable[[CoderReport, Path], Awaitable[None]]
    _on_reviewer_hard: Callable[[ReviewerVerdict, Path | None], Awaitable[None]]
    _review_round: Callable[..., Awaitable[tuple[ReviewerVerdict | None, Path | None]]]
    _handle_pending_surprises: Callable[[str], Awaitable[bool]]
    _apply_briefing: Callable[[str], str]
    _apply_env_notice: Callable[[str], str]
    _apply_operator_note: Callable[[str], str]
    _last_report: CoderReport | None
    _approve_gate: Callable[..., Awaitable[None]]
    _escalate: Callable[..., Awaitable[object]]
    _merge: Callable[[], Awaitable[bool]]
    _advance_generation: Callable[[], None]

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

    def _upstream_artifact_entries(self) -> list[ArtifactEntry]:
        """Registered entries for every direct upstream group whose recipe is
        not `code`, in `Group.dependencies` order (plan U6 Goal). Empty when
        no Artifact Manifest is wired (`deps.artifacts is None`, every
        construction site that predates this unit) or none of the group's
        direct upstreams is a non-`code` recipe."""
        store = self.deps.artifacts
        if store is None:
            return []
        entries: list[ArtifactEntry] = []
        for dep_id in self.group.dependencies:
            upstream = self.deps.groups_by_id.get(dep_id)
            if upstream is None or upstream.recipe == "code":
                continue
            entry = store.get(dep_id)
            if entry is not None:
                entries.append(entry)
        return entries

    def _apply_artifact_inputs(self, prompt: str) -> str:
        """Fold non-`code` direct upstreams' Artifact Manifest entries into
        `prompt` (plan U6 Goal). A no-op — byte-identical `prompt` — when
        there is nothing to fold in, so a code-only plan's prompts are
        unchanged."""
        block = render_artifact_inputs_block(self._upstream_artifact_entries())
        if not block:
            return prompt
        return f"{prompt}\n\n{block}"

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
            prompt = self._apply_artifact_inputs(prompt)
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
                self._last_report = report
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

    # ------------------------------------------------------------ outcomes

    def _breaker_reason(self, rounds: int) -> str | None:
        if rounds >= self.deps.breaker.max_rounds_per_generation:
            return f"round threshold reached ({rounds} rounds this generation)"
        context = self.deps.runner.usage_of(self.coder_sid).last_context_tokens
        if context > self.deps.breaker.context_token_limit:
            return (
                f"context tokens {context} exceeded limit {self.deps.breaker.context_token_limit}"
            )
        return None

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
