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


def heartbeat_path(paths: RunPaths, group_id: str) -> Path:
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
    """

    def __init__(
        self,
        paths: RunPaths,
        group_id: str,
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        log: Callable[[str], None] | None = None,
        log_interval: float = DEFAULT_LOG_INTERVAL_SECONDS,
    ) -> None:
        self.paths = paths
        self.group_id = group_id
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
        self._phase: str | None = None
        self._phase_since = time.monotonic()
        self._last_log = time.monotonic()

    # ------------------------------------------------------------------ facts

    def mark_round(self, generation: int, round_no: int) -> None:
        """Record that a round just started. In-memory; the thread does the I/O."""
        with self._lock:
            self._generation = generation
            self._round = round_no
            self._round_started_at = _now()
        # A round is itself a phase, so starting one ends whatever came before
        # (the fork, a rewrite) and restarts the elapsed clock.
        self.mark_phase("running")
        self.write_once()

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

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "group_id": self.group_id,
                "started_at": self._started_at,
                "generation": self._generation,
                "round": self._round,
                "round_started_at": self._round_started_at,
                "phase": self._phase,
                "phase_elapsed_s": round(time.monotonic() - self._phase_since, 1),
                "updated_at": _now(),
            }

    def _due_log_line(self) -> str | None:
        """The periodic "still here" line, or None when one is not due yet.

        Facts only, like everything else here: what it is doing and for how long.
        It does not say "stalled" — that remains the reader's call.
        """
        with self._lock:
            now = time.monotonic()
            if self._log is None or now - self._last_log < self.log_interval:
                return None
            self._last_log = now
            phase = self._phase or "working"
            elapsed = now - self._phase_since
            where = f" (generation {self._generation} round {self._round})" if self._round else ""
        return f"group {self.group_id}: still {phase}{where}, {_humanize(elapsed)} elapsed"

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self.write_once()
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.group_id}", daemon=True
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


def read_heartbeat(paths: RunPaths, group_id: str) -> dict | None:
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
