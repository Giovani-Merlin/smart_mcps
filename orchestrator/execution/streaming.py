"""Bidirectional stream-json worker channel (plan U1).

``SessionRunner`` used to shell the claude CLI with ``--output-format json`` and
block on a single ``proc.communicate()`` — the orchestrator could only see or
speak to a session between rounds. This module reshapes that into an
incremental reader over ``--output-format stream-json --include-partial-messages``,
so a caller gets one callback per assistant turn *as the round runs*, and can
write a follow-up message onto the child's stdin (``--input-format
stream-json``) from inside that callback — while the round is still in flight,
not after it ends.

The initial prompt still rides ``-p <prompt>`` on argv, exactly as before;
``--input-format stream-json`` only widens what the child accepts after that —
mid-round follow-ups — it does not change how the round starts. Reshaping
prompt delivery itself is out of scope here and not required by anything this
module's callers need.

No per-round timeout here either, for the same reason ``_spawn`` never had one
(sessions.py's R7 note): a token ceiling is a proxy for cost, not for stuck,
and wall-clock is a terrible proxy for either. A hung stream is only detected
by the process itself exiting or by the caller closing stdin/killing it.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class StreamError(Exception):
    """The stream-json child process failed or produced an unusable stream."""


@dataclass(frozen=True)
class TurnUsage:
    """Usage of exactly one assistant turn within a round, from a stream-json
    ``assistant`` event's ``message.usage`` — the per-turn observation a
    between-rounds check on the final envelope cannot provide."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def from_message_usage(cls, usage: dict) -> TurnUsage:
        return cls(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        )


@dataclass
class StreamOutcome:
    """What a completed (exited) stream-json process produced."""

    returncode: int
    envelope: dict | None  # the terminal "result" event, or None if never seen
    stderr: str


class SubprocessTracker(Protocol):
    """Matches ``sessions.SubprocessTracker`` — duplicated here rather than
    imported to keep this module free of a dependency on ``sessions.py`` (the
    dependency runs the other way: ``sessions.py`` imports this module)."""

    def spawned(self, pid: int, context: str) -> None: ...

    def exited(self, pid: int) -> None: ...


class StreamingProcess:
    """One tracked ``claude`` subprocess speaking stream-json in both directions.

    ``start()`` launches the child and begins reading its stdout on a
    background thread, calling ``on_turn`` once per assistant event as it
    arrives — the round is still running when that callback fires, so a
    callback that calls ``send()`` is a genuine mid-round follow-up, not a new
    process. ``wait()`` blocks for the child to exit, joins the reader
    threads, and returns the terminal ``result`` event as a
    ``StreamOutcome`` — it never raises; the caller (``sessions.SessionRunner``)
    decides what a non-zero exit or a missing terminal result means.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        on_turn: Callable[[TurnUsage], None] | None = None,
        tracker: SubprocessTracker | None = None,
        context: str = "",
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self.on_turn = on_turn
        self._tracker = tracker
        self._context = context
        self._proc: subprocess.Popen[str] | None = None
        self._result_envelope: dict | None = None
        self._stderr_lines: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._exit_reported = False
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        assert self._proc is not None
        return self._proc.pid

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._argv,
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self._env,
        )
        if self._tracker is not None:
            self._tracker.spawned(self._proc.pid, self._context)
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "assistant":
                usage = ((event.get("message") or {}).get("usage")) or {}
                if usage and self.on_turn is not None:
                    self.on_turn(TurnUsage.from_message_usage(usage))
            elif event_type == "result":
                self._result_envelope = event

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr_lines.append(line)

    def send(self, text: str) -> None:
        """Write a well-formed stream-json user message onto the child's
        stdin. Safe to call from inside ``on_turn`` while the round is still
        running — that is the whole point of the channel."""
        assert self._proc is not None and self._proc.stdin is not None
        message = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        with self._lock:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()

    def close_stdin(self) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except (OSError, ValueError):
                pass

    def wait(self) -> StreamOutcome:
        """Block until the child exits; join the reader threads so every event
        already on stdout/stderr has been consumed before returning."""
        assert self._proc is not None
        self.close_stdin()
        returncode = self._proc.wait()
        if self._tracker is not None and not self._exit_reported:
            self._tracker.exited(self._proc.pid)
            self._exit_reported = True
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=5)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        return StreamOutcome(
            returncode=returncode,
            envelope=self._result_envelope,
            stderr="".join(self._stderr_lines),
        )
