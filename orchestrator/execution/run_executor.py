"""The ``run`` recipe's executor (plan U8): declared commands as confined,
re-adoptable Run Children, no reviewer, no coder session.

Layout under ``<group_dir>/run/``::

    attempt-<k>/
        <n>.state.json   # RunChildState, written at launch
        <n>.out, <n>.err # command stdout/stderr
        <n>.exit         # exit code text, written atomically by the wrapper
        <n>.result.json  # CommandResult, written once the command settles
        settled.json     # {"outcome": "failed"|"completed", ...} once this
                          # attempt reaches a terminal outcome
    retry-note.txt        # optional operator note, consumed by the next
                          # attempt after a `retry` (see _start_attempt)

A crash re-entry (the orchestrator restarted mid-command) continues the same
attempt and re-adopts whatever pid its ``<n>.state.json`` names. An operator
`retry` after a FAILED group's attempt was marked ``settled`` starts a new
attempt from the first command that did not exit 0 in the previous one — or
from command 1 when ``retry-note.txt`` contains ``--from-start``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from string import Template

from orchestrator.execution.artifacts import ARTIFACT_SUMMARY_MAX_CHARS, ArtifactEntry
from orchestrator.execution.confinement import (
    build_policy,
    default_cache_root,
    landlock_preexec,
    system_write_paths,
    worker_cache_dirs,
    worker_cache_env,
)
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.manifest import (
    artifact_name,
    atomic_write_text,
    log_event,
    record_session,
)
from orchestrator.execution.merge import MergeConflict, commits_ahead
from orchestrator.execution.preflight import PreflightFailure
from orchestrator.execution.run_child import (
    RunChildState,
    boot_id,
    is_adoptable,
    kill_process_group,
    launch,
    proc_cmdline_head,
    proc_starttime,
    process_alive,
)
from orchestrator.execution.sessions import _scrub_virtualenv, session_display_name
from orchestrator.execution.scheduler import (
    Executor,
    GroupContext,
    GroupFailure,
    GroupState,
    RunAbort,
)
from orchestrator.execution.worktrees import (
    _git,
    _git_ok,
    data_layer_write_paths,
    integration_branch,
)
from orchestrator.model import (
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    HumanAction,
    SessionEntry,
    SessionRole,
    Surprise,
    VerificationResult,
)
from orchestrator.prompts import load_template
from orchestrator.recipes.run import (
    CommandResult,
    RunArgs,
    RunRecord,
    RunVerificationReport,
    run_command_for_item,
)

#: A consumed surprise's description is cut to this many characters in the
#: artifact summary (the full text stays in run.log).
SURPRISE_SUMMARY_MAX_CHARS = 120

DEFAULT_POLL_INTERVAL_S = 1.0


class TimedOut(Exception):
    """A command exceeded its declared ``wall_clock_min`` cap."""


def make_executor(deps) -> Executor:
    async def executor(ctx: GroupContext) -> GroupState:
        return await _RunExecution(deps, ctx).run()

    return executor


class _RunExecution:
    def __init__(self, deps, ctx: GroupContext):
        self.deps = deps
        self.ctx = ctx
        self.group = ctx.group
        self.gid = ctx.group.id
        self.paths = deps.store.paths
        self.args = RunArgs.model_validate(self.group.recipe_args or {})
        self.workspace: Path | None = None
        self._heartbeat = RoundHeartbeat(
            self.paths, self.gid, log=lambda text: log_event(self.paths, text)
        )
        # The manifest entry for this attempt's runner (plan: a run group is a
        # real ``SessionRole.RUNNER`` session, so `status`, the report and the
        # observatory see it with no reader change) and the surprises consumed
        # at start, folded into the artifact summary.
        self._runner_entry: SessionEntry | None = None
        self._surprises: list[Surprise] = []

    # ------------------------------------------------------------- entry

    async def run(self) -> GroupState:
        self.ctx.set_state(GroupState.RUNNING)
        self._heartbeat.start()
        try:
            return await self._run()
        finally:
            self._heartbeat.stop()
            # Every exit — COMPLETED, GroupFailure, RunAbort, cancellation —
            # closes the runner session so Elapsed reads real.
            self._close_runner_session()

    async def _run(self) -> GroupState:
        run_dir = self._run_dir()
        attempt_no, start_idx, results = self._start_attempt(run_dir)
        self._runner_entry = self._record_runner_session(attempt_no)
        self._consume_surprises()
        self._heartbeat.mark_phase("worktree")
        self.workspace = await asyncio.to_thread(self.deps.workspace_for, self.group)
        log_event(self.paths, f"group {self.gid}: run recipe worktree ready at {self.workspace}")
        attempt_dir = run_dir / f"attempt-{attempt_no}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        for idx in range(start_idx, len(self.args.commands)):
            n = idx + 1
            command = self.args.commands[idx]
            cwd = self.workspace / command.cwd if command.cwd else self.workspace
            try:
                result = await self._run_command(attempt_dir, n, command, cwd)
            except (TimedOut, _CommandDied) as exc:
                results.append(
                    CommandResult(
                        cmd=command.cmd,
                        exit_status=124 if isinstance(exc, TimedOut) else 1,
                        duration_s=command.wall_clock_min * 60.0,
                    )
                )
                await self._triage_and_fail(
                    f"group {self.gid}: command {n}/{len(self.args.commands)} "
                    f"({command.cmd!r}) {exc}"
                )
            results.append(result)
            self._write_result(attempt_dir, n, result)
            if result.exit_status != 0:
                await self._triage_and_fail(
                    f"group {self.gid}: command {n}/{len(self.args.commands)} "
                    f"({command.cmd!r}) exited {result.exit_status}"
                )

        missing_outputs = [p for p in self.args.outputs if not (self.workspace / p).exists()]
        if missing_outputs:
            await self._triage_and_fail(
                f"group {self.gid}: declared outputs missing: {', '.join(missing_outputs)}"
            )

        measurements, measurements_missing = self._read_measurements()

        offenders = self._porcelain_offenders()
        if offenders:
            await self._triage_and_fail(
                f"group {self.gid}: git status --porcelain outside commit_paths: "
                f"{', '.join(offenders)}"
            )

        # Hashed before the merge: merging tears the worktree (and its data-dir
        # links) down, so an output read afterwards is never a file — on
        # r20260925-101742 g5 registered `sha256: {}` beside full measurements.
        sha256 = self._output_sha256s()
        self._heartbeat.mark_phase("merging into integration")
        commit = await self._commit_and_merge()

        summary = self._build_summary(results, measurements_missing)
        record = RunRecord(
            commands=results,
            outputs=list(self.args.outputs),
            measurements=measurements,
            summary=summary,
        )
        self._write_verification_report(results, attempt_no, summary)
        self._write_settled(attempt_dir, "completed", summary)
        self._register_artifact(record, commit, measurements_missing, sha256)
        log_event(self.paths, f"group {self.gid}: run recipe completed")
        return GroupState.COMPLETED

    # ---------------------------------------------------- runner session

    def _runner_session_id(self, attempt_no: int) -> str:
        return f"{self.gid}-run-a{attempt_no}"

    def _record_runner_session(self, attempt_no: int) -> SessionEntry:
        """The manifest entry for this attempt. A crash re-entry continues the
        same attempt and reuses its entry (its ``started_at`` stands); a new
        attempt gets a new one."""
        session_id = self._runner_session_id(attempt_no)
        group_entry = self.deps.manifest.groups.get(self.gid)
        if group_entry is not None:
            for entry in group_entry.sessions:
                if entry.session_id == session_id:
                    return entry
        generation = self.ctx.generation
        entry = SessionEntry(
            session_id=session_id,
            role=SessionRole.RUNNER,
            generation=generation,
            name=session_display_name(self.deps.run_id, self.gid, "runner", generation),
            started_at=datetime.now(UTC).isoformat(),
        )
        record_session(
            self.deps.manifest,
            group_id=self.gid,
            group_name=self.group.name,
            summary=self.group.summary,
            entry=entry,
        )
        self.deps.store.save(self.deps.manifest)
        return entry

    def _close_runner_session(self) -> None:
        entry = self._runner_entry
        if entry is None or entry.ended_at is not None:
            return
        entry.ended_at = datetime.now(UTC).isoformat()
        self.deps.store.save(self.deps.manifest)

    def _consume_surprises(self) -> None:
        """A run group has no coder to read a surprise: take the pending ones
        off the board at start, log each, and note them in the artifact
        summary (see ``_build_summary``) so a downstream group's prompt
        carries them. Until this one aimed at a run group died in the residue
        as "never delivered" (r20260925-101742)."""
        self._surprises = list(self.deps.board.consume(self.gid))
        for surprise in self._surprises:
            desc = " ".join(surprise.description.split())
            log_event(
                self.paths,
                f"group {self.gid}: consumed surprise [{surprise.kind}] "
                f"(run recipe, no coder; noted in the artifact summary): {desc}",
            )

    # --------------------------------------------------------- attempts

    def _run_dir(self) -> Path:
        return self.paths.group_dir(self.gid) / "run"

    def _existing_attempts(self, run_dir: Path) -> list[int]:
        if not run_dir.is_dir():
            return []
        found = []
        for child in run_dir.iterdir():
            if child.is_dir() and child.name.startswith("attempt-"):
                try:
                    found.append(int(child.name.removeprefix("attempt-")))
                except ValueError:
                    continue
        return sorted(found)

    def _start_attempt(self, run_dir: Path) -> tuple[int, int, list[CommandResult]]:
        """Returns ``(attempt_no, start_idx, results)`` — ``results`` is
        pre-seeded with the commands a retry is carrying forward unrun."""
        attempts = self._existing_attempts(run_dir)
        if not attempts:
            return 1, 0, []
        latest = attempts[-1]
        latest_dir = run_dir / f"attempt-{latest}"
        settled_path = latest_dir / "settled.json"
        if not settled_path.is_file():
            # Crash re-entry: continue the same attempt. Resume at the first
            # command with no recorded result yet.
            results: list[CommandResult] = []
            start_idx = 0
            for idx in range(len(self.args.commands)):
                result_path = latest_dir / f"{idx + 1}.result.json"
                if result_path.is_file():
                    results.append(CommandResult.model_validate_json(result_path.read_text()))
                    start_idx = idx + 1
                else:
                    break
            return latest, start_idx, results
        # A settled attempt: this entry is an operator `retry`. Start a new
        # attempt, carrying forward every command that already exited 0.
        note_path = run_dir / "retry-note.txt"
        note = ""
        if note_path.is_file():
            note = note_path.read_text()
            note_path.unlink(missing_ok=True)
        from_start = "--from-start" in note
        results = []
        start_idx = 0
        if not from_start:
            for idx in range(len(self.args.commands)):
                result_path = latest_dir / f"{idx + 1}.result.json"
                if not result_path.is_file():
                    break
                result = CommandResult.model_validate_json(result_path.read_text())
                if result.exit_status != 0:
                    break
                results.append(result)
                start_idx = idx + 1
        return latest + 1, start_idx, results

    def _write_result(self, attempt_dir: Path, n: int, result: CommandResult) -> None:
        atomic_write_text(attempt_dir / f"{n}.result.json", result.model_dump_json(indent=2) + "\n")

    def _write_settled(self, attempt_dir: Path, outcome: str, detail: str) -> None:
        atomic_write_text(
            attempt_dir / "settled.json",
            json.dumps({"outcome": outcome, "detail": detail}, indent=2) + "\n",
        )

    # --------------------------------------------------------- commands

    def _worker_cache_root(self) -> Path:
        """The cache root the run's coder sessions use (``SessionRunner.cache_root``),
        so a Run Child finds the same warmed caches; ``default_cache_root()`` when
        the deps carry no runner (tests)."""
        runner = getattr(self.deps, "runner", None)
        root = getattr(runner, "cache_root", None)
        return Path(root) if root is not None else default_cache_root()

    def _confinement_preexec(self, cwd: Path, attempt_dir: Path):
        """The Run Child's Landlock profile: the worker profile a coder session
        gets (worktree, project slug, the cache dirs every toolchain is pointed
        at, ``[session] extra_write_paths``) plus the run's shared ``data_dirs``
        and the unit's ``allow_write`` paths — and ``attempt_dir``, where the
        launch wrapper writes the command's exit file.

        Both the cache rule and the ``attempt_dir`` rule were missing on
        r20260925-101742 (g5): ``uv run`` died within a second on
        ``failed to open ~/.cache/uv/…: Permission denied``, then the wrapper's
        ``echo $rc > <n>.exit.tmp`` was denied too, so no exit file ever
        appeared and the command "ran" to its 20-minute cap. The unit tests
        never saw it because a ``tmp_path`` run dir sits under ``/tmp``, which
        the system rules already allow."""
        repo_root = self.paths.repo_root
        extra_write = [attempt_dir]
        if self.deps.workspace_config is not None:
            extra_write.extend(data_layer_write_paths(repo_root, self.deps.workspace_config))
        for entry in self.args.allow_write:
            extra_write.append(Path(entry).expanduser())
        runner = getattr(self.deps, "runner", None)
        extra_write_paths = [Path(p) for p in (getattr(runner, "extra_write_paths", None) or [])]
        # create=True on every spawn, as the session runner does: Landlock
        # rules address existing paths, so a cache dir removed mid-run would
        # drop out of the ruleset silently.
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
        """The Run Child's environment: the orchestrator's own, with every
        toolchain cache pointed under the worker cache root (the overlay a coder
        session gets — the allowlist above only admits *those* dirs, so a child
        left on ``~/.cache/uv`` fails its first ``uv run``) and the orchestrator's
        venv scrubbed off ``PATH`` so the worktree's own wins."""
        base = dict(os.environ)
        return _scrub_virtualenv({**base, **worker_cache_env(self._worker_cache_root(), base=base)})

    async def _run_command(self, attempt_dir: Path, n: int, command, cwd: Path) -> CommandResult:
        cwd.mkdir(parents=True, exist_ok=True)
        state_path = attempt_dir / f"{n}.state.json"
        exit_path = attempt_dir / f"{n}.exit"
        out_path = attempt_dir / f"{n}.out"
        err_path = attempt_dir / f"{n}.err"
        cap_seconds = command.wall_clock_min * 60.0

        state: RunChildState | None = None
        proc: subprocess.Popen | None = None
        if state_path.is_file() and not exit_path.is_file():
            candidate = RunChildState.model_validate_json(state_path.read_text())
            if is_adoptable(candidate, current_boot_id=boot_id()):
                state = candidate
                log_event(
                    self.paths,
                    f"group {self.gid}: re-adopted run child pid {state.pid} for command {n}",
                )
            elif not process_alive(candidate.pid) and not exit_path.is_file():
                raise _CommandDied("died with the orchestrator (no exit file, pid gone)")

        if state is None:
            preexec_fn = self._confinement_preexec(cwd, attempt_dir)
            proc = await asyncio.to_thread(
                launch,
                command.cmd,
                cwd=cwd,
                exit_path=exit_path,
                out_path=out_path,
                err_path=err_path,
                preexec_fn=preexec_fn,
                env=self._child_env(),
            )
            state = RunChildState(
                n=n,
                pid=proc.pid,
                starttime=proc_starttime(proc.pid),
                cmdline_head=proc_cmdline_head(proc.pid),
                started_at=datetime.now(UTC).isoformat(),
                started_monotonic=time.monotonic(),
                boot_id=boot_id(),
            )
            atomic_write_text(state_path, state.model_dump_json(indent=2) + "\n")

        total = len(self.args.commands)
        self._heartbeat_phase(n, total, state, cap_seconds)
        try:
            exit_code = await self._await_exit(
                state.pid,
                exit_path,
                state.started_monotonic,
                cap_seconds,
                relabel=lambda elapsed: self._heartbeat.relabel_phase(
                    self._phase_label(n, total, elapsed, cap_seconds)
                ),
            )
        except asyncio.CancelledError:
            kill_process_group(state.pid)
            raise
        finally:
            # Reap our own direct child (never a re-adopted one — this
            # process is not its parent, so waitpid would raise ECHILD) so a
            # long-lived orchestrator does not accumulate zombies.
            if proc is not None:
                try:
                    await asyncio.to_thread(proc.wait, 5)
                except subprocess.TimeoutExpired:
                    pass
        duration = time.monotonic() - state.started_monotonic
        if exit_code is None:
            raise _CommandDied("died with the orchestrator (no exit file, pid gone)")
        return CommandResult(cmd=command.cmd, exit_status=exit_code, duration_s=duration)

    async def _await_exit(
        self,
        pid: int,
        exit_path: Path,
        started_monotonic: float,
        cap_seconds: float,
        relabel: Callable[[float], None] | None = None,
    ) -> int | None:
        """Poll until the child's exit file appears (or the child vanishes).

        ``relabel`` is called with the elapsed seconds on every poll so the
        heartbeat's ``Ns/caps`` label keeps moving: on r20260924 it froze at
        ``0s/60s`` for a whole command because it was written once, at launch.
        """
        while True:
            if exit_path.is_file():
                text = exit_path.read_text().strip()
                return int(text) if text else None
            elapsed = time.monotonic() - started_monotonic
            if relabel is not None:
                relabel(elapsed)
            if elapsed > cap_seconds:
                kill_process_group(pid)
                raise TimedOut(f"exceeded wall_clock_min cap of {cap_seconds / 60:.2f} minutes")
            await asyncio.sleep(DEFAULT_POLL_INTERVAL_S)
            if not process_alive(pid) and not exit_path.is_file():
                return None

    @staticmethod
    def _phase_label(n: int, total: int, elapsed: float, cap_seconds: float) -> str:
        return f"command {n}/{total} · {elapsed:.0f}s/{cap_seconds:.0f}s"

    def _heartbeat_phase(
        self, n: int, total: int, state: RunChildState, cap_seconds: float
    ) -> None:
        elapsed = time.monotonic() - state.started_monotonic
        self._heartbeat.mark_phase(self._phase_label(n, total, elapsed, cap_seconds))

    # ------------------------------------------------------------ output

    def _read_measurements(self) -> tuple[dict, bool]:
        if not self.args.measurements:
            return {}, False
        path = self.workspace / self.args.measurements
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}, True
        if not isinstance(raw, dict):
            return {}, True
        return {k: v for k, v in raw.items() if isinstance(v, (int, float, str, bool))}, False

    def _porcelain_offenders(self) -> list[str]:
        status = _git_ok(self.workspace, "status", "--porcelain").splitlines()
        offenders = []
        for line in status:
            if not line.strip():
                continue
            path = line[3:].strip()
            if "->" in path:  # rename entries: "old -> new"
                path = path.split(" -> ", 1)[1].strip()
            if not any(fnmatch.fnmatch(path, pattern) for pattern in self.args.commit_paths):
                offenders.append(path)
        return offenders

    async def _commit_and_merge(self) -> str:
        if self.args.commit_paths and _git_ok(self.workspace, "status", "--porcelain").strip():
            _git(self.workspace, "add", "-A", "--", *self.args.commit_paths)
            _git(
                self.workspace,
                "commit",
                "-q",
                "-m",
                f"run({self.deps.run_id}): {self.gid} {self.group.name}",
            )
        # With nothing committed the unit "never commits" (plan): skip the merge,
        # which refuses an empty branch, and record the commit the run ran on.
        base = integration_branch(self.deps.run_id)
        base_exists = _git(self.workspace, "rev-parse", "--verify", "--quiet", base).returncode == 0
        if base_exists and commits_ahead(self.workspace, base, "HEAD") == 0:
            log_event(self.paths, f"group {self.gid}: run recipe committed nothing; merge skipped")
            return _git_ok(self.workspace, "rev-parse", "HEAD").strip()
        try:
            return await asyncio.to_thread(self.deps.merge_group, self.group, self.workspace)
        except (MergeConflict, PreflightFailure) as exc:
            await self._triage_and_fail(f"group {self.gid}: merge failed: {exc}")
            raise RuntimeError("unreachable") from exc

    def _build_summary(self, results: list[CommandResult], measurements_missing: bool) -> str:
        parts = [f"{r.cmd!r} exit={r.exit_status} ({r.duration_s:.1f}s)" for r in results]
        text = f"run recipe: {len(results)} command(s) — " + "; ".join(parts)
        if measurements_missing:
            text += " (measurements_missing)"
        if self._surprises:
            noted = []
            for surprise in self._surprises:
                desc = " ".join(surprise.description.split())
                if len(desc) > SURPRISE_SUMMARY_MAX_CHARS:
                    desc = desc[: SURPRISE_SUMMARY_MAX_CHARS - 1] + "…"
                noted.append(f"[{surprise.kind}] {desc}")
            text += "; surprises consumed: " + "; ".join(noted)
        return text[:ARTIFACT_SUMMARY_MAX_CHARS]

    def _write_verification_report(
        self, results: list[CommandResult], attempt_no: int, summary: str
    ) -> None:
        """The synthetic coder-shaped report (``RunVerificationReport``): one
        ``pass`` per verification item whose ``Run:`` is a declared command
        that exited 0 on this attempt. Every other item is left out — the
        report layer reads those as ``recipe``, never ``unverified``."""
        passed_cmds = {" ".join(r.cmd.split()): r for r in results if r.exit_status == 0}
        verification_results = []
        for item in self.group.verification:
            command = run_command_for_item(item.description, self.args.commands)
            if command is None:
                continue
            result = passed_cmds.get(" ".join(command.cmd.split()))
            if result is None:
                continue
            verification_results.append(
                VerificationResult(
                    item_id=item.id,
                    status="pass",
                    notes=(
                        f"run recipe attempt {attempt_no}: `{command.cmd}` exit 0 "
                        f"in {result.duration_s:.1f}s"
                    ),
                )
            )
        self.deps.store.save_group_artifact(
            self.gid,
            artifact_name("report", self.ctx.generation, attempt_no),
            RunVerificationReport(summary=summary, verification_results=verification_results),
        )

    def _output_sha256s(self) -> dict[str, str]:
        """sha256 of every declared output that exists in the workspace right now."""
        sha256 = {}
        for out_path in self.args.outputs:
            full = self.workspace / out_path if self.workspace else None
            if full is not None and full.is_file():
                sha256[out_path] = hashlib.sha256(full.read_bytes()).hexdigest()
        return sha256

    def _register_artifact(
        self,
        record: RunRecord,
        commit: str,
        measurements_missing: bool,
        sha256: dict[str, str],
    ) -> None:
        store = self.deps.artifacts
        if store is None:
            return
        store.register(
            ArtifactEntry(
                artifact_id=self.gid,
                group_id=self.gid,
                tasks=list(self.group.tasks),
                recipe=self.group.recipe,
                paths=list(record.outputs),
                sha256=sha256,
                commit=commit,
                schema="RunRecord",
                summary=record.summary,
                status="complete",
                measurements=dict(record.measurements),
            )
        )

    # --------------------------------------------------------------- triage

    def _triage_prompt(self, failure_summary: str) -> str:
        commands_block = "\n".join(
            f"{i + 1}. {c.cmd} (cap {c.wall_clock_min}min)"
            for i, c in enumerate(self.args.commands)
        )
        return Template(load_template("run_triage")).substitute(
            group_id=self.gid,
            group_name=self.group.name,
            failure_summary=failure_summary,
            commands_block=commands_block or "(none)",
            output_block=self._output_tail(),
        )

    def _output_tail(self, max_bytes: int = 4000) -> str:
        """The last command's stdout/stderr tails for the triage prompt. Without
        them the triage reads only the failure summary and guesses: on
        r20260925-101742 it blamed a slow sampler for a 20-minute timeout whose
        stderr said ``Permission denied`` in its first line."""
        run_dir = self._run_dir()
        attempts = self._existing_attempts(run_dir)
        if not attempts:
            return "(no command output recorded)"
        attempt_dir = run_dir / f"attempt-{attempts[-1]}"
        recorded = sorted(
            (p for p in attempt_dir.glob("*.out") if p.stem.isdigit()), key=lambda p: int(p.stem)
        )
        if not recorded:
            return "(no command output recorded)"
        n = recorded[-1].stem
        parts = []
        for stream in ("out", "err"):
            path = attempt_dir / f"{n}.{stream}"
            text = ""
            if path.is_file():
                text = path.read_bytes()[-max_bytes:].decode("utf-8", "replace").strip()
            parts.append(f"command {n} std{stream} (last {max_bytes} bytes):\n{text or '(empty)'}")
        return "\n\n".join(parts)

    async def _triage_and_fail(self, failure_summary: str) -> None:
        log_event(self.paths, f"group {self.gid}: run failure — {failure_summary}")
        verdict, diagnosis = "work_failure", failure_summary
        if self.deps.triage is not None:
            try:
                prompt = self._triage_prompt(failure_summary)
                result = await asyncio.to_thread(self.deps.triage, prompt)
                verdict = result.get("verdict", "work_failure")
                diagnosis = result.get("diagnosis", failure_summary)
            except Exception as exc:  # noqa: BLE001 — triage is one-shot, best-effort
                diagnosis = f"{failure_summary} (triage call failed: {exc})"
        message = (
            failure_summary if diagnosis == failure_summary else f"{failure_summary}\n{diagnosis}"
        )
        if verdict == "needs_decision":
            response = await self._escalate_decision(message)
            if response is not None and response.answer:
                message = f"{message}\n[operator] {response.answer}"
        run_dir = self._run_dir()
        attempts = self._existing_attempts(run_dir)
        if attempts:
            self._write_settled(run_dir / f"attempt-{attempts[-1]}", "failed", message)
        raise GroupFailure(message)

    async def _escalate_decision(self, prompt_text: str):
        broker, policy = self.deps.broker, self.deps.policy
        if (
            broker is None
            or policy is None
            or not policy.should_escalate(EscalationKind.CODER_BLOCKED)
        ):
            return None
        request = EscalationRequest(
            id=uuid.uuid4().hex[:12],
            run_id=self.deps.run_id,
            group_id=self.gid,
            generation=self.ctx.generation,
            kind=EscalationKind.CODER_BLOCKED,
            prompt=prompt_text,
            context=EscalationContext(),
        )
        response = await asyncio.to_thread(broker.raise_escalation, request)
        if response is None:
            return None
        if response.action == HumanAction.ABORT:
            broker.trigger_abort()
            raise RunAbort(
                f"operator aborted the run at group {self.gid} (run recipe needs_decision)"
            )
        return response


class _CommandDied(Exception):
    """A recorded Run Child pid is gone with no exit file — it died along
    with a previous orchestrator process rather than completing."""
