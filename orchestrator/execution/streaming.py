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
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
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


#: Text in a ``tool_result`` that looks like a refusal or a kernel denial. Kept
#: here rather than imported from ``denial.py`` because this is a *collection*
#: filter, not the classifier: its job is to keep the passive signal small enough
#: to carry, and it deliberately errs wide — the classifier decides.
_DENY_SIGNAL_RE = re.compile(
    r"permission denied|os error 13|EACCES|\[Errno 13\]|EPERM|"
    r"read-only file system|operation not permitted|"
    r"requires? (?:approval|permission)|not allowed|"
    r"tool use was (?:rejected|denied|blocked)|user (?:rejected|denied)",
    re.IGNORECASE,
)

#: Cap per signal and in total. This rides on an outcome object and exists only to
#: corroborate a classification, so it must never grow with a chatty build log.
_DENY_SIGNAL_MAX_CHARS = 500
_DENY_SIGNAL_MAX_COUNT = 10


def _tool_result_text(content: object) -> str:
    """Flatten a ``tool_result`` block's ``content`` to text.

    The field is a union in practice — a plain string, or a list of typed blocks —
    so both are handled rather than assuming whichever one this session happened
    to see first.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(part for part in parts if part)
    return ""


@dataclass
class StreamOutcome:
    """What a completed (exited) stream-json process produced."""

    returncode: int
    envelope: dict | None  # the terminal "result" event, or None if never seen
    stderr: str
    #: Refusal/errno text seen in `tool_result` blocks during the round (plan P2).
    #: Passive and advisory: an *independent* corroborator for attributing a
    #: `permission_denied` report, since it comes from the harness rather than from
    #: the model's own account of what happened. Never the sole basis for a
    #: classification — the CLI owns this wording and may change it.
    deny_signals: list[str] = field(default_factory=list)


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
        preexec_fn: Callable[[], None] | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self.on_turn = on_turn
        self._tracker = tracker
        self._context = context
        self._preexec_fn = preexec_fn
        self._proc: subprocess.Popen[str] | None = None
        self._result_envelope: dict | None = None
        self._stderr_lines: list[str] = []
        self._deny_signals: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._exit_reported = False
        self._lock = threading.Lock()
        # Follow-ups written by `send()` since the last `result` event. A round
        # ends when the child reports `result` with nothing outstanding; until
        # then a mid-round `send()` means one more `result` is still owed. See
        # `_read_stdout` for why this decides when stdin closes.
        self._pending_followups = 0
        self._stdin_closed = False

    @property
    def pid(self) -> int:
        assert self._proc is not None
        return self._proc.pid

    def start(self, prompt: str | None = None) -> None:
        """Launch the child and, when *prompt* is given, write it as the round's
        opening stream-json message.

        The prompt **must** travel over stdin: under ``--input-format
        stream-json`` the CLI ignores a prompt passed as ``-p <text>`` entirely,
        emitting neither an assistant turn nor a ``result`` before exiting 0.
        Passing it on argv looked right and did nothing.
        """
        self._proc = subprocess.Popen(
            self._argv,
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self._env,
            preexec_fn=self._preexec_fn,
        )
        if self._tracker is not None:
            self._tracker.spawned(self._proc.pid, self._context)
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        if prompt is not None:
            # Not counted as a follow-up: this message *is* the round, and the
            # `result` answering it is what ends it.
            self._write_message(prompt)

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
            elif event_type == "user":
                # `user` events carry the `tool_result` blocks — i.e. what actually
                # came back from every tool call. This branch did not exist, so
                # every one of them was dropped: the only account of a denial the
                # orchestrator had was the model's own, in its report. Collected
                # passively here, capped, and used to corroborate that account.
                self._collect_deny_signals(event)
            elif event_type == "result":
                self._result_envelope = event
                # The child does **not** exit on `result` while stdin is open —
                # it waits for the next message, so `wait()` blocks forever.
                # (Observed live on run r20260812-161423: 4h05m wall clock, 18s
                # CPU, blocked in epoll_wait on an open stdin pipe.) EOF is what
                # ends the process, so close stdin once nothing is outstanding.
                # A follow-up sent mid-round still owes us its own `result`.
                with self._lock:
                    if self._pending_followups > 0:
                        self._pending_followups -= 1
                    else:
                        self._close_stdin_locked()
        # stdout is at EOF: the child is finishing or has died. Never leave stdin
        # open here, or a child that failed without a `result` wedges `wait()`.
        with self._lock:
            self._close_stdin_locked()

    def _collect_deny_signals(self, event: dict) -> None:
        """Harvest refusal/errno text from a ``user`` event's ``tool_result`` blocks.

        Wrapped broadly and capped: this is advisory evidence, so a surprising
        event shape must cost nothing. A round that fails because its *diagnostics*
        raised would be a strictly worse outcome than the opaque denial this exists
        to explain.
        """
        try:
            with self._lock:
                if len(self._deny_signals) >= _DENY_SIGNAL_MAX_COUNT:
                    return
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _tool_result_text(block.get("content"))
                if not text or not _DENY_SIGNAL_RE.search(text):
                    continue
                with self._lock:
                    if len(self._deny_signals) >= _DENY_SIGNAL_MAX_COUNT:
                        return
                    self._deny_signals.append(text[:_DENY_SIGNAL_MAX_CHARS])
        except Exception:  # noqa: BLE001 — advisory evidence never fails a round
            pass

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            self._stderr_lines.append(line)

    def send(self, text: str) -> None:
        """Write a well-formed stream-json user message onto the child's
        stdin. Safe to call from inside ``on_turn`` while the round is still
        running — that is the whole point of the channel."""
        with self._lock:
            self._pending_followups += 1
            self._write_message_locked(text)

    def _write_message(self, text: str) -> None:
        with self._lock:
            self._write_message_locked(text)

    def _write_message_locked(self, text: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        if self._stdin_closed:
            return
        message = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError):
            # The child exited underneath us; `wait()` reports the real failure.
            self._stdin_closed = True

    def _close_stdin_locked(self) -> None:
        if self._stdin_closed:
            return
        self._stdin_closed = True
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except (OSError, ValueError):
                pass

    def close_stdin(self) -> None:
        with self._lock:
            self._close_stdin_locked()

    def wait(self) -> StreamOutcome:
        """Block until the child exits; join the reader threads so every event
        already on stdout/stderr has been consumed before returning."""
        assert self._proc is not None
        # stdin is closed by the reader thread, on the `result` event that ends
        # the round (or at stdout EOF). It deliberately is *not* closed here:
        # this call blocks until the child exits, and the child only exits once
        # stdin is at EOF — closing it after `wait()` returns would deadlock.
        # An earlier version assumed the CLI terminates on its own `result`
        # event; it does not, and that assumption hung a run for four hours.
        returncode = self._proc.wait()
        self.close_stdin()
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
            deny_signals=list(self._deny_signals),
        )
