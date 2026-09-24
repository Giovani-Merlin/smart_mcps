"""Escalation, approval gates, the coder-question channel, rewrite and
relaunch (plan U3 of the review-loop split).

`EscalationHandlers` is mixed onto `_GroupExecution`, whose `__init__` is the
only place the attributes below are born (see `ExecutionHost`). The module
helpers are shared with `merge_ladder.py`, the one inter-mixin import edge.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.execution.decisions import decision_ledger, render_decisions_section
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.manifest import atomic_write_text
from orchestrator.execution.prompting import render_coder_answer_prompt, render_reentry_prompt
from orchestrator.execution.scheduler import GroupContext, GroupFailure, GroupState, RunAbort
from orchestrator.execution.sessions import ContentFiltered, RoundResult
from orchestrator.execution.worktrees import diff_stat
from orchestrator.model import (
    CoderReport,
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    Group,
    HumanAction,
    ReviewerVerdict,
    SessionEntry,
    Surprise,
)

if TYPE_CHECKING:
    from orchestrator.execution.review import ReviewDeps


class EscalationHandlers:
    """Escalation broker calls, approval gates, rewrite and relaunch."""

    deps: ReviewDeps
    ctx: GroupContext
    group: Group
    gid: str
    generation: int
    workspace: Path | None
    coder_sid: str
    coder_entry: SessionEntry | None
    handoff_prompt: str | None
    rewrites: int
    sessions_spawned: int
    _questions: int
    _operator_notes: list[str]
    _current_round_no: int
    _heartbeat: RoundHeartbeat
    _log: Callable[[str], None]
    _round_tag: Callable[[int], str]
    _spread: Callable[[list[Surprise]], None]
    _worker_call: Callable[..., Awaitable[RoundResult]]
    _make_coder_on_turn: Callable[[SessionEntry | None], object]

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

    def _decisions_text(self) -> str:
        """Binding operator decisions for this group, rendered fresh from disk
        at every prompt build (plan U9) so a later answer reaches every
        subsequent coder, handoff, reviewer and re-review prompt."""
        return render_decisions_section(decision_ledger(self.deps.store.paths, self.gid))

    def _advance_generation(self) -> None:
        self.generation += 1
        self.ctx.set_generation(self.generation)


def _context_surprise(group_id: str, description: str) -> Surprise:
    """Escalation context handed to the speccer when the group itself triggered
    the rewrite (blocked/too_hard/structural) and no upstream surprise exists."""
    return Surprise(kind="other", description=description, affected_groups=[group_id])


def _is_retry(response: EscalationResponse | None) -> bool:
    return response is not None and response.action == HumanAction.RETRY


def _operator_surprise(group_id: str, answer: str) -> Surprise:
    """Fold an operator's free-text guidance into the next rewrite as a surprise —
    no ``rewrite_spec`` signature change (plan Phase D)."""
    return Surprise(kind="other", description=f"[operator] {answer}", affected_groups=[group_id])
