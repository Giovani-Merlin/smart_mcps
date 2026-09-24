"""The merge gate and everything that fires when it fails: the untracked
ladder, the one-shot flake re-run, preflight classification and in-place
conflict resolution (plan U2 of the review-loop split).

`MergeLadder` is mixed onto `_GroupExecution`, whose `__init__` is the only
place the attributes below are born (see `ExecutionHost`).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.execution.artifacts import ARTIFACT_SUMMARY_MAX_CHARS, ArtifactEntry
from orchestrator.execution.escalating import _is_retry, _operator_surprise
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.merge import MergeConflict
from orchestrator.execution.manifest import archive_review_scratch
from orchestrator.execution.preflight import (
    PreflightFailure,
    compare_to_baseline,
    failing_tests_from_junit,
)
from orchestrator.execution.prompting import (
    CODER_SCRATCH_DIRNAME,
    render_conflict_resolve_prompt,
)
from orchestrator.execution.scheduler import GroupContext, GroupFailure, GroupState
from orchestrator.execution.sessions import SessionError, nudge_until_report
from orchestrator.execution.worktrees import changed_paths, ensure_excluded, integration_branch
from orchestrator.model import (
    CoderReport,
    EscalationKind,
    EscalationResponse,
    Group,
    SessionEntry,
    Surprise,
)

if TYPE_CHECKING:
    from orchestrator.execution.review import ReviewDeps


class MergeLadder:
    """Merge gate, untracked ladder, flake re-run and conflict resolution."""

    deps: ReviewDeps
    ctx: GroupContext
    group: Group
    gid: str
    generation: int
    workspace: Path | None
    coder_sid: str
    coder_entry: SessionEntry | None
    _flake_reruns: int
    _untracked_strikes: int
    _last_report: CoderReport | None
    _heartbeat: RoundHeartbeat
    _log: Callable[[str], None]
    _escalate: Callable[..., Awaitable[EscalationResponse | None]]
    _rewrite: Callable[..., Awaitable[None]]
    _relaunch: Callable[..., Awaitable[None]]
    _spread: Callable[[list[Surprise]], None]
    _persist_coder_usage: Callable[[], None]
    _make_coder_on_turn: Callable[..., object]

    def _log_driver_run_items(self) -> None:
        """Name the driver-run items still owed, once, at merge.

        A `driver_run` item passes the verification gate untouched (see
        `unmet_required_verification`), so without this line the only trace
        of an unrun check would be a `skipped` result buried in the report.
        The driver reads this line and runs them before `finish`. Since
        r20260924 the coder *may* attempt a sandbox-safe one, so an item its
        latest report marks `pass` is listed as passed, not as pending.
        """
        passed_ids: set[str] = set()
        if self._last_report is not None:
            passed_ids = {
                r.item_id for r in self._last_report.verification_results if r.status == "pass"
            }
        driver_items = [item.id for item in self.group.verification if item.driver_run]
        passed = [i for i in driver_items if i in passed_ids]
        pending = [i for i in driver_items if i not in passed_ids]
        if passed:
            self._log(
                f"group {self.gid}: {len(passed)} driver-run verification item(s) "
                f"passed by the coder — {', '.join(passed)}"
            )
        if pending:
            self._log(
                f"group {self.gid}: {len(pending)} driver-run verification item(s) "
                f"not run by the coder — {', '.join(pending)}"
            )

    async def _merge(self) -> bool:
        assert self.workspace is not None
        attempts_left = self.deps.execution.max_conflict_resolve_attempts
        while True:
            self.ctx.set_state(GroupState.MERGING)
            self._heartbeat.mark_phase("merging into integration")  # F4
            self._log(f"group {self.gid}: merge attempt")
            self._archive_coder_scratch()
            # Read before the merge, not after (plan U6): `merge_group` removes
            # the group's own worktree on success, so `self.workspace` no
            # longer exists by the time control returns here.
            paths = changed_paths(self.workspace, self.deps.base_ref_for(self.group))
            try:
                merge_sha = await asyncio.to_thread(
                    self.deps.merge_group, self.group, self.workspace
                )
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
                # An operator `retry` here means the same as `answer`: the text
                # guides the rewrite (a conflict is evidence about the diff).
                extra = [conflict]
                if response is not None:
                    extra.append(_operator_surprise(self.gid, response.answer))
                await self._rewrite(f"merge conflict: {exc}", extra=extra)
                return False
            except PreflightFailure as exc:
                # Not a git conflict — no coder resume is attempted in place
                # (plan U4 Decisions: only a concrete git/test failure ever
                # warrants an LLM call, and the in-place resume is specific to
                # resolving conflict markers).
                self._log(f"group {self.gid}: preflight failed ({exc})")
                if exc.kind == "untracked":
                    if await self._handle_untracked(exc):
                        continue  # the tree was cleaned in place: re-run the gate
                    return False  # relaunched on the same spec
                diagnosis, attributable = self._classify_preflight(exc)
                surprise = Surprise(kind="other", description=diagnosis, affected_groups=[self.gid])
                self._spread([surprise])
                if attributable:
                    if self._should_rerun_for_flake(exc):
                        self._flake_reruns += 1
                        self._log(
                            f"group {self.gid}: preflight: re-running gate once (suspected flake)"
                        )
                        continue
                    # A new, real regression — escalate, then rewrite (plan U3):
                    # the failure is evidence about the diff and a rewritten
                    # spec can act on it.
                    response = await self._escalate(
                        EscalationKind.PREFLIGHT_FAILED,
                        prompt=f"preflight failed for {self.gid}: {exc}",
                        surprises=[surprise],
                    )
                    if _is_retry(response):
                        if not response.answer.strip():
                            # The operator fixed the world by hand and the tree
                            # is unchanged: re-run the gate, no LLM call.
                            self._log_remerge("operator retry with no text")
                            continue
                        # With text: the operator changed something the coder
                        # must know about (a test, a fixture) — same spec,
                        # fresh coder, the text as its briefing.
                        await self._relaunch(response.answer, f"preflight failed: {exc}")
                        return False
                    extra = [surprise]
                    if response is not None:
                        extra.append(_operator_surprise(self.gid, response.answer))
                    await self._rewrite(f"preflight failed: {exc}", extra=extra)
                    return False
                # env/timeout, or every failing test was already red on the
                # launch branch: not evidence about the diff, so no rewrite is
                # spent on it (plan U3 Decisions) — escalate for visibility only,
                # the group fails fast either way.
                response = await self._escalate(
                    EscalationKind.PREFLIGHT_FAILED,
                    prompt=f"preflight failed for {self.gid} (not attributable to this diff): {exc}",
                    surprises=[surprise],
                )
                if _is_retry(response) and not response.answer.strip():
                    self._log_remerge("operator retry with no text")
                    continue
                if response is not None:
                    diagnosis += f"\n[operator] {response.answer}"
                raise GroupFailure(diagnosis) from exc
            self._log(f"group {self.gid}: merged into the integration branch")
            self._log_driver_run_items()
            self._register_code_artifact(commit=merge_sha, paths=paths)
            return True

    def _register_code_artifact(self, *, commit: str, paths: list[str]) -> None:
        """Register this group's Artifact Manifest entry on a successful
        merge (plan U6 Goal). A no-op when no Artifact Manifest is wired —
        every construction site that predates this unit."""
        store = self.deps.artifacts
        if store is None:
            return
        summary = self._last_report.summary if self._last_report is not None else self.group.summary
        store.register(
            ArtifactEntry(
                artifact_id=self.gid,
                group_id=self.gid,
                tasks=list(self.group.tasks),
                recipe=self.group.recipe,
                paths=paths,
                commit=commit,
                schema="CoderReport",
                summary=summary[:ARTIFACT_SUMMARY_MAX_CHARS],
                status="complete",
            )
        )

    def _log_remerge(self, reason: str) -> None:
        """The cheap ``preflight_failed`` resolution: refresh + preflight + merge
        again with no LLM call — the caller ``continue``s the merge loop."""
        self._log(f"group {self.gid}: re-running the merge gate ({reason})")

    def _should_rerun_for_flake(self, exc: PreflightFailure) -> bool:
        """One automatic re-run of the gate on an attributable regression when
        nobody is there to `retry` by hand (autonomous for this kind), so a
        flaky test does not cost a rewrite plus a coder generation. Capped at
        one per generation: a second identical failure is a real regression."""
        if exc.kind != "regression" or self._flake_reruns >= 1:
            return False
        policy, broker = self.deps.policy, self.deps.broker
        autonomous = broker is None or policy is None
        autonomous = autonomous or not policy.should_escalate(EscalationKind.PREFLIGHT_FAILED)
        return autonomous

    async def _handle_untracked(self, exc: PreflightFailure) -> bool:
        """The untracked ladder. Returns True when the gate should simply be
        re-run (the tree was cleaned in place, by the operator or by archiving),
        False when a fresh coder was relaunched on the same spec.

        First strike: attributable to the coder, but cheap — no rewrite, no
        speccer; a same-spec relaunch carrying an operator note that names the
        paths. Second consecutive strike: the leftovers are moved to the
        group's ``untracked/`` archive, a surprise records it for the report,
        and the merge proceeds.
        """
        assert self.workspace is not None
        self._untracked_strikes += 1
        paths = ", ".join(exc.paths)
        note = (
            f"untracked files left in the worktree: {paths} — commit them, move them "
            f"into {CODER_SCRATCH_DIRNAME}/, or delete them"
        )
        if self._untracked_strikes >= 2:
            archive = self.deps.store.paths.untracked_archive_dir(self.gid)
            _move_paths(self.workspace, exc.paths, archive)
            self._log(
                f"group {self.gid}: UNTRACKED FILES ARCHIVED after a second consecutive "
                f"untracked-only gate failure — moved to {archive}: {paths}; merging anyway"
            )
            self._spread(
                [
                    Surprise(
                        kind="informational",
                        description=(
                            f"group {self.gid} left untracked files twice; archived to "
                            f"{archive} and merged without them: {paths}"
                        ),
                        affected_groups=[self.gid],
                    )
                ]
            )
            self._log_remerge("untracked leftovers archived")
            return True
        response = await self._escalate(
            EscalationKind.PREFLIGHT_FAILED,
            prompt=f"preflight failed for {self.gid} (untracked files): {exc}",
        )
        if _is_retry(response) and not response.answer.strip():
            self._log_remerge("operator retry with no text")
            return True
        if response is not None and response.answer.strip():
            note = f"{note}\n[operator] {response.answer}"
        await self._relaunch(note, f"untracked files left in the worktree: {paths}")
        return False

    def _archive_coder_scratch(self) -> None:
        """Exclude and archive the coder's scratch directory before the gate
        runs, mirroring ``_archive_review_scratch`` — same cap, archived beside
        the group's artifacts. A no-op when it was never created."""
        assert self.workspace is not None
        scratch_dir = self.workspace / CODER_SCRATCH_DIRNAME
        if not scratch_dir.exists():
            return
        ensure_excluded(self.workspace, CODER_SCRATCH_DIRNAME)
        archive_review_scratch(
            scratch_dir,
            self.deps.store.paths.coder_scratch_archive_dir(self.gid),
            cap_bytes=self.deps.execution.review_scratch_cap_bytes,
            log=self._log,
        )

    def _classify_preflight(self, exc: PreflightFailure) -> tuple[str, bool]:
        """Route a preflight failure by cause (plan U3): ``env``/``timeout``
        never ran a real test, and a ``regression`` whose failing tests are all
        already red on the launch branch is not evidence about this diff
        either — neither is attributable, and the caller must not spend a
        rewrite on it. Only a genuinely new, run failure is attributable.

        Returns the diagnosis text handed to the next generation (or the
        operator) — carrying the check output's own "short test summary info"
        tail, not just a path to the log — and whether the failure is
        attributable.
        """
        summary_tail = ""
        if exc.output_path is not None and exc.output_path.is_file():
            summary_tail = _short_test_summary(exc.output_path.read_text())
        if exc.kind in ("env", "timeout", "untracked"):
            diagnosis = f"preflight failed ({exc.kind}): {exc.reason}"
            if summary_tail:
                diagnosis += f"\n{summary_tail}"
            return diagnosis, False
        # kind == "regression": tests actually ran and something actually
        # failed — the only question left is whether it is new. The gate
        # already answered that (it had to, to decide whether to raise), so
        # take its verdict rather than re-deriving one: re-derivation guessed a
        # single `preflight-junit.xml` beside the log and so was blind to the
        # UI steps' own reports. The fallback covers a `PreflightFailure`
        # raised from somewhere other than `run_preflight`.
        comparison = exc.comparison
        if comparison is None:
            junit_path = (
                exc.output_path.parent / "preflight-junit.xml"
                if exc.output_path is not None
                else None
            )
            failing = (
                failing_tests_from_junit(junit_path) if junit_path is not None else frozenset()
            )
            comparison = compare_to_baseline(self.deps.preflight_baseline, failing)
        if comparison.verdict == "pre_existing":
            diagnosis = (
                f"preflight failed (pre-existing): {exc.reason} — every failing test was "
                "already failing on the launch branch"
            )
            if summary_tail:
                diagnosis += f"\n{summary_tail}"
            return diagnosis, False
        # Name the tests this diff actually broke. The raw summary tail lists
        # every FAILED line, pre-existing ones included, and a rewrite spec
        # built from it sends the next coder to fix tests it did not break —
        # often outside its own declared files. The baseline is what tells
        # them apart, so spend it here too, not only on the routing decision.
        diagnosis = f"preflight failed (regression): {exc.reason}"
        if comparison.new_failures:
            named = "\n".join(f"  {test_id}" for test_id in sorted(comparison.new_failures))
            diagnosis += f"\nnew failures introduced by this diff:\n{named}"
        if summary_tail:
            diagnosis += f"\n{summary_tail}"
        if comparison.new_failures:
            diagnosis += (
                "\nAny FAILED line above that is not in the list of new failures was "
                "already red on the launch branch — it is not yours to fix."
            )
        return diagnosis, True

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
                on_turn=self._make_coder_on_turn(self.coder_entry),
            )
            report, _ = await asyncio.to_thread(
                nudge_until_report,
                self.deps.runner,
                result,
                CoderReport,
                cwd=self.workspace,
                verification_ids=[item.id for item in self.group.verification],
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


def _short_test_summary(output: str) -> str:
    """The "short test summary info" tail of a pytest run's captured output
    (plan U3/F18) — the FAILED/ERROR lines and the final result line, not the
    full scrollback. Empty when the marker never appears, e.g. an env/timeout
    failure that produced no pytest output at all."""
    marker = "short test summary info"
    idx = output.find(marker)
    if idx == -1:
        return ""
    line_end = output.find("\n", idx)
    if line_end == -1:
        return ""
    return output[line_end + 1 :].strip()


def _move_paths(worktree: Path, paths: list[str], dest_dir: Path) -> None:
    """Move ``paths`` (relative, as ``git status --porcelain`` names them — a
    directory carries a trailing slash) out of ``worktree`` into ``dest_dir``,
    keeping their relative layout."""
    for rel in paths:
        source = worktree / rel.rstrip("/")
        if not source.exists():
            continue
        target = dest_dir / rel.rstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(source), str(target))
