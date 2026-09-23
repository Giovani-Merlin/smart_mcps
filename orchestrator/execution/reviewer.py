"""Reviewer round: the reviewer ferry and its scratch archive (plan U4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.execution.manifest import archive_review_scratch, artifact_name
from orchestrator.execution.prompting import (
    REVIEW_SCRATCH_DIRNAME,
    render_extra_pass_prompt,
    render_re_review_prompt,
    render_reviewer_prompt,
)
from orchestrator.execution.scheduler import GroupState
from orchestrator.execution.sessions import RoundResult, nudge_until_report, session_display_name
from orchestrator.execution.worktrees import ensure_excluded
from orchestrator.model import Group, ReviewerVerdict, ReviewIntensity, SessionRole


if TYPE_CHECKING:
    from orchestrator.execution.review import ReviewDeps


class ReviewerRound:
    deps: ReviewDeps
    group: Group
    gid: str
    generation: int
    workspace: Path | None
    reviewer_sid: str | None
    extra_pass_done: bool
    _log: Callable[[str], None]
    _round_tag: Callable[[int], str]
    _record: Callable[..., object]
    _spread: Callable[..., None]
    _worker_call: Callable[..., Awaitable[object]]
    _launch_call: Callable[[], Callable[..., RoundResult]]
    _decisions_text: Callable[[], str]
    _persist_reviewer_usage: Callable[[str], None]

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
