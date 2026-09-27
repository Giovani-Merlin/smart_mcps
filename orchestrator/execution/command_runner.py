"""Run Child launch, poll and re-adoption, lifted out of ``run_executor.py``
(plan U2) so any executor that needs a confined, re-adoptable detached
command — not only the ``run`` recipe — can reuse it verbatim.

Layout under an attempt directory (the executor owns the directory name and
lifetime; ``CommandRunner`` only reads and writes the per-command files
inside it)::

    <n>.state.json   # RunChildState, written at launch
    <n>.out, <n>.err # command stdout/stderr
    <n>.exit         # exit code text, written atomically by the wrapper
    <n>.cancelled    # present when a deliberate stop killed this child
    <n>.result.json  # CommandResult, written by ``write_result`` once the
                      # command settles
    settled.json      # {"outcome": "failed"|"completed", ...}, written by
                      # ``write_settled`` once the attempt reaches a terminal
                      # outcome

A crash re-entry (the orchestrator restarted mid-command) continues the same
attempt and re-adopts whatever pid its ``<n>.state.json`` names.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.execution.manifest import atomic_write_text, log_event
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
from orchestrator.recipes.run import CommandResult

DEFAULT_POLL_INTERVAL_S = 1.0


class TimedOut(Exception):
    """A command exceeded its declared wall-clock cap."""


class CommandDied(Exception):
    """A recorded Run Child pid is gone with no exit file — it died along
    with a previous orchestrator process rather than completing."""


class CommandRunner:
    """Launches, polls and re-adopts one confined detached command at a
    time, reporting progress on the caller's heartbeat.

    ``config`` is reserved for future recipe-specific tuning (e.g. a
    per-recipe poll interval); it is not read today.
    """

    def __init__(self, paths, gid: str, heartbeat, config=None):
        self.paths = paths
        self.gid = gid
        self.heartbeat = heartbeat
        self.config = config

    # --------------------------------------------------------- attempts

    def existing_attempts(self, run_dir: Path) -> list[int]:
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

    def write_result(self, attempt_dir: Path, n: int, result: CommandResult) -> None:
        atomic_write_text(attempt_dir / f"{n}.result.json", result.model_dump_json(indent=2) + "\n")

    def write_settled(self, attempt_dir: Path, outcome: str, detail: str) -> None:
        atomic_write_text(
            attempt_dir / "settled.json",
            json.dumps({"outcome": outcome, "detail": detail}, indent=2) + "\n",
        )

    # ---------------------------------------------------------- run

    async def run(
        self,
        attempt_dir: Path,
        n: int,
        command,
        cwd: Path,
        *,
        preexec_fn: Callable[[], None] | None,
        env: dict[str, str] | None,
        total: int = 1,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    ) -> CommandResult:
        """Launch (or re-adopt) command ``n`` of ``total`` in ``attempt_dir``
        and block until it settles, reading and writing::

            <n>.state.json   # RunChildState, written at launch
            <n>.out, <n>.err # command stdout/stderr
            <n>.exit         # exit code text, written atomically by the wrapper
            <n>.cancelled    # present when a deliberate stop killed this child

        ``command`` needs only ``.cmd`` (str) and ``.wall_clock_min``
        (float). Raises ``TimedOut`` if the wall-clock cap is exceeded, and
        ``CommandDied`` if a recorded pid is gone with no exit file (died
        with a previous orchestrator process). Neither ``<n>.result.json``
        nor ``settled.json`` is written here — callers persist those via
        ``write_result``/``write_settled`` once they know the run's overall
        outcome.
        """
        cwd.mkdir(parents=True, exist_ok=True)
        state_path = attempt_dir / f"{n}.state.json"
        exit_path = attempt_dir / f"{n}.exit"
        out_path = attempt_dir / f"{n}.out"
        err_path = attempt_dir / f"{n}.err"
        cancelled_path = attempt_dir / f"{n}.cancelled"
        cap_seconds = command.wall_clock_min * 60.0

        state: RunChildState | None = None
        proc: subprocess.Popen | None = None
        if cancelled_path.is_file():
            # A deliberate stop (ctrl-c, Observatory Stop, an abort cancelling
            # in-flight groups) killed this child on purpose — see the
            # CancelledError handler below. It did not "die with the
            # orchestrator": relaunch it instead of spending a triage call
            # and failing the group (smoke r20260926: SIGINT + resume cost a
            # triage call and a `retry` for a command that was never broken).
            self._discard_cancelled_child(n, state_path, exit_path, cancelled_path)
        elif state_path.is_file() and not exit_path.is_file():
            candidate = RunChildState.model_validate_json(state_path.read_text())
            if is_adoptable(candidate, current_boot_id=boot_id()):
                state = candidate
                log_event(
                    self.paths,
                    f"group {self.gid}: re-adopted run child pid {state.pid} for command {n}",
                )
            elif not process_alive(candidate.pid) and not exit_path.is_file():
                raise CommandDied("died with the orchestrator (no exit file, pid gone)")

        if state is None:
            proc = await asyncio.to_thread(
                launch,
                command.cmd,
                cwd=cwd,
                exit_path=exit_path,
                out_path=out_path,
                err_path=err_path,
                preexec_fn=preexec_fn,
                env=env,
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

        self._heartbeat_phase(n, total, state, cap_seconds)
        try:
            exit_code = await self._await_exit(
                state.pid,
                exit_path,
                state.started_monotonic,
                cap_seconds,
                poll_interval,
                relabel=lambda elapsed: self._relabel_heartbeat(n, total, elapsed, cap_seconds),
            )
        except asyncio.CancelledError:
            # Marker first, kill second: the marker is what tells the
            # re-entry this was deliberate, so it must be on disk even if
            # the process is torn down before the grace period ends.
            atomic_write_text(
                cancelled_path,
                json.dumps(
                    {"pid": state.pid, "cancelled_at": datetime.now(UTC).isoformat()}, indent=2
                )
                + "\n",
            )
            log_event(
                self.paths,
                f"group {self.gid}: command {n}/{total} stopped with the orchestrator "
                f"(pid {state.pid} killed); `resume` relaunches it",
            )
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
            raise CommandDied("died with the orchestrator (no exit file, pid gone)")
        return CommandResult(cmd=command.cmd, exit_status=exit_code, duration_s=duration)

    async def _await_exit(
        self,
        pid: int,
        exit_path: Path,
        started_monotonic: float,
        cap_seconds: float,
        poll_interval: float,
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
            await asyncio.sleep(poll_interval)
            if not process_alive(pid) and not exit_path.is_file():
                return None

    @staticmethod
    def _phase_label(n: int, total: int, elapsed: float, cap_seconds: float) -> str:
        return f"command {n}/{total} · {elapsed:.0f}s/{cap_seconds:.0f}s"

    def _relabel_heartbeat(self, n: int, total: int, elapsed: float, cap_seconds: float) -> None:
        """Refresh the moving ``Ns/caps`` label *and* carry it to disk now.

        ``relabel_phase`` alone leaves the write to the heartbeat's regular
        tick (15 s), so a ``status`` a few seconds into a command read the
        ``0s`` label ``mark_phase`` wrote at launch until the next tick came
        round. One small atomic write per poll is what keeps the label honest.
        """
        self.heartbeat.relabel_phase(self._phase_label(n, total, elapsed, cap_seconds))
        self.heartbeat.write_once()

    def _discard_cancelled_child(
        self, n: int, state_path: Path, exit_path: Path, cancelled_path: Path
    ) -> None:
        """Clear a deliberately-stopped command's remains so it relaunches
        fresh. If the kill never landed (the process was torn down mid-grace)
        and the same child is still there, finish the job first; a stale exit
        file (a child that caught SIGTERM and let the wrapper record ``143``)
        would otherwise be read as this relaunch's exit status."""
        if state_path.is_file():
            try:
                previous = RunChildState.model_validate_json(state_path.read_text())
            except ValueError:
                previous = None
            if previous is not None and is_adoptable(previous, current_boot_id=boot_id()):
                kill_process_group(previous.pid)
        log_event(
            self.paths,
            f"group {self.gid}: command {n} was stopped deliberately last time; relaunching",
        )
        for path in (cancelled_path, state_path, exit_path):
            path.unlink(missing_ok=True)

    def _heartbeat_phase(
        self, n: int, total: int, state: RunChildState, cap_seconds: float
    ) -> None:
        elapsed = time.monotonic() - state.started_monotonic
        self.heartbeat.mark_phase(self._phase_label(n, total, elapsed, cap_seconds))
