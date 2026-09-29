"""The ``optimize`` recipe's executor (plan U10): the code loop with a settle
step that scores every committed candidate against a KPI Contract and keeps
only the ones that clear it.

``OptimizeExecution`` subclasses ``_GroupExecution`` (the ``code`` recipe's
own host class) rather than duplicating the round loop: launch/re-entry,
the breaker, escalation, rewrite and the merge gate are all reused verbatim.
Only ``_settle_round`` (what happens to a `completed` report), ``_first_round``
(the champion/harness baseline, established once) and ``_next_round_prompt``
(the per-round resume) are overridden — the whole feature is "the settle
step", per the plan's Decisions.

Layout under ``<group_dir>/``::

    ledger.json          # Ledger — one Attempt per round, append-only
    eval/
        harness.sha256    # {"combined": ..., "paths": {path: sha256}},
                          # written once, at the very first evaluation
        attempt-0/        # the champion baseline evaluation
        attempt-<n>/      # round n's evaluation
        attempt-<n>-confirm/  # a `promising` candidate's confirmation run
"""

from __future__ import annotations

import fnmatch
import json
import os
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from string import Template
from types import SimpleNamespace

from orchestrator.config import OptimizeRecipeConfig
from orchestrator.execution.artifacts import ArtifactEntry
from orchestrator.execution.command_runner import CommandDied, CommandRunner, TimedOut
from orchestrator.execution.confinement import (
    build_policy,
    default_cache_root,
    landlock_preexec,
    system_write_paths,
    worker_cache_dirs,
    worker_cache_env,
)
from orchestrator.execution.denial import classify_denial, denial_remedy
from orchestrator.execution.evaluate_executor import _combined_hash, _harness_snapshot
from orchestrator.execution.kpi import (
    Attempt,
    Ledger,
    decide,
    noise_floor,
    render_ledger_table,
    signed_delta,
)
from orchestrator.execution.manifest import artifact_name, atomic_write_text, log_event
from orchestrator.execution.prompting import (
    CODER_SCRATCH_DIRNAME,
    REVIEW_SCRATCH_DIRNAME,
    render_re_review_prompt,
    render_reentry_prompt,
    render_reviewer_prompt,
)
from orchestrator.execution.review import _GroupExecution
from orchestrator.execution.scheduler import Executor, GroupContext, GroupFailure, GroupState
from orchestrator.execution.sessions import (
    _scrub_virtualenv,
    nudge_until_report,
    session_display_name,
)
from orchestrator.execution.worktrees import _git, _git_ok, data_layer_write_paths
from orchestrator.model import EscalationKind, PermissionDenied, ReviewerVerdict, SessionRole
from orchestrator.prompts import load_template
from orchestrator.recipes.optimize import OptimizeArgs


class _EvalCrash(Exception):
    """One evaluation attempt failed to produce a score: a declared command
    exited nonzero, timed out, or the measurements file lacked the KPI/guard
    keys. Caught by the settle step and recorded as the round's ``crash``
    outcome — it counts against rounds, never against the evaluation cap."""


def make_executor(deps) -> Executor:
    async def executor(ctx: GroupContext) -> GroupState:
        return await OptimizeExecution(deps, ctx).run()

    return executor


