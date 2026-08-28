"""Round heartbeat: evidence that a group is still moving, never a verdict.

An operator staring at a group that has said nothing for 40 minutes has no way to
tell a long round from a wedged one. The obvious fix — a ``STALLED`` state, or a
``stalled: true`` field — is the wrong one: the moment "stalled" is persisted it
becomes a de facto state that future code branches on, and R7's no-timeout
decision stands. So this module writes *facts only* — when the current round
started, what round it is, and when the writer last ran — and leaves "is it
stalled?" to the reader, who can weigh heartbeat age against a transcript mtime
and decide for itself.

Two properties are contractual:

- **It cannot fail a round.** Every write is wrapped; an unwritable run directory
  loses the evidence, not the work.
- **It cannot hold the process open.** The writer is a daemon thread, so a run
  that finishes while a tick is pending still exits.
"""

from __future__ import annotations

import datetime
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from orchestrator.execution.manifest import RunPaths, atomic_write_text

HEARTBEAT_NAME = "heartbeat.json"
SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 15.0
#: How often the heartbeat writes a line to the run log. Far slower than the
#: file tick: `heartbeat.json` is polled by a reader that wants freshness, while
#: the run log is read by a human (and streamed to the observatory's log pane),
#: where one line a minute is presence and four would be noise.
DEFAULT_LOG_INTERVAL_SECONDS = 60.0


