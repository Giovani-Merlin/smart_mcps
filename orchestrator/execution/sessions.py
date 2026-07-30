"""claude CLI wrapper: fork-first sessions, blocking print-mode rounds, reports.

Mechanics pinned by the plan's Key Technical Decisions and verified by the U5 spike
(2026-07-16, CLI 2.1.211 — docs/research/design-deviations.md):

- One base session per run; every coder/reviewer session forks from it so the
  shared prefix is byte-identical. Print-mode forking honors ``--session-id`` and
  leaves the base reusable. Fork calls are serialized behind a lock (the session
  store has no documented concurrency guarantees).
- Rounds are blocking ``claude -p`` calls; process exit is round completion.
- The final message must carry a ``<run-report>`` block; a missing/invalid report
  gets a bounded re-nudge, then fails the round (CoCoder's silent-exit lesson,
  docs/research/cocoder-analysis.md §8 point 5).
- Usage comes from the JSON envelope. The breaker's context signal is the latest
  round's input + cache_read + cache_creation + output — ``input_tokens`` alone
  counts only non-cached input and grossly understates context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

REQUIRED_CLI_FLAGS = (
    "--print",
    "--output-format",
    "--resume",
    "--fork-session",
    "--session-id",
    "--name",
    "--json-schema",
)

DEFAULT_MAX_NUDGES = 2

_NUDGE_PROMPT = (
    "Your previous message did not end with a valid report block ({error}). "
    'Reply now with ONLY a <run-report status="..."> block whose body is valid JSON '
    "for the expected report schema — no other text."
)

M = TypeVar("M", bound=BaseModel)


class SessionError(Exception):
    """A claude CLI call failed at the process/envelope level."""


class PreflightError(SessionError):
    """The installed CLI does not support the flags this design pins."""


class ReportError(SessionError):
    """The round's final message never produced a valid report block."""


