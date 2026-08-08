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
import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.escalation import EscalationBroker, EscalationPolicy
from orchestrator.execution.manifest import (
    ManifestStore,
    RunPaths,
    artifact_name,
    atomic_write_text,
    completed_round_count,
    log_event,
    record_session,
)
from orchestrator.execution.prompting import (
    render_coder_answer_prompt,
    render_coder_prompt,
    render_conflict_resolve_prompt,
    render_extra_pass_prompt,
    render_handoff_prompt,
    render_re_review_prompt,
    render_reentry_prompt,
    render_reviewer_prompt,
    render_revision_prompt,
)
from orchestrator.execution.scheduler import Executor, GroupContext, GroupState, RunAbort
from orchestrator.execution.sessions import (
    RoundResult,
    SessionError,
    SessionRunner,
    nudge_until_report,
    session_display_name,
)
from orchestrator.execution.worktrees import diff_stat, integration_branch
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
)


class GroupFailure(Exception):
    """The group exhausted its bounds; surfaced to the operator, never retried."""


class MergeConflict(Exception):
    """Raised by the merge seam (U8). Routes the merging group to rewriting and
    fans a surprise out to the groups involved."""

    def __init__(self, message: str, affected_groups: list[str] | None = None):
        super().__init__(message)
        self.affected_groups = affected_groups or []


class SurpriseBoard:
    """Cross-group surprise registry. A mark is consumed by the named group's
    executor at its next checkpoint (before launch, or before accepting an
    approval); marks for completed/failed groups are simply never read.

    Persisted to the run directory when constructed with ``paths`` (plan U7):
    a plain in-memory dict dies with the process, silently dropping a surprise
    marked for a group that has not yet run. ``paths=None`` keeps every
    existing in-process test byte-identical.
    """

    def __init__(self, paths: RunPaths | None = None) -> None:
        self._paths = paths
        self._lock = threading.Lock()
        self._pending: dict[str, list[Surprise]] = {}
        if paths is not None and paths.surprises_path.is_file():
            raw = json.loads(paths.surprises_path.read_text())
            self._pending = {
                gid: [Surprise.model_validate(item) for item in items] for gid, items in raw.items()
            }

    def mark(self, surprise: Surprise, *, source_group: str | None = None) -> None:
        with self._lock:
            for gid in surprise.affected_groups:
                if gid != source_group:
                    self._pending.setdefault(gid, []).append(surprise)
            self._persist()

    def pending_for(self, group_id: str) -> list[Surprise]:
        with self._lock:
            return list(self._pending.get(group_id, []))

    def consume(self, group_id: str) -> list[Surprise]:
        with self._lock:
            surprises = self._pending.pop(group_id, [])
            if surprises:
                self._persist()
            return surprises

    def _persist(self) -> None:
        if self._paths is None:
            return
        payload = {
            gid: [surprise.model_dump() for surprise in surprises]
            for gid, surprises in self._pending.items()
        }
        atomic_write_text(self._paths.surprises_path, json.dumps(payload, indent=2) + "\n")


@dataclass
class ReviewDeps:
    """Everything a group's review loop needs; seams injected so tests script
    sessions in-process and U8/U9 wire worktrees, merges, and the speccer."""

    run_id: str
    runner: SessionRunner
    store: ManifestStore
    manifest: RunManifest
    base_session_id: str
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


def make_executor(deps: ReviewDeps) -> Executor:
    """The scheduler-facing executor: runs one group to a terminal state."""

    async def executor(ctx: GroupContext) -> GroupState:
        return await _GroupExecution(deps, ctx).run()

    return executor