class OptimizeExecution(_GroupExecution):
    """One optimize group's loop: every `completed` coder report is a
    candidate, scored against the KPI Contract and either kept as the new
    Champion or reverted."""

    def __init__(self, deps, ctx: GroupContext):
        super().__init__(deps, ctx)
        self.args = OptimizeArgs.model_validate(self.group.recipe_args or {})
        self._commands = CommandRunner(deps.store.paths, self.gid, self._heartbeat)
        self._ledger_path = deps.store.paths.group_dir(self.gid) / "ledger.json"
        self._ledger = Ledger.load(self._ledger_path)
        self._launch_commit: str | None = None
        self._champion: str | None = None
        self._champion_kpi_value: float | None = None
        self._champion_guard_values: dict[str, float] = {}
        self._harness_hash = ""
        self._evaluations_used = 0
        self._consecutive_non_keep = 0
        self._consecutive_reverts = 0
        self._pending_round_notes = ""

    # ------------------------------------------------------------ overrides

    async def _first_round(self):
        assert self.workspace is not None
        if self._launch_commit is None:
            self._launch_commit = _git_ok(self.workspace, "rev-parse", "HEAD").strip()
            self._champion = self._launch_commit
            await self._establish_baseline()
        return await super()._first_round()

    def _breaker_reason(self, rounds: int) -> str | None:
        # Rounds are bounded by `evaluations` (checked in `_after_candidate`),
        # not `max_rounds_per_generation` — only the context-token ceiling
        # still retires a generation here.
        context = self.deps.runner.usage_of(self.coder_sid).last_context_tokens
        limit = self.deps.breaker.context_token_limit
        if context > limit:
            return f"context tokens {context} exceeded limit {limit}"
        return None

    async def _settle_round(self, report, report_path: Path, rounds: int, result):
        if report.status == "permission_denied":
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
            return False, None, None
        return await self._settle_candidate(report, report_path, rounds)

    async def _next_round_prompt(self, verdict, verdict_path, rounds: int):
        self.ctx.set_state(GroupState.RUNNING)
        self._log(f"{self._round_tag(rounds + 1)}: started")
        self._heartbeat.mark_round(self.generation, rounds + 1)
        revision_on_turn = self._make_coder_on_turn(self.coder_entry)
        prompt = self._pending_round_notes
        return await self._worker_call(
            partial(
                self.deps.runner.resume,
                session_id=self.coder_sid,
                prompt=prompt,
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

    # ------------------------------------------------------------- rounds

    async def _settle_candidate(self, report, report_path: Path, rounds: int):
        assert self.workspace is not None
        candidate_commit = _git_ok(self.workspace, "rev-parse", "HEAD").strip()
        kpi = self.args.kpi

        violation = self._mutable_violation()
        if violation is not None:
            why = f"candidate touched a path outside the mutable region: {violation}"
            self._log(f"{self._round_tag(rounds)}: candidate discarded ({why})")
            self._record_attempt(
                rounds,
                candidate_commit,
                None,
                {},
                None,
                noise_floor(self._ledger.deltas()),
                "discard",
                why,
            )
            self._revert_to_champion()
            return await self._after_candidate(rounds, "discard", report, why)

        attempt_dir = self._eval_dir() / f"attempt-{rounds}"
        try:
            measurements = await self._run_evaluation(attempt_dir)
        except _EvalCrash as exc:
            why = f"evaluation crashed: {exc}"
            self._log(f"{self._round_tag(rounds)}: candidate crashed ({why})")
            self._record_attempt(
                rounds,
                candidate_commit,
                None,
                {},
                None,
                noise_floor(self._ledger.deltas()),
                "crash",
                why,
            )
            self._revert_to_champion()
            return await self._after_candidate(rounds, "crash", report, why)

        assert self._champion_kpi_value is not None
        value = measurements[kpi.key]
        guard_values = {g.key: measurements[g.key] for g in kpi.guards}
        delta = signed_delta(value, self._champion_kpi_value, kpi.direction)
        guard_deltas = [
            signed_delta(
                guard_values[g.key],
                self._champion_guard_values.get(g.key, guard_values[g.key]),
                g.direction,
            )
            for g in kpi.guards
        ]
        floor = noise_floor(self._ledger.deltas())
        self._evaluations_used += 1
        outcome = decide(delta, guard_deltas, floor, kpi)
        why = f"delta={delta:+g} floor={floor:g}"

        if outcome == "promising":
            verdict = await self._cheat_review(report_path, rounds)
            if verdict.status != "approved":
                outcome = "discard"
                why = (
                    f"cheat reviewer {verdict.status}: "
                    f"{verdict.notes or '; '.join(verdict.required_changes) or '(no notes)'}"
                )
            else:
                confirm_dir = self._eval_dir() / f"attempt-{rounds}-confirm"
                try:
                    confirm_measurements = await self._run_evaluation(confirm_dir)
                except _EvalCrash as exc:
                    outcome = "crash"
                    why = f"confirmation crashed: {exc}"
                else:
                    self._evaluations_used += 1
                    confirm_value = confirm_measurements[kpi.key]
                    confirm_guard_values = {g.key: confirm_measurements[g.key] for g in kpi.guards}
                    confirm_delta = signed_delta(
                        confirm_value, self._champion_kpi_value, kpi.direction
                    )
                    confirm_guard_deltas = [
                        signed_delta(
                            confirm_guard_values[g.key],
                            self._champion_guard_values.get(g.key, confirm_guard_values[g.key]),
                            g.direction,
                        )
                        for g in kpi.guards
                    ]
                    confirm_outcome = decide(confirm_delta, confirm_guard_deltas, floor, kpi)
                    if confirm_outcome in ("keep", "promising"):
                        outcome = "keep"
                        why = f"confirmed: delta={confirm_delta:+g} (first delta={delta:+g})"
                    else:
                        outcome = "inconclusive"
                        why = f"confirmation did not clear: delta={confirm_delta:+g}"

        self._record_attempt(
            rounds, candidate_commit, value, guard_values, delta, floor, outcome, why
        )
        if outcome == "keep":
            self._champion = candidate_commit
            self._champion_kpi_value = value
            self._champion_guard_values = guard_values
            self._last_report = report
        else:
            self._revert_to_champion()
        self._log(f"{self._round_tag(rounds)}: candidate {outcome} ({why})")
        return await self._after_candidate(rounds, outcome, report, why)

    async def _after_candidate(self, rounds: int, outcome: str, report, why: str):
        kpi = self.args.kpi
        self._consecutive_non_keep = 0 if outcome == "keep" else self._consecutive_non_keep + 1
        # A revert streak counts only discard/crash — a run of candidates
        # actively ruled out — not `inconclusive`, which is a weaker, noisier
        # signal and resets the streak without being a "keep".
        self._consecutive_reverts = (
            self._consecutive_reverts + 1 if outcome in ("discard", "crash") else 0
        )

        good_enough_cleared = False
        if kpi.good_enough is not None and self._champion_kpi_value is not None:
            good_enough_cleared = (
                self._champion_kpi_value >= kpi.good_enough
                if kpi.direction == "max"
                else self._champion_kpi_value <= kpi.good_enough
            )
        if self._evaluations_used >= self.args.evaluations or good_enough_cleared:
            return await self._finish_loop()

        cfg = self._optimize_config()
        patience_exhausted = outcome != "keep" and self._consecutive_non_keep > cfg.patience
        reverts_exhausted = self._consecutive_reverts > cfg.consecutive_reverts
        if patience_exhausted or reverts_exhausted:
            if reverts_exhausted:
                cap_label = f"consecutive reverts ({cfg.consecutive_reverts})"
            else:
                cap_label = f"patience ({cfg.patience})"
            response = await self._escalate(
                EscalationKind.CAPS_EXHAUSTED,
                prompt=(
                    f"group {self.gid}: optimize {cap_label} exhausted with no "
                    f"improvement\n\n{render_ledger_table(self._ledger)}"
                ),
            )
            if response is None:
                return await self._finish_loop()
            self._consecutive_non_keep = 0
            self._consecutive_reverts = 0

        reason = self._breaker_reason(rounds)
        if reason:
            await self._retire(reason)
            verdict = ReviewerVerdict(
                status="changes_required",
                required_changes=[render_ledger_table(self._ledger)],
                notes=f"generation retired: {reason}",
            )
            self._prepare_handoff(report, verdict)
            return False, None, None

        self._pending_round_notes = self._render_round_prompt(rounds, outcome, why)
        return None, None, None

    async def _finish_loop(self):
        ruled_out = sum(1 for a in self._ledger.attempts if a.outcome != "keep")
        if self._champion != self._launch_commit:
            merged = await self._merge()
            if merged:
                self._log(
                    f"group {self.gid}: optimize loop merged champion "
                    f"{(self._champion or '')[:8]} ({ruled_out} candidate(s) ruled out)"
                )
            else:
                # r20260927 g3-5 logged "merged champion" right after a failed
                # gate had relaunched the group — the line must mean it landed.
                self._log(
                    f"group {self.gid}: optimize loop champion "
                    f"{(self._champion or '')[:8]} did not merge this attempt"
                )
            return merged, None, None
        self._register_ledger_artifact(ruled_out)
        self._log(f"group {self.gid}: no improvement found — {ruled_out} candidate(s) ruled out")
        return True, None, None

    def _optimize_config(self) -> OptimizeRecipeConfig:
        recipes = self.deps.recipes_config
        return recipes.optimize if recipes is not None else OptimizeRecipeConfig()

    # ------------------------------------------------------------- champion

    def _eval_dir(self) -> Path:
        return self.deps.store.paths.group_dir(self.gid) / "eval"

    async def _establish_baseline(self) -> None:
        measurements = await self._run_evaluation(self._eval_dir() / "attempt-0")
        self._champion_kpi_value = measurements[self.args.kpi.key]
        self._champion_guard_values = {g.key: measurements[g.key] for g in self.args.kpi.guards}

    def _mutable_violation(self) -> str | None:
        assert self.workspace is not None and self._champion is not None
        diff_paths = _git_ok(
            self.workspace, "diff", "--name-only", f"{self._champion}..HEAD"
        ).splitlines()
        porcelain_paths = []
        for line in _git_ok(self.workspace, "status", "--porcelain").splitlines():
            if not line.strip():
                continue
            path = line[3:].strip()
            if "->" in path:
                path = path.split(" -> ", 1)[1].strip()
            porcelain_paths.append(path)
        harness_globs = self.args.kpi.harness_paths
        allowed = self.group.files
        seen: set[str] = set()
        for path in (*diff_paths, *porcelain_paths):
            path = path.strip()
            if (
                not path
                or path in seen
                or path.startswith(CODER_SCRATCH_DIRNAME)
                or path == self.args.measurements
            ):
                continue
            seen.add(path)
            if any(path == glob or fnmatch.fnmatch(path, glob) for glob in harness_globs):
                return path
            if allowed and not any(path == glob or fnmatch.fnmatch(path, glob) for glob in allowed):
                return path
        return None

    def _revert_to_champion(self) -> None:
        assert self.workspace is not None and self._champion is not None
        _git(self.workspace, "reset", "--hard", self._champion)
        _git(self.workspace, "clean", "-fd", "-e", CODER_SCRATCH_DIRNAME)

    def _record_attempt(
        self,
        round_no: int,
        candidate_commit: str,
        kpi_value: float | None,
        guard_values: dict[str, float],
        delta: float | None,
        floor: float,
        outcome: str,
        why: str,
    ) -> None:
        attempt = Attempt(
            round_no=round_no,
            candidate_commit=candidate_commit,
            kpi_value=kpi_value,
            guard_values=guard_values,
            delta=delta,
            noise_floor=floor,
            outcome=outcome,
            harness_hash=self._harness_hash,
            why=why[:2000],
            at=datetime.now(UTC).isoformat(),
        )
        self._ledger.append(self._ledger_path, attempt)

    def _register_ledger_artifact(self, ruled_out: int) -> None:
        store = self.deps.artifacts
        if store is None:
            return
        store.register(
            ArtifactEntry(
                artifact_id=self.gid,
                group_id=self.gid,
                tasks=list(self.group.tasks),
                recipe=self.group.recipe,
                paths=["ledger.json"],
                commit=self._champion,
                schema="Ledger",
                summary=f"no improvement found — {ruled_out} candidate(s) ruled out",
                status="complete",
            )
        )

    def _render_round_prompt(self, round_no: int, outcome: str, detail: str) -> str:
        return Template(load_template("optimize_round")).substitute(
            round_no=str(round_no),
            outcome=outcome,
            detail=detail,
            ledger_table=render_ledger_table(self._ledger),
        )

    # ------------------------------------------------------------- evaluation

    def _worker_cache_root(self) -> Path:
        runner = getattr(self.deps, "runner", None)
        root = getattr(runner, "cache_root", None)
        return Path(root) if root is not None else default_cache_root()

    def _confinement_preexec(self, cwd: Path, attempt_dir: Path):
        repo_root = self.deps.store.paths.repo_root
        extra_write = [attempt_dir]
        if self.deps.workspace_config is not None:
            extra_write.extend(data_layer_write_paths(repo_root, self.deps.workspace_config))
        for entry in self.args.allow_write:
            extra_write.append(Path(entry).expanduser())
        runner = getattr(self.deps, "runner", None)
        extra_write_paths = [Path(p) for p in (getattr(runner, "extra_write_paths", None) or [])]
        cache_dirs = worker_cache_dirs(self._worker_cache_root(), create=True)
        policy = build_policy(
            worktree=self.workspace,
            claude_home=Path.home() / ".claude",
            system_paths=[*system_write_paths(), *extra_write_paths],
            cache_dirs=cache_dirs,
            extra_write=extra_write,
        )
        preexec_fn, _result = landlock_preexec(policy)
        return preexec_fn

    def _child_env(self) -> dict[str, str]:
        base = dict(os.environ)
        return _scrub_virtualenv({**base, **worker_cache_env(self._worker_cache_root(), base=base)})

    async def _run_smoke(self, attempt_dir: Path) -> None:
        smoke = self.args.kpi.smoke
        if not smoke:
            return
        command = SimpleNamespace(cmd=smoke, wall_clock_min=5.0)
        preexec_fn = self._confinement_preexec(self.workspace, attempt_dir)
        try:
            result = await self._commands.run(
                attempt_dir,
                0,
                command,
                self.workspace,
                preexec_fn=preexec_fn,
                env=self._child_env(),
                total=len(self.args.commands) + 1,
            )
        except (TimedOut, CommandDied) as exc:
            raise _EvalCrash(f"smoke failed: {exc}") from exc
        if result.exit_status != 0:
            raise _EvalCrash(f"smoke failed (exit {result.exit_status})")

    async def _run_evaluation(self, attempt_dir: Path) -> dict[str, float]:
        assert self.workspace is not None
        attempt_dir.mkdir(parents=True, exist_ok=True)
        kpi = self.args.kpi
        snapshot = _harness_snapshot(self.workspace, kpi.harness_paths)
        combined = _combined_hash(snapshot)
        baseline_path = self._eval_dir() / "harness.sha256"
        if baseline_path.is_file():
            try:
                baseline = json.loads(baseline_path.read_text())
            except (OSError, ValueError):
                baseline = {}
            baseline_paths = baseline.get("paths", {})
            first_diff = next(
                (p for p in kpi.harness_paths if baseline_paths.get(p) != snapshot.get(p)), None
            )
            if first_diff is not None:
                message = f"group {self.gid}: harness path changed since the first evaluation: {first_diff}"
                log_event(self.deps.store.paths, f"group {self.gid}: optimize failure — {message}")
                raise GroupFailure(message)
        else:
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                baseline_path,
                json.dumps({"combined": combined, "paths": snapshot}, indent=2) + "\n",
            )
        self._harness_hash = combined
        await self._run_smoke(attempt_dir)

        for idx, command in enumerate(self.args.commands):
            n = idx + 1
            cwd = self.workspace / command.cwd if command.cwd else self.workspace
            preexec_fn = self._confinement_preexec(cwd, attempt_dir)
            try:
                result = await self._commands.run(
                    attempt_dir,
                    n,
                    command,
                    cwd,
                    preexec_fn=preexec_fn,
                    env=self._child_env(),
                    total=len(self.args.commands),
                )
            except (TimedOut, CommandDied) as exc:
                raise _EvalCrash(f"command {n} ({command.cmd!r}) {exc}") from exc
            self._commands.write_result(attempt_dir, n, result)
            if result.exit_status != 0:
                raise _EvalCrash(f"command {n} ({command.cmd!r}) exited {result.exit_status}")

        raw = self._read_measurements_raw()
        self._archive_measurements(attempt_dir)
        value = raw.get(kpi.key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _EvalCrash(f"measurements missing KPI key {kpi.key!r}")
        values = {kpi.key: float(value)}
        for guard in kpi.guards:
            guard_value = raw.get(guard.key)
            if not isinstance(guard_value, (int, float)) or isinstance(guard_value, bool):
                raise _EvalCrash(f"measurements missing guard key {guard.key!r}")
            values[guard.key] = float(guard_value)
        return values

    def _archive_measurements(self, attempt_dir: Path) -> None:
        """Move an untracked measurements file out of the worktree into the
        attempt's eval dir once read. Left in place it is an untracked file at
        merge, which the gate refuses — r20260927 g3-5: every live optimize
        run relaunched a second generation and merged only after the untracked
        ladder archived `measurements.json`. A tracked measurements file is
        left alone (the harness owns it)."""
        assert self.workspace is not None
        path = self.workspace / self.args.measurements
        if not path.is_file():
            return
        if _git_ok(self.workspace, "ls-files", "--", self.args.measurements).strip():
            return
        attempt_dir.mkdir(parents=True, exist_ok=True)
        path.replace(attempt_dir / Path(self.args.measurements).name)

    def _read_measurements_raw(self) -> dict:
        assert self.workspace is not None
        path = self.workspace / self.args.measurements
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    # --------------------------------------------------------- cheat review

    async def _cheat_review(self, report_path: Path, rounds: int) -> ReviewerVerdict:
        assert self.workspace is not None
        recipe = self._worker_recipe()

        def _recover(sid: str):
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
                        template=recipe.reviewer_prompt,
                    ),
                    name=session_display_name(
                        self.deps.run_id, self.gid, "reviewer", self.generation
                    ),
                    cwd=self.workspace,
                ),
                recover=_recover,
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
                recover=_recover,
            )

        def _nudge(round_result):
            return nudge_until_report(
                self.deps.runner, round_result, ReviewerVerdict, cwd=self.workspace
            )

        verdict, _result = await self._worker_call(
            partial(_nudge, result), recover=lambda sid: _nudge(_recover(sid))
        )
        self._persist_reviewer_usage(self.reviewer_sid)
        self.deps.store.save_group_artifact(
            self.gid, artifact_name("verdict", self.generation, rounds), verdict
        )
        self._log(f"{self._round_tag(rounds)}: cheat reviewer verdict {verdict.status}")
        return verdict