@dataclass(frozen=True)
class RoundUsage:
    """Token usage of one round, from the CLI JSON envelope."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def context_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    @classmethod
    def from_envelope(cls, envelope: dict) -> RoundUsage:
        """Context occupancy of the round's final turn.

        The envelope's top-level ``usage`` *sums* every turn of the round, so on a
        multi-turn round its cache-read total grows without bound and has nothing to
        do with how full the context actually is — a 190-turn coder round reported
        18.6M against a real occupancy of 262k. ``usage.iterations`` carries the
        per-turn entries, and the last of them is the context the next round would
        resume into. Envelopes without ``iterations`` (older CLIs, the test stub)
        fall back to the top level, where the two are identical for a single turn.
        """
        usage = envelope.get("usage") or {}
        iterations = usage.get("iterations") or []
        latest = iterations[-1] if isinstance(iterations, list) and iterations else usage
        return cls(
            input_tokens=int(latest.get("input_tokens", 0) or 0),
            output_tokens=int(latest.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(latest.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(latest.get("cache_creation_input_tokens", 0) or 0),
        )


@dataclass
class SessionUsage:
    """Cumulative usage for one session across rounds (breaker input, plan U5)."""

    rounds: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_context_tokens: int = 0

    def add(self, usage: RoundUsage) -> None:
        self.rounds += 1
        self.total_input_tokens += usage.input_tokens + usage.cache_creation_input_tokens
        self.total_output_tokens += usage.output_tokens
        self.last_context_tokens = usage.context_tokens


@dataclass(frozen=True)
class RoundResult:
    session_id: str
    text: str
    usage: RoundUsage
    envelope: dict = field(repr=False)


def session_display_name(run_id: str, group_id: str, role: str, generation: int) -> str:
    """The convention pinned on ``SessionEntry.name``: <run_id>-<group_id>-<role>-g<gen>."""
    return f"{run_id}-{group_id}-{role}-g{generation}"


class SubprocessTracker(Protocol):
    """Observes worker subprocess lifetimes so the run state can record live PIDs
    (plan U6: resume terminates orphans before re-entering a session)."""

    def spawned(self, pid: int, context: str) -> None: ...

    def exited(self, pid: int) -> None: ...


class SessionRunner:
    """Owns every claude subprocess call; the only module that shells the CLI."""

    def __init__(
        self,
        *,
        claude_bin: str | Sequence[str] = "claude",
        model: str | None = None,
        permission_mode: str | None = "acceptEdits",
        allowed_tools: Sequence[str] | None = None,
        transcript_root: Path | None = None,
        env: dict[str, str] | None = None,
        tracker: SubprocessTracker | None = None,
        max_thinking_tokens: int | None = None,
        thinking: str | None = None,
    ):
        self._bin = [claude_bin] if isinstance(claude_bin, str) else list(claude_bin)
        self.model = model
        self.max_thinking_tokens = max_thinking_tokens
        self.thinking = thinking
        self.permission_mode = permission_mode
        self.allowed_tools = list(allowed_tools) if allowed_tools else None
        self.transcript_root = transcript_root or Path.home() / ".claude" / "projects"
        self._env = _scrub_virtualenv({**os.environ, **(env or {})})
        self.tracker = tracker
        self._fork_lock = threading.Lock()
        self._usage: dict[str, SessionUsage] = {}

    def preflight(self) -> None:
        """Fail fast with a versioned message if the CLI lacks a pinned flag."""
        help_text = self._capture(["--help"])
        missing = [flag for flag in REQUIRED_CLI_FLAGS if flag not in help_text]
        if missing:
            version = self._capture(["--version"]).strip() or "unknown version"
            raise PreflightError(
                f"claude CLI ({version}) does not support required flags: "
                f"{', '.join(missing)} — upgrade to a version that does"
            )

    def start_base(self, *, run_id: str, base_context: str, cwd: Path) -> RoundResult:
        """Create the run's base session loading the compiled base context."""
        prompt = (
            "Internalize the following shared context for an orchestrated run. "
            "It applies to every task you will be given in this session and its forks. "
            f"Reply with just: OK\n\n{base_context}"
        )
        session_id = str(uuid.uuid4())
        return self._call(
            prompt,
            cwd=cwd,
            extra=["--session-id", session_id, "--name", f"{run_id}-base"],
        )

    def start_fork(
        self,
        *,
        base_id: str,
        prompt: str,
        name: str,
        cwd: Path,
        json_schema: dict | None = None,
    ) -> RoundResult:
        """Fork the base session and run the first round in one blocking call.

        Serialized: the session store has no documented locking (plan Key Technical
        Decisions); forking is fast, so this never serializes the groups themselves.
        """
        session_id = str(uuid.uuid4())
        with self._fork_lock:
            return self._call(
                prompt,
                cwd=cwd,
                extra=[
                    "--resume",
                    base_id,
                    "--fork-session",
                    "--session-id",
                    session_id,
                    "--name",
                    name,
                ],
                json_schema=json_schema,
            )

    def resume(
        self, *, session_id: str, prompt: str, cwd: Path, json_schema: dict | None = None
    ) -> RoundResult:
        """One warm round against an existing session."""
        return self._call(prompt, cwd=cwd, extra=["--resume", session_id], json_schema=json_schema)

    def usage_of(self, session_id: str) -> SessionUsage:
        return self._usage.get(session_id, SessionUsage())

    def transcript_path(self, session_id: str) -> Path | None:
        """Locate the session transcript by UUID — exact regardless of cwd encoding."""
        matches = sorted(self.transcript_root.glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    def _capture(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                [*self._bin, *args], capture_output=True, text=True, env=self._env, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreflightError(f"cannot run {self._bin[0]} {args[0]}: {exc}") from exc
        return result.stdout + result.stderr

    def _call(
        self, prompt: str, *, cwd: Path, extra: list[str], json_schema: dict | None = None
    ) -> RoundResult:
        argv = [*self._bin, "-p", prompt, "--output-format", "json", *extra]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            argv += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.model:
            argv += ["--model", self.model]
        if self.max_thinking_tokens is not None:
            argv += ["--max-thinking-tokens", str(self.max_thinking_tokens)]
        if self.thinking:
            argv += ["--thinking", self.thinking]
        if json_schema is not None:
            argv += ["--json-schema", json.dumps(json_schema)]
        context = _argv_context(extra)
        returncode, stdout, stderr = self._spawn(argv, cwd=cwd, context=context)
        if returncode != 0:
            raise SessionError(f"claude exited {returncode} ({context}): {stderr.strip()[:500]}")
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SessionError(f"claude emitted a non-JSON envelope: {exc}") from exc
        if not isinstance(envelope, dict) or "result" not in envelope:
            raise SessionError("claude envelope is missing the 'result' field")
        if envelope.get("is_error"):
            raise SessionError(f"claude reported an error result: {str(envelope['result'])[:500]}")
        session_id = str(envelope.get("session_id", ""))
        usage = RoundUsage.from_envelope(envelope)
        self._usage.setdefault(session_id, SessionUsage()).add(usage)
        return RoundResult(
            session_id=session_id, text=str(envelope["result"]), usage=usage, envelope=envelope
        )

    def _spawn(self, argv: list[str], *, cwd: Path, context: str) -> tuple[int, str, str]:
        """One tracked subprocess: the tracker sees the PID for the round's lifetime,
        so a crashed orchestrator's resume can reap surviving workers (plan U6)."""
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env,
        )
        if self.tracker is not None:
            self.tracker.spawned(proc.pid, context)
        try:
            # No per-round timeout (R7): a round runs as long as the CLI does —
            # wall-clock is a terrible proxy for stuck, and long rounds are normal.
            stdout, stderr = proc.communicate()
        finally:
            if self.tracker is not None:
                self.tracker.exited(proc.pid)
        return proc.returncode, stdout, stderr


def _scrub_virtualenv(env: dict[str, str]) -> dict[str, str]:
    """Drop the orchestrator's own venv from the worker env (plan U6, R16).

    A worker inheriting ``VIRTUAL_ENV`` and its PATH entries resolves
    ``python``/``pytest`` to the parent checkout's venv from inside its
    worktree — the worktree's own venv (provisioned at creation) must win.
    """
    venv = env.pop("VIRTUAL_ENV", None)
    if venv and env.get("PATH"):
        prefix = venv.rstrip(os.sep) + os.sep
        env["PATH"] = os.pathsep.join(
            entry
            for entry in env["PATH"].split(os.pathsep)
            if entry and entry != venv and not entry.startswith(prefix)
        )
    return env


def _argv_context(extra: list[str]) -> str:
    """Session context for error messages without echoing whole prompts."""
    for flag in ("--session-id", "--resume"):
        if flag in extra:
            return f"{flag} {extra[extra.index(flag) + 1]}"
    return "new session"


_REPORT_RE = re.compile(r"<run-report\b[^>]*>(.*?)</run-report>", re.DOTALL)
_FENCE_RE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.DOTALL)