class _GroupExecution:
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
        # Re-entry discovery (R4): a live coder entry at the persisted generation
        # can only pre-exist the executor on a resumed run — fresh runs start with
        # an empty group entry. One-shot: consumed by the first generation.
        self._reentry_entry: SessionEntry | None = self._find_reentry_session()

    async def run(self) -> GroupState:
        # interactive tier only: approve before anything is launched.
        await self._approve_gate(
            EscalationKind.GROUP_START, f"launch group {self.gid} ({self.group.name})?"
        )
        if self.deps.board.pending_for(self.gid):
            await self._rewrite("upstream surprise named this group before launch")
        self.workspace = self.deps.workspace_for(self.group)
        self._log(f"group {self.gid}: worktree ready at {self.workspace}")
        while True:
            merged = await self._run_generation()
            if merged:
                self._log(f"group {self.gid}: completed")
                return GroupState.COMPLETED
            # a rewrite or a retirement happened inside; loop spawns the next session

    # ------------------------------------------------------------ generation

    async def _run_generation(self) -> bool:
        """One coder session's lifetime. True → merged; False → respawn/rewritten."""
        assert self.workspace is not None
        self.ctx.set_state(GroupState.RUNNING)
        first: RoundResult | None = None
        reentry, self._reentry_entry = self._reentry_entry, None  # one-shot
        is_reentry = reentry is not None
        if reentry is not None:
            first = await self._reenter(reentry)
        if first is None:
            prompt = self.handoff_prompt or render_coder_prompt(self.deps.run_id, self.group)
            self.handoff_prompt = None
            # The session id is generated and recorded *before* the blocking fork
            # call, not after (plan U7): a crash mid-call would otherwise leave no
            # manifest entry for a group interrupted during its very first round,
            # so a later resume would fork a brand new session instead of finding
            # the one already under way.
            self.coder_sid = str(uuid.uuid4())
            self.reviewer_sid = None
            self.coder_entry = self._record(SessionRole.CODER, self.coder_sid)
            first = await asyncio.to_thread(
                self.deps.runner.start_fork,
                base_id=self.deps.base_session_id,
                prompt=prompt,
                name=session_display_name(self.deps.run_id, self.gid, "coder", self.generation),
                cwd=self.workspace,
                session_id=self.coder_sid,
            )
            self._refresh_transcript(self.coder_entry)
            self._log(f"group {self.gid} generation {self.generation}: coder launched")
        # Re-entry (warm-resumed or fallback-forked) continues this generation's
        # numbering rather than starting over, so round-numbered artifacts don't
        # collide with — and silently overwrite — pre-crash ones still on disk.
        rounds = (
            completed_round_count(self.deps.store.paths, self.gid, self.generation)
            if is_reentry
            else 0
        )
        result = first
        self._log(f"{self._round_tag(rounds + 1)}: started")

        while True:
            report, result = await asyncio.to_thread(
                nudge_until_report, self.deps.runner, result, CoderReport, cwd=self.workspace
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
                self._log(f"{self._round_tag(rounds)}: ended (permission_denied)")
                raise PermissionDenied(f"group {self.gid} denied command: {report.denied_command}")
            if report.status != "completed":
                self._log(f"{self._round_tag(rounds)}: ended (coder {report.status})")
                await self._on_coder_stuck(report, report_path)
                return False

            verdict, verdict_path = await self._review_round(report_path, rounds)
            if verdict is None or verdict.status == "approved":
                outcome = "self-verified" if verdict is None else "approved"
                self._log(f"{self._round_tag(rounds)}: ended ({outcome})")
                if self.deps.board.pending_for(self.gid):
                    # A surprise named this group while it was in review: its
                    # pending approval is not accepted (plan U7 scenario).
                    await self._rewrite("surprise named this group during review")
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
            result = await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=self.coder_sid,
                prompt=render_revision_prompt(str(verdict_path), verdict.required_changes),
                cwd=self.workspace,
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
        return live[-1] if live else None

    async def _reenter(self, entry: SessionEntry) -> RoundResult | None:
        """Warm-resume the interrupted coder in its worktree (R4). Returns the
        resumed round, or None to fall through to a fresh fork from base — when
        the persisted context already exceeds the breaker limit (R5) or the warm
        resume itself fails at the envelope. A SessionError from that fork
        propagates: the group lands interrupted again, since the envelope is
        still failing (no in-run retry loop). Exactly one re-entry lifecycle
        line is written either way (R6)."""
        assert self.workspace is not None
        limit = self.deps.breaker.context_token_limit
        if entry.last_context_tokens > limit:
            self._reentry_fallback(
                entry, f"context tokens {entry.last_context_tokens} exceed limit {limit}"
            )
            return None
        try:
            result = await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=entry.session_id,
                prompt=render_reentry_prompt(self.group),
                cwd=self.workspace,
            )
        except SessionError as exc:
            self._reentry_fallback(entry, f"warm resume failed: {exc}")
            return None
        self.coder_sid = entry.session_id
        self.coder_entry = entry
        self.reviewer_sid = None
        self.sessions_spawned += 1  # live session again: a later rewrite respawns fresh
        self._log(f"group {self.gid} re-entry: resumed session {entry.session_id}")
        return result

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

    def _persist_coder_usage(self) -> None:
        """Record the active coder's latest context size on its manifest entry
        after every round (R5): in-memory usage dies with the process, and the
        next re-entry pre-checks this value against the breaker limit."""
        if self.coder_entry is None:
            return
        self._copy_usage(self.coder_entry, self.coder_sid)
        self.deps.store.save(self.deps.manifest)

    def _refresh_transcript(self, entry: SessionEntry) -> None:
        """Fill in a pre-registered entry's transcript path once its session
        actually exists on disk (plan U7) — recorded before the fork call, so
        the path itself isn't known until the call returns."""
        entry.transcript_path = _transcript_str(self.deps.runner, entry.session_id)
        self.deps.store.save(self.deps.manifest)

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
        if self.reviewer_sid is None:
            first = await asyncio.to_thread(
                self.deps.runner.start_fork,
                base_id=self.deps.base_session_id,
                prompt=render_reviewer_prompt(
                    self.deps.run_id,
                    self.group,
                    report_path=str(report_path),
                    base_ref=self.deps.base_ref_for(self.group),
                ),
                name=session_display_name(self.deps.run_id, self.gid, "reviewer", self.generation),
                cwd=self.workspace,
            )
            self.reviewer_sid = first.session_id
            self._record(SessionRole.REVIEWER, first.session_id)
            result = first
        else:
            result = await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=self.reviewer_sid,
                prompt=render_re_review_prompt(str(report_path)),
                cwd=self.workspace,
            )
        verdict, result = await asyncio.to_thread(
            nudge_until_report, self.deps.runner, result, ReviewerVerdict, cwd=self.workspace
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
            result = await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=self.reviewer_sid,
                prompt=render_extra_pass_prompt(),
                cwd=self.workspace,
            )
            verdict, result = await asyncio.to_thread(
                nudge_until_report, self.deps.runner, result, ReviewerVerdict, cwd=self.workspace
            )
            self._persist_reviewer_usage(self.reviewer_sid)
            verdict_path = self.deps.store.save_group_artifact(
                self.gid, f"verdict-g{self.generation}-r{rounds}-extra.json", verdict
            )
            self._spread(verdict.surprises)
            self._log(f"{self._round_tag(rounds)}: reviewer verdict {verdict.status} (extra pass)")
        return verdict, verdict_path

    # ------------------------------------------------------------ outcomes

    async def _merge(self) -> bool:
        assert self.workspace is not None
        attempts_left = self.deps.execution.max_conflict_resolve_attempts
        while True:
            self.ctx.set_state(GroupState.MERGING)
            self._log(f"group {self.gid}: merge attempt")
            try:
                await asyncio.to_thread(self.deps.merge_group, self.group, self.workspace)
            except MergeConflict as exc:
                self._log(f"group {self.gid}: merge conflict ({exc})")
                conflict = Surprise(
                    kind="merge_conflict", description=str(exc), affected_groups=exc.affected_groups
                )
                self._spread([conflict])
                if attempts_left > 0:
                    attempts_left -= 1
                    if await self._resolve_conflict_in_place(exc):
                        continue  # retry the merge with the resolved worktree
                response = await self._escalate(
                    EscalationKind.MERGE_CONFLICT,
                    prompt=f"merge conflict for {self.gid}: {exc}",
                    surprises=[conflict],
                )
                extra = [conflict]
                if response is not None:
                    extra.append(_operator_surprise(self.gid, response.answer))
                await self._rewrite(f"merge conflict: {exc}", extra=extra)
                return False
            self._log(f"group {self.gid}: merged into the integration branch")
            return True

    async def _resolve_conflict_in_place(self, exc: MergeConflict) -> bool:
        """One warm-resume attempt at the group's own coder session (plan U1),
        tried before falling back to a full spec rewrite: the session that just
        built this work still holds full context of it. Returns True when the
        coder finished cleanly and the merge should be retried; False when the
        resume/report itself failed, so the caller falls straight through to
        escalate-then-rewrite without a second merge attempt."""
        assert self.workspace is not None
        self._log(f"group {self.gid}: attempting in-place conflict resolution")
        try:
            result = await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=self.coder_sid,
                prompt=render_conflict_resolve_prompt(
                    self.group,
                    conflict_summary=str(exc),
                    integration_branch=integration_branch(self.deps.run_id),
                ),
                cwd=self.workspace,
            )
            report, _ = await asyncio.to_thread(
                nudge_until_report, self.deps.runner, result, CoderReport, cwd=self.workspace
            )
        except SessionError as inner_exc:
            self._log(f"group {self.gid}: conflict resolve attempt failed: {inner_exc}")
            return False
        self._persist_coder_usage()
        self._spread(report.surprises)
        if report.status != "completed":
            self._log(f"group {self.gid}: conflict resolve ended ({report.status})")
            return False
        self._log(f"group {self.gid}: conflict resolve attempt reported completed")
        return True

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
            extra.append(_operator_surprise(self.gid, response.answer))
        surprises = self.deps.board.consume(self.gid) + extra
        self.group = await asyncio.to_thread(self.deps.rewrite_spec, self.group, surprises)
        self.rewrites += 1
        self.handoff_prompt = None  # the fresh session gets the rewritten spec
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

    async def _retire(self, reason: str) -> None:
        assert self.coder_entry is not None
        self.coder_entry.retirement_reason = reason
        self.deps.store.save(self.deps.manifest)
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
            return await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=self.coder_sid,
                prompt=render_coder_answer_prompt(response.answer),
                cwd=self.workspace,
            )
        extra = [_context_surprise(self.gid, f"coder needs_input: {question}")]
        if response is not None:
            extra.append(_operator_surprise(self.gid, response.answer))
        await self._rewrite(f"coder needs input: {question}", extra=extra)
        return None

    async def _on_coder_stuck(self, report: CoderReport, report_path: Path) -> None:
        """A blocked/failed coder report: escalate, then rewrite (guided if answered)."""
        response = await self._escalate(
            EscalationKind.CODER_BLOCKED,
            prompt=f"coder for {self.gid} reported {report.status}: {report.summary}",
            report_path=str(report_path),
            want_diff=True,
        )
        extra = [_context_surprise(self.gid, f"coder {report.status}: {report.summary}")]
        if response is not None:
            extra.append(_operator_surprise(self.gid, response.answer))
        await self._rewrite(f"coder reported status {report.status}", extra=extra)

    async def _on_reviewer_hard(self, verdict: ReviewerVerdict, verdict_path: Path | None) -> None:
        """A too_hard/structural verdict: escalate, then rewrite (guided if answered)."""
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

        Returns the operator's ``answer`` response, or ``None`` when the caller must
        run its autonomous path — escalation is off for this kind, or a timeout with
        ``on_timeout = autonomous`` fired. ``skip`` (→ GroupFailure) and ``abort``
        (→ RunAbort) are raised here and never returned. When broker/policy are
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
        """Interactive-tier approval gate: an ``answer`` (or a non-escalating tier)
        means proceed; ``skip``/``abort`` raise inside ``_escalate``."""
        await self._escalate(kind, prompt=prompt)

    def _diff(self) -> str:
        if self.workspace is None:
            return ""
        return diff_stat(self.workspace, self.deps.base_ref_for(self.group))

    def _log(self, text: str) -> None:
        """Append to the run's always-on lifecycle log (R10): control-plane events
        land in ``run.log`` in every run mode, HITL or autonomous."""
        log_event(self.deps.store.paths, text)

    def _round_tag(self, round_no: int) -> str:
        return f"group {self.gid} generation {self.generation} round {round_no}"

    # ------------------------------------------------------------ bookkeeping

    def _spread(self, surprises: list[Surprise]) -> None:
        """Fan surprises out to the groups they name — never back at the source."""
        for surprise in surprises:
            self.deps.board.mark(surprise, source_group=self.gid)

    def _advance_generation(self) -> None:
        self.generation += 1
        self.ctx.set_generation(self.generation)

    def _record(self, role: SessionRole, session_id: str) -> SessionEntry:
        entry = SessionEntry(
            session_id=session_id,
            role=role,
            generation=self.generation,
            name=session_display_name(self.deps.run_id, self.gid, role.value, self.generation),
            transcript_path=_transcript_str(self.deps.runner, session_id),
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


def _operator_surprise(group_id: str, answer: str) -> Surprise:
    """Fold an operator's free-text guidance into the next rewrite as a surprise —
    no ``rewrite_spec`` signature change (plan Phase D)."""
    return Surprise(kind="other", description=f"[operator] {answer}", affected_groups=[group_id])