def _humanize(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def heartbeat_path(paths: RunPaths, group_id: str | None) -> Path:
    """Where a heartbeat lives. ``group_id=None`` is the *run*-scoped heartbeat,
    used for the phases that happen before any group exists (establishing the
    base session), so it sits beside `manifest.json` rather than under a group
    directory that would have to be invented for it."""
    if group_id is None:
        return paths.run_dir / HEARTBEAT_NAME
    return paths.group_dir(group_id) / HEARTBEAT_NAME


def _now() -> str:
    # Milliseconds, not seconds: a reader comparing two consecutive samples to see
    # whether the writer is still alive needs them to differ, and ticks land well
    # inside one second.
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


class RoundHeartbeat:
    """Writes ``heartbeat.json`` for one group from a daemon thread.

    ``mark_round`` is called from the review loop at the points where it already
    logs "round N: started"; it only mutates in-memory fields, so it adds nothing
    to the loop's control flow and cannot change round numbering.

    With ``group_id=None`` the same machinery covers a *run*-scoped phase — the
    base session, which runs before any group exists and was the first long
    silence an operator met. Rounds are never marked in that mode; only phases.
    """

    def __init__(
        self,
        paths: RunPaths,
        group_id: str | None,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        log: Callable[[str], None] | None = None,
        log_interval: float = DEFAULT_LOG_INTERVAL_SECONDS,
    ) -> None:
        self.paths = paths
        self.group_id = group_id
        # What the periodic line calls the thing it is reporting on. A run-scoped
        # heartbeat has no group to name, and "group None" would be worse than
        # nothing in the one log an operator reads while waiting.
        self.subject = f"group {group_id}" if group_id is not None else f"run {paths.run_id}"
        self.interval = interval
        self._log = log
        self.log_interval = log_interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = _now()
        self._generation = 0
        self._round = 0
        self._round_started_at: str | None = None
        self._round_started_mono: float | None = None
        self._phase: str | None = None
        self._phase_since = time.monotonic()
        # An externally-owned phase that temporarily shadows `_phase` — see
        # `push_phase`. Not folded into `_phase`, because what the group goes
        # back to when the overlay lifts has to survive it.
        self._overlay: str | None = None
        self._overlay_since = time.monotonic()
        # Paused time accumulates across every push/pop cycle within the current
        # round, so a round with several rate-limit pauses reports their sum, not
        # just the last one. Reset only when a new round starts.
        self._paused_accum = 0.0
        self._last_log = time.monotonic()
        # Optional per-tick side task (plan F9): the review loop hangs its
        # transcript probe here — `start_fork` blocks for the round's whole
        # first turn, so this thread is the only thing awake to notice the
        # transcript file appearing. Same contract as everything else here: it
        # can never fail a round, so `_loop` wraps the call.
        self.on_tick: Callable[[], None] | None = None

    # ------------------------------------------------------------------ facts

    def mark_round(self, generation: int, round_no: int) -> None:
        """Record that a round just started. In-memory; the thread does the I/O."""
        with self._lock:
            self._generation = generation
            self._round = round_no
            self._round_started_at = _now()
            self._round_started_mono = time.monotonic()
            self._paused_accum = 0.0
        # A round is itself a phase, so starting one ends whatever came before
        # (the fork, a rewrite) and restarts the elapsed clock. `mark_phase`
        # writes, so this needs no write of its own.
        self.mark_phase("running")

    def mark_phase(self, phase: str) -> None:
        """Name what the group is doing between round boundaries.

        Rounds are not fine-grained enough to explain a silence: forking the base
        session for a group's first prompt took 21 minutes on a real run, all of
        it inside one blocking call before round 1 existed, so both the log and
        the heartbeat could only say "nothing yet".
        """
        with self._lock:
            self._phase = phase
            self._phase_since = time.monotonic()
            # Reset the log clock so the next periodic line is measured from the
            # phase, not from whenever the previous one happened to fire.
            self._last_log = time.monotonic()
        # Written immediately, not left to the next tick. A phase change is
        # announced precisely because the process is about to block for a long
        # time, so a reader polling `heartbeat.json` in that window would
        # otherwise be told the *previous* phase for up to a full interval — long
        # enough to make a re-entry look like it was still on the round before.
        self.write_once()

    def push_phase(self, phase: str) -> None:
        """Overlay a phase owned by something outside this group's loop, keeping
        the underlying one to restore.

        The rate-limit gate is the case this exists for: it pauses from a worker
        thread, on behalf of every group at once, and it must not clobber the
        phase the review loop set — that phase is exactly what the group goes
        back to doing when the limit releases. Overlays do not nest; a second
        push replaces the overlay and keeps the original base phase.
        """
        with self._lock:
            self._overlay = phase
            self._overlay_since = time.monotonic()
        self.write_once()

    def pop_phase(self) -> None:
        """Drop the overlay; the phase underneath becomes current again."""
        with self._lock:
            if self._overlay is None:
                return
            self._paused_accum += time.monotonic() - self._overlay_since
            self._overlay = None
        self.write_once()

    def _paused_seconds_locked(self, now: float) -> float:
        """Total paused time this round: completed push/pop cycles plus, if an
        overlay is active right now, its still-running portion. Caller holds
        ``self._lock``."""
        paused = self._paused_accum
        if self._overlay is not None:
            paused += now - self._overlay_since
        return paused

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._overlay is not None:
                phase: str | None = self._overlay
                phase_since = self._overlay_since
            else:
                phase = self._phase
                phase_since = self._phase_since
            round_elapsed = (
                round(now - self._round_started_mono, 1)
                if self._round_started_mono is not None
                else None
            )
            paused = round(self._paused_seconds_locked(now), 1)
            return {
                "schema_version": SCHEMA_VERSION,
                "group_id": self.group_id,
                "started_at": self._started_at,
                "generation": self._generation,
                "round": self._round,
                "round_started_at": self._round_started_at,
                "phase": phase,
                "phase_elapsed_s": round(now - phase_since, 1),
                "round_elapsed_s": round_elapsed,
                "paused_s": paused,
                "updated_at": _now(),
            }

    def _due_log_line(self) -> str | None:
        """The periodic "still here" line, or None when one is not due yet.

        Facts only, like everything else here: what it is doing and for how long.
        It does not say "stalled" — that remains the reader's call.

        Honours the overlay exactly as ``snapshot`` does: without this, a group
        paused on the rate-limit gate keeps logging its pre-pause phase, which is
        the asymmetry that made the periodic line lie about what was actually
        happening.
        """
        with self._lock:
            now = time.monotonic()
            if self._log is None or now - self._last_log < self.log_interval:
                return None
            self._last_log = now
            if self._overlay is not None:
                phase = self._overlay
                phase_since = self._overlay_since
            else:
                phase = self._phase or "working"
                phase_since = self._phase_since
            elapsed = now - phase_since
            where = f" (generation {self._generation} round {self._round})" if self._round else ""
            round_elapsed = (
                now - self._round_started_mono if self._round_started_mono is not None else None
            )
            paused = self._paused_seconds_locked(now)
        line = f"{self.subject}: still {phase}{where}, {_humanize(elapsed)} elapsed"
        if round_elapsed is not None:
            line += f" ({_humanize(round_elapsed)} elapsed, {_humanize(paused)} paused)"
        return line

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self.write_once()
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.group_id or self.paths.run_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        self.write_once()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.write_once()
            self._maybe_log()
            self._maybe_on_tick()

    def _maybe_on_tick(self) -> None:
        """Run the caller's tick hook, if any — same never-fail contract as
        ``write_once``: evidence gathering must not be able to kill a round."""
        hook = self.on_tick
        if hook is None:
            return
        try:
            hook()
        except Exception:  # noqa: BLE001 - see write_once
            pass

    def _maybe_log(self) -> None:
        """Same contract as ``write_once``: evidence is never worth a round, so a
        failing log sink is swallowed rather than allowed to kill the thread."""
        try:
            line = self._due_log_line()
            if line is not None and self._log is not None:
                self._log(line)
        except Exception:  # noqa: BLE001 - see write_once
            pass

    def write_once(self) -> None:
        """Best-effort by contract: an audit write can never fail a round.

        The catch is deliberately broad. The narrow ``OSError`` version would be
        right if the filesystem were the only thing that can go wrong here, but a
        bug in this module would then take down a group's work over a file
        nothing reads back — the exact trade this whole feature refuses.
        """
        try:
            path = heartbeat_path(self.paths, self.group_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename, like every other artifact the observatory reads:
            # this file is rewritten every few seconds and polled concurrently, so
            # a plain write would hand readers a truncated file routinely.
            atomic_write_text(path, json.dumps(self.snapshot(), indent=2) + "\n")
        except Exception:  # noqa: BLE001 - evidence is never worth a round
            pass


def read_heartbeat(paths: RunPaths, group_id: str | None) -> dict | None:
    """Read a group's heartbeat, or None when there is none to read.

    Every run currently on disk predates this file, so absence is the normal
    case and must never surface as an error.
    """
    path = heartbeat_path(paths, group_id)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
