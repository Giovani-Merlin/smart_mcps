"""In-process activity registry for stream-json worker children (plan U1).

This is the foundation the Sign of Life mechanism (plan U2) stands on: every
stream event a worker child emits stamps an ``ActivityRegistry`` entry, so a
later per-tick probe can ask "what did this group's child last do, and when"
without parsing a transcript or touching disk. Nothing here decides whether a
child is stuck — R3 keeps that call, evidence-only, one layer up in the
liveness probe. This module only ever records facts: it has no window, no
threshold, and no verdict.

The registry lives for the lifetime of the orchestrator process. It is fed by
``SessionRunner._spawn`` and read by ``RoundHeartbeat`` (via an
``activity_provider`` callable) and by the liveness probe. Every method is
thread-safe and none of them can raise on a caller's behalf — a session
lookup or a hook failure must never take down a round, the same contract
``heartbeat.py`` already holds itself to.
"""

from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass

#: How much of the last assistant turn's text is worth keeping. This rides on
#: an in-memory record, not a report, so it is capped generously but still
#: bounded — a worker cannot make this grow without limit just by talking.
_LAST_ASSISTANT_TEXT_MAX_CHARS = 2000


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


@dataclass
class ChildActivity:
    """Everything the registry knows about one live (or just-exited) worker
    child. ``cured`` is set once by the liveness probe (plan U5) after it has
    killed this pid as a Suspend Cure, so the runner can tell that apart from
    a genuine failure."""

    pid: int
    session_id: str
    cwd: str
    spawned_at: str
    last_event_at: str | None = None
    last_event_type: str | None = None
    last_assistant_text: str = ""
    cured: bool = False


class ActivityRegistry:
    """Thread-safe, in-process record of every worker child's most recent
    activity, keyed by pid.

    Entries are never evicted on ``exited`` — a pid that has just exited is
    exactly the one ``was_cured`` needs to answer for, in ``SessionRunner``'s
    own next few lines of code — so ``exited`` only drops the pid from the
    "live" set that ``current`` searches. The dict itself lives for the
    process's lifetime; a pid is a small, bounded record, and a run's worker
    count is nowhere near large enough for that to matter.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: dict[int, ChildActivity] = {}
        # Spawn order, oldest first — `current` walks it in reverse so the
        # newest spawn for a given cwd wins, per the contract.
        self._order: list[int] = []
        self._live: set[int] = set()

    def spawned(self, pid: int, *, session_id: str, cwd: str) -> None:
        with self._lock:
            self._children[pid] = ChildActivity(
                pid=pid, session_id=session_id, cwd=cwd, spawned_at=_now()
            )
            self._order.append(pid)
            self._live.add(pid)

    def note_event(self, pid: int, event_type: str) -> None:
        """Stamp the latest stream event a child produced. A no-op for a pid
        the registry never saw ``spawned`` for — never raises, since the
        stream reader that calls this must never be interrupted by it."""
        with self._lock:
            child = self._children.get(pid)
            if child is None:
                return
            child.last_event_at = _now()
            child.last_event_type = event_type

    def note_assistant_text(self, pid: int, text: str) -> None:
        """Record the text of the child's last assistant turn. A no-op for an
        unknown pid, same contract as ``note_event``."""
        with self._lock:
            child = self._children.get(pid)
            if child is None:
                return
            child.last_assistant_text = text[:_LAST_ASSISTANT_TEXT_MAX_CHARS]

    def exited(self, pid: int) -> None:
        with self._lock:
            self._live.discard(pid)

    def current(self, cwd: str) -> ChildActivity | None:
        """The newest live child whose cwd equals *cwd*, or ``None`` when
        none is live there (including once its exit has been recorded)."""
        with self._lock:
            for pid in reversed(self._order):
                if pid not in self._live:
                    continue
                child = self._children.get(pid)
                if child is not None and child.cwd == cwd:
                    return child
        return None

    def mark_cured(self, pid: int) -> None:
        with self._lock:
            child = self._children.get(pid)
            if child is not None:
                child.cured = True

    def was_cured(self, pid: int) -> bool:
        with self._lock:
            child = self._children.get(pid)
            return bool(child is not None and child.cured)