def parse_report(text: str, model_cls: type[M]) -> M:
    """Extract and validate the last ``<run-report>`` block of a final message."""
    matches = _REPORT_RE.findall(text)
    if not matches:
        raise ReportError("no <run-report> block in the final message")
    body = matches[-1].strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1)
    try:
        return model_cls.model_validate_json(body)
    except ValidationError as exc:
        raise ReportError(f"report body failed validation: {exc}") from exc


def nudge_until_report(
    runner: SessionRunner,
    first: RoundResult,
    model_cls: type[M],
    *,
    cwd: Path,
    max_nudges: int = DEFAULT_MAX_NUDGES,
) -> tuple[M, RoundResult]:
    """Parse the round's report, re-nudging the session a bounded number of times.

    Exactly ``max_nudges`` corrective resumes are attempted before the round fails
    (plan U5 test scenario; CoCoder's silent-exit lesson).
    """
    result = first
    for attempt in range(max_nudges + 1):
        try:
            return parse_report(result.text, model_cls), result
        except ReportError as exc:
            if attempt == max_nudges:
                raise ReportError(f"round failed after {max_nudges} re-nudges: {exc}") from exc
            result = runner.resume(
                session_id=result.session_id,
                prompt=_NUDGE_PROMPT.format(error=exc),
                cwd=cwd,
            )
    raise AssertionError("unreachable")
