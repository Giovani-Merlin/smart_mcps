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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.manifest import ManifestStore, artifact_name, record_session
from orchestrator.execution.prompting import (
    render_coder_prompt,
    render_extra_pass_prompt,
    render_handoff_prompt,
    render_re_review_prompt,
    render_reviewer_prompt,
    render_revision_prompt,
)
from orchestrator.execution.scheduler import Executor, GroupContext, GroupState
from orchestrator.execution.sessions import (
    SessionRunner,
    nudge_until_report,
    session_display_name,
)
from orchestrator.execution.worktrees import diff_stat
from orchestrator.model import (
    CoderReport,
    Group,
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
    approval); marks for completed/failed groups are simply never read."""

    def __init__(self) -> None:
        self._pending: dict[str, list[Surprise]] = {}
        self._lock = threading.Lock()

    def mark(self, surprise: Surprise, *, source_group: str | None = None) -> None:
        with self._lock:
            for gid in surprise.affected_groups:
                if gid != source_group:
                    self._pending.setdefault(gid, []).append(surprise)

    def pending_for(self, group_id: str) -> list[Surprise]:
        with self._lock:
            return list(self._pending.get(group_id, []))

    def consume(self, group_id: str) -> list[Surprise]:
        with self._lock:
            return self._pending.pop(group_id, [])


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

    async def run(self) -> GroupState:
        if self.deps.board.pending_for(self.gid):
            await self._rewrite("upstream surprise named this group before launch")
        self.workspace = self.deps.workspace_for(self.group)
        while True:
            merged = await self._run_generation()
            if merged:
                return GroupState.COMPLETED
            # a rewrite or a retirement happened inside; loop spawns the next session

    # ------------------------------------------------------------ generation

    async def _run_generation(self) -> bool:
        """One coder session's lifetime. True → merged; False → respawn/rewritten."""
        assert self.workspace is not None
        self.ctx.set_state(GroupState.RUNNING)
        prompt = self.handoff_prompt or render_coder_prompt(self.deps.run_id, self.group)
        self.handoff_prompt = None
        first = await asyncio.to_thread(
            self.deps.runner.start_fork,
            base_id=self.deps.base_session_id,
            prompt=prompt,
            name=session_display_name(self.deps.run_id, self.gid, "coder", self.generation),
            cwd=self.workspace,
        )
        self.coder_sid = first.session_id
        self.coder_entry = self._record(SessionRole.CODER, first.session_id)
        self.reviewer_sid = None
        rounds = 0
        result = first

        while True:
            report, result = await asyncio.to_thread(
                nudge_until_report, self.deps.runner, result, CoderReport, cwd=self.workspace
            )
            rounds += 1
            report_path = self.deps.store.save_group_artifact(
                self.gid, artifact_name("report", self.generation, rounds), report
            )
            self._spread(report.surprises)
            if report.status != "completed":
                await self._rewrite(
                    f"coder reported status {report.status}",
                    extra=[_context_surprise(self.gid, f"coder {report.status}: {report.summary}")],
                )
                return False

            verdict, verdict_path = await self._review_round(report_path, rounds)
            if verdict is None or verdict.status == "approved":
                if self.deps.board.pending_for(self.gid):
                    # A surprise named this group while it was in review: its
                    # pending approval is not accepted (plan U7 scenario).
                    await self._rewrite("surprise named this group during review")
                    return False
                return await self._merge()
            if verdict.status in ("too_hard", "structural"):
                await self._rewrite(
                    f"reviewer verdict: {verdict.status}",
                    extra=[
                        _context_surprise(self.gid, f"reviewer {verdict.status}: {verdict.notes}")
                    ],
                )
                return False

            # changes_required — breaker gate before the next warm round
            reason = self._breaker_reason(rounds)
            if reason:
                self._retire(reason)
                self._prepare_handoff(report, verdict)
                return False
            assert verdict_path is not None
            self.ctx.set_state(GroupState.RUNNING)
            result = await asyncio.to_thread(
                self.deps.runner.resume,
                session_id=self.coder_sid,
                prompt=render_revision_prompt(str(verdict_path), verdict.required_changes),
                cwd=self.workspace,
            )

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
        verdict_path = self.deps.store.save_group_artifact(
            self.gid, artifact_name("verdict", self.generation, rounds), verdict
        )
        self._spread(verdict.surprises)

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
            verdict_path = self.deps.store.save_group_artifact(
                self.gid, f"verdict-g{self.generation}-r{rounds}-extra.json", verdict
            )
            self._spread(verdict.surprises)
        return verdict, verdict_path

    # ------------------------------------------------------------ outcomes

    async def _merge(self) -> bool:
        assert self.workspace is not None
        self.ctx.set_state(GroupState.MERGING)
        try:
            await asyncio.to_thread(self.deps.merge_group, self.group, self.workspace)
        except MergeConflict as exc:
            conflict = Surprise(
                kind="merge_conflict", description=str(exc), affected_groups=exc.affected_groups
            )
            self._spread([conflict])
            await self._rewrite(f"merge conflict: {exc}", extra=[conflict])
            return False
        return True

    async def _rewrite(self, why: str, extra: list[Surprise] | None = None) -> None:
        self.ctx.set_state(GroupState.REWRITING)
        if self.rewrites >= self.deps.execution.max_rewrites:
            raise GroupFailure(f"rewrite cap ({self.deps.execution.max_rewrites}) exhausted: {why}")
        surprises = self.deps.board.consume(self.gid) + list(extra or [])
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

    def _retire(self, reason: str) -> None:
        assert self.coder_entry is not None
        self.coder_entry.retirement_reason = reason
        self.deps.store.save(self.deps.manifest)
        if self.generation >= self.deps.breaker.max_generations:
            raise GroupFailure(
                f"generation cap ({self.deps.breaker.max_generations}) exhausted: {reason}"
            )
        self._advance_generation()

    def _prepare_handoff(self, report: CoderReport, verdict: ReviewerVerdict) -> None:
        assert self.workspace is not None
        outstanding = "\n".join(f"- {change}" for change in verdict.required_changes)
        self.handoff_prompt = render_handoff_prompt(
            self.deps.run_id,
            self.group,
            generation=self.generation,
            retirement_reason=self.coder_entry.retirement_reason or "retired",
            last_report=report.model_dump_json(indent=2),
            outstanding=outstanding,
            diff_summary=diff_stat(self.workspace, self.deps.base_ref_for(self.group)),
        )

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
