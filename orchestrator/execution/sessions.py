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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from orchestrator.execution.confinement import (
    build_policy,
    landlock_preexec,
    operator_memory_deny_patterns,
    warn_once,
)
from orchestrator.execution.worktrees import denied_git_tool_patterns
from orchestrator.execution.streaming import StreamError, StreamingProcess, TurnUsage

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
    """Cumulative usage for one session across rounds (breaker input, plan U5).

    The four token classes stay separate rather than being folded into one input
    total: a run whose spend is mostly ``cache_read`` is cheap and healthy, and
    collapsing it into ``total_input_tokens`` (as this did originally) makes an
    efficient run and an expensive one indistinguishable. ``last_context_tokens``
    is unchanged — the circuit breaker reads it and must not shift behaviour.
    """

    rounds: int = 0
    total_input_tokens: int = 0  # uncached input only
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    last_context_tokens: int = 0

    def add(self, usage: RoundUsage) -> None:
        self.rounds += 1
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.total_cache_read_tokens += usage.cache_read_input_tokens
        self.total_cache_creation_tokens += usage.cache_creation_input_tokens
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
        disallowed_tools: Sequence[str] | None = None,
        settings: str | None = None,
        confine: bool = True,
        safety_deny: bool = True,
    ):
        self._bin = [claude_bin] if isinstance(claude_bin, str) else list(claude_bin)
        self.model = model
        self.max_thinking_tokens = max_thinking_tokens
        self.thinking = thinking
        self.permission_mode = permission_mode
        self.allowed_tools = list(allowed_tools) if allowed_tools else None
        self.disallowed_tools = list(disallowed_tools) if disallowed_tools else None
        self.settings = settings
        self.confine = confine
        self.safety_deny = safety_deny
        self.transcript_root = transcript_root or Path.home() / ".claude" / "projects"
        self._env = _scrub_virtualenv({**os.environ, **(env or {})})
        self.tracker = tracker
        self._fork_lock = threading.Lock()
        self._usage: dict[str, SessionUsage] = {}
        self._confinement_warned = False

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

    def start_base(
        self,
        *,
        run_id: str,
        base_context: str,
        cwd: Path,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> RoundResult:
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
            on_turn=on_turn,
        )

    def start_fork(
        self,
        *,
        base_id: str,
        prompt: str,
        name: str,
        cwd: Path,
        session_id: str | None = None,
        json_schema: dict | None = None,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> RoundResult:
        """Fork the base session and run the first round in one blocking call.

        Serialized: the session store has no documented locking (plan Key Technical
        Decisions); forking is fast, so this never serializes the groups themselves.

        ``on_turn``, when given, is called once per assistant turn *while the
        round is still running* (plan U1) with that turn's usage and a ``send``
        callable bound to the live child process — the seam a context-ladder or
        other per-turn observer hangs off of. ``None`` (the default) preserves
        today's fully blocking round.

        ``session_id``, when given, lets the caller record the id in the manifest
        *before* this blocking call runs (plan U7): a crash mid-call would
        otherwise leave no manifest entry for a group interrupted during its
        very first round, so a later resume forks a brand new session instead
        of finding the one already recorded.
        """
        session_id = session_id or str(uuid.uuid4())
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
                on_turn=on_turn,
            )

    def resume(
        self,
        *,
        session_id: str,
        prompt: str,
        cwd: Path,
        json_schema: dict | None = None,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> RoundResult:
        """One warm round against an existing session. ``on_turn`` — see
        ``start_fork`` — lets a caller observe and speak into this round too."""
        return self._call(
            prompt,
            cwd=cwd,
            extra=["--resume", session_id],
            json_schema=json_schema,
            on_turn=on_turn,
        )

    def usage_of(self, session_id: str) -> SessionUsage:
        return self._usage.get(session_id, SessionUsage())

    def transcript_path(self, session_id: str) -> Path | None:
        """Locate the session transcript by UUID — exact regardless of cwd encoding."""
        matches = sorted(self.transcript_root.glob(f"*/{session_id}.jsonl"))
        return matches[0] if matches else None

    def effective_disallowed_tools(self) -> list[str]:
        """Configured deny rules plus the built-in safety rules.

        Composed here rather than at the call site on purpose. The previous
        arrangement left it to whoever built the runner, and the one production
        construction site (``cli.py``) passed nothing — so Landlock, the git deny
        list and ``--settings`` were all unreachable in a real run while their
        unit tests passed against directly-built runners. A boundary assembled by
        its consumers is a boundary that goes missing; this one assembles itself,
        and ``safety_deny=False`` is the single explicit opt-out.

        Order is configured-first so an operator rule keeps precedence, and the
        result is de-duplicated because the git patterns overlap what a config
        may already list.
        """
        rules = list(self.disallowed_tools or [])
        if self.safety_deny:
            rules += denied_git_tool_patterns()
            rules += operator_memory_deny_patterns(self._claude_home())
        seen: set[str] = set()
        return [r for r in rules if not (r in seen or seen.add(r))]

    def _claude_home(self) -> Path:
        """``~/.claude`` (or its test-double), derived from ``transcript_root``
        (``<claude_home>/projects``) so confinement and transcript discovery
        agree on the same root without a second constructor argument."""
        return self.transcript_root.parent

    def _capture(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                [*self._bin, *args], capture_output=True, text=True, env=self._env, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreflightError(f"cannot run {self._bin[0]} {args[0]}: {exc}") from exc
        return result.stdout + result.stderr

    def _call(
        self,
        prompt: str,
        *,
        cwd: Path,
        extra: list[str],
        json_schema: dict | None = None,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> RoundResult:
        argv = [
            *self._bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--input-format",
            "stream-json",
            *extra,
        ]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            argv += ["--allowedTools", ",".join(self.allowed_tools)]
        denied = self.effective_disallowed_tools()
        if denied:
            argv += ["--disallowedTools", ",".join(denied)]
        if self.settings:
            argv += ["--settings", self.settings]
        if self.model:
            argv += ["--model", self.model]
        if self.max_thinking_tokens is not None:
            argv += ["--max-thinking-tokens", str(self.max_thinking_tokens)]
        if self.thinking:
            argv += ["--thinking", self.thinking]
        if json_schema is not None:
            argv += ["--json-schema", json.dumps(json_schema)]
        context = _argv_context(extra)
        returncode, stdout, stderr = self._spawn(argv, cwd=cwd, context=context, on_turn=on_turn)
        if returncode != 0:
            raise SessionError(
                f"claude exited {returncode} ({context}): {_error_detail(stdout, stderr)}"
            )
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

    def _spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        context: str,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> tuple[int, str, str]:
        """One tracked subprocess, read incrementally rather than a single
        blocking ``communicate()`` (plan U1): the tracker still sees the PID for
        exactly the round's lifetime (spawned once, exited once — plan U6's
        crash-resume reaping depends on that), but the stream is now consumed
        turn by turn so ``on_turn`` fires while the round is still running, and
        that callback can ``send()`` a follow-up onto the same live process.

        Returns the same ``(returncode, stdout, stderr)`` shape ``_call`` always
        parsed: ``stdout`` is the terminal ``result`` stream event re-serialized
        as one JSON line, so every downstream envelope-parsing rule — including
        ``RoundUsage.from_envelope``'s ``iterations[-1]`` fallback — is unchanged.
        """
        preexec_fn = None
        if self.confine:
            policy = build_policy(worktree=cwd, claude_home=self._claude_home())
            preexec_fn, result = landlock_preexec(policy)
            if not result.applied:
                self._confinement_warned = warn_once(
                    result, already_warned=self._confinement_warned
                )
        stream = StreamingProcess(
            argv,
            cwd=cwd,
            env=self._env,
            tracker=self.tracker,
            context=context,
            preexec_fn=preexec_fn,
        )
        if on_turn is not None:
            stream.on_turn = lambda usage: on_turn(usage, stream.send)
        stream.start()
        try:
            outcome = stream.wait()
        except StreamError as exc:
            raise SessionError(f"claude stream failed ({context}): {exc}") from exc
        if outcome.envelope is None:
            if outcome.returncode == 0:
                raise SessionError(f"claude stream ended without a terminal result ({context})")
            return outcome.returncode, "", outcome.stderr
        return outcome.returncode, json.dumps(outcome.envelope), outcome.stderr


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


def _error_detail(stdout: str, stderr: str) -> str:
    """Best available error text for a non-zero exit (plan U4).

    A usage-limit failure exits non-zero with empty ``stderr`` — the useful text
    sits in ``stdout``'s JSON envelope instead. Try that first; fall back to
    ``stderr`` unchanged if ``stdout`` doesn't parse or has no usable ``result``.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and envelope.get("result"):
        return str(envelope["result"])[:500]
    return stderr.strip()[:500]


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
