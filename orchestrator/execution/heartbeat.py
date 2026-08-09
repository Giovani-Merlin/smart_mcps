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
from pathlib import Path

from orchestrator.execution.manifest import RunPaths, atomic_write_text

HEARTBEAT_NAME = "heartbeat.json"
SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 15.0


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
    ) -> None:
        self.paths = paths
        self.group_id = group_id
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = _now()
        self._generation = 0
        self._round = 0
        self._round_started_at: str | None = None

    # ------------------------------------------------------------------ facts

    def mark_round(self, generation: int, round_no: int) -> None:
        """Record that a round just started. In-memory; the thread does the I/O."""
        with self._lock:
            self._generation = generation
            self._round = round_no
            self._round_started_at = _now()
        self.write_once()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "group_id": self.group_id,
                "started_at": self._started_at,
                "generation": self._generation,
                "round": self._round,
                "round_started_at": self._round_started_at,
                "updated_at": _now(),
            }

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
