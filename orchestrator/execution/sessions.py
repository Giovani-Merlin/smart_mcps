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
    default_cache_root,
    landlock_preexec,
    operator_memory_deny_patterns,
    system_write_paths,
    warn_once,
    worker_cache_dirs,
    worker_cache_env,
)
from orchestrator.execution.worktrees import denied_git_tool_patterns
from orchestrator.execution.auth import AuthLadder, is_auth_error
from orchestrator.execution.ratelimit import UsageLimitGate
from orchestrator.execution.streaming import StreamError, StreamingProcess, TurnUsage

REQUIRED_CLI_FLAGS = (
    "--print",
    "--output-format",
    "--verbose",
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


class UsageLimit(SessionError):
    """The account's usage limit was reached — the round never ran.

    A distinct type because it is the one envelope failure where *retrying with a
    different session changes nothing*. The re-entry path catches ``SessionError``
    and falls back to forking a fresh generation, which is the right response to a
    session that has become unreachable; against a usage limit it fails
    identically and spends a generation of the breaker's budget on a call that
    could not have succeeded. Re-raised out of the fallback instead, so the group
    lands INTERRUPTED (resumable, since it is still a ``SessionError``) with its
    generation intact.

    ``detail`` carries the limit prose *unwrapped* — no ``claude exited 1 (…)``
    prefix — because that is what ``ratelimit.parse_reset_at`` reads the reset
    time out of, and re-extracting it from a formatted message would be a second
    parser of the same string.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


class AuthExpired(SessionError):
    """The account's OAuth token was rejected — the round never ran (plan U4).

    A distinct type for the same reason ``UsageLimit`` is: the response is not
    "fork a fresh generation", it is "fix the credential, then replay this
    exact call". ``detail`` carries the wire text unwrapped, same convention as
    ``UsageLimit.detail``.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


#: How a usage limit announces itself on the wire. It exits non-zero with an
#: *empty* stderr and the text in the stdout envelope's `result`, which is why
#: this is matched against `_error_detail`'s output rather than stderr.
#:
#: The wordings differ by limit *type* and none of them are documented, so this
#: list is evidence rather than guesswork — see `tests/test_sessions.py` for each
#: string verbatim and where it was observed. The `hit your <x> limit` form was
#: caught by the live tier on 2026-08-13; the older `usage limit reached|<epoch>`
#: form alone did not match it, which would have sent a limited run down the
#: pointless-fork path P6 exists to prevent.
_USAGE_LIMIT_RE = re.compile(
    r"usage limit reached|hit your \w+ limit|"
    r"(?:session|usage|weekly|monthly) limit(?:\b|·)|limit · resets|"
    r"rate[ _-]?limit(?:ed|:)?|too many requests|"
    r"limit will reset|429\b|overloaded_error",
    re.IGNORECASE,
)


def is_usage_limit(detail: str) -> bool:
    """Whether a process-failure detail is an account/rate limit rather than a
    broken call. Text-matched because the CLI reports it as prose in the
    envelope's ``result``, with no code or field to key off."""
    return bool(_USAGE_LIMIT_RE.search(detail))


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


@dataclass(frozen=True)
class RoundSpend:
    """What one round actually cost, summed across every turn (plan U9).

    Unlike ``RoundUsage.context_tokens`` — which deliberately reads only the
    last turn, because occupancy is what the next round resumes into — spend is
    what every turn billed, and the envelope's *top-level* ``usage`` already is
    that all-turns sum (see ``RoundUsage.from_envelope``'s docstring). No
    iteration walk is needed, and this degrades correctly on older CLIs that
    emit no ``iterations`` at all, where the top level is the whole round.

    Per-request billing is independent, and ``cache_read_input_tokens`` on turn
    *n* reports the whole re-read prefix — including what turn *n-1* wrote —
    billed at 0.1x. Summing across turns is therefore correct and is not double
    counting.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    #: Turn 1's own cache read: context this round inherited rather than
    #: created, and cannot shrink. Reported separately from the round's total
    #: cache read so the two are never conflated.
    inherited_cache_read_tokens: int = 0

    @classmethod
    def from_envelope(cls, envelope: dict) -> RoundSpend:
        usage = envelope.get("usage") or {}
        iterations = usage.get("iterations") or []
        first_turn = iterations[0] if isinstance(iterations, list) and iterations else usage
        return cls(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            inherited_cache_read_tokens=int(first_turn.get("cache_read_input_tokens", 0) or 0),
        )


@dataclass
class SessionUsage:
    """Cumulative usage for one session across rounds (breaker input, plan U5).

    The four token classes stay separate rather than being folded into one input
    total: a run whose spend is mostly ``cache_read`` is cheap and healthy, and
    collapsing it into ``total_input_tokens`` (as this did originally) makes an
    efficient run and an expensive one indistinguishable. ``last_context_tokens``
    is unchanged — the circuit breaker reads it and must not shift behaviour.

    Spend and occupancy are two quantities (plan U9): the cumulative counters
    below are built from ``RoundSpend`` (every turn of every round), while
    ``last_context_tokens`` is built from ``RoundUsage`` (the latest turn only).
    A 190-turn round must contribute all 190 turns' worth of spend but only its
    last turn's occupancy.
    """

    rounds: int = 0
    total_input_tokens: int = 0  # uncached input only
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    #: Sum of every round's turn-1 inherited cache read — its own figure,
    #: distinct from total_cache_read_tokens, because it is context the session
    #: did not create and cannot shrink.
    total_inherited_cache_read_tokens: int = 0
    last_context_tokens: int = 0

    def add(self, usage: RoundUsage, spend: RoundSpend) -> None:
        self.rounds += 1
        self.total_input_tokens += spend.input_tokens
        self.total_output_tokens += spend.output_tokens
        self.total_cache_read_tokens += spend.cache_read_input_tokens
        self.total_cache_creation_tokens += spend.cache_creation_input_tokens
        self.total_inherited_cache_read_tokens += spend.inherited_cache_read_tokens
        self.last_context_tokens = usage.context_tokens


@dataclass(frozen=True)
class RoundResult:
    session_id: str
    text: str
    usage: RoundUsage
    envelope: dict = field(repr=False)
    #: Refusal/errno text the harness returned to the worker during this round
    #: (plan P2). Empty for every stub and for any round where nothing matched;
    #: used only to corroborate a `permission_denied` report's own account.
    deny_signals: list[str] = field(default_factory=list, repr=False)


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
        cache_root: Path | None = None,
        extra_write_paths: Sequence[Path] | None = None,
        gate: UsageLimitGate | None = None,
        auth_ladder: AuthLadder | None = None,
        auth_gate: UsageLimitGate | None = None,
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
        # The one place both halves of the cache mechanism are computed, from one
        # root: the environment the worker gets, and the paths Landlock allows.
        # Resolved from *this* process's environ — a worker's own XDG_CACHE_HOME
        # is about to point inside this root, so re-deriving it there would nest
        # a root inside itself.
        self.cache_root = Path(cache_root) if cache_root is not None else default_cache_root()
        self._cache_dirs = worker_cache_dirs(self.cache_root)
        self.extra_write_paths = [Path(p) for p in extra_write_paths or []]
        cache_env = worker_cache_env(self.cache_root, base=dict(os.environ))
        # HOME is never rewritten: `_claude_home`, transcript discovery,
        # `probe_claude_runtime_dirs` and the project-slug rule all key off it,
        # and a worker with a different HOME would lose its own session store.
        #
        # Precedence: cache env beats what the orchestrator inherited (that is the
        # whole point), and an explicit `env=` still beats both — callers pass it
        # to pin a specific variable and must keep winning.
        self._env = _scrub_virtualenv({**os.environ, **cache_env, **(env or {})})
        self.tracker = tracker
        # One gate per run, shared by every group (see `_call`'s retry loop).
        # ``None`` restores the pre-auto-resume behaviour exactly: a usage limit
        # raises straight out of the call.
        self.gate = gate
        # Plan U4: rungs (a)+(b) — read expiry, refresh in place — and rung (c)
        # — arm-and-poll — of the auth ladder. Both default to ``None``, which
        # restores exactly today's behaviour: a 401 raises ``AuthExpired``
        # straight out of ``_call_with_retry`` (still a ``SessionError``, so the
        # group lands INTERRUPTED and is resumable, same as an unconfigured
        # usage-limit gate).
        self.auth_ladder = auth_ladder
        self.auth_gate = auth_gate
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
            # `--print` only. The prompt travels over stdin as a stream-json
            # message (see `StreamingProcess.start`) — under `--input-format
            # stream-json` a prompt on argv is silently ignored.
            "--print",
            "--output-format",
            "stream-json",
            # The CLI *rejects* `--print --output-format=stream-json` without
            # `--verbose` ("requires --verbose", exit 1) — it is a hard
            # precondition of the streaming channel, not a logging preference.
            # Omitting it made every real worker launch fail at spawn while the
            # e2e stub CLI, which does not enforce the pairing, stayed green.
            "--verbose",
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
        return self._call_with_retry(argv, prompt=prompt, cwd=cwd, context=context, on_turn=on_turn)

    def _call_with_retry(
        self,
        argv: list[str],
        *,
        prompt: str,
        cwd: Path,
        context: str,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> RoundResult:
        """Spawn, and on a usage limit wait for the reset and re-send the same argv.

        This is the right level for the retry for three reasons. It covers every
        session path at once — base, coder fork, warm resume, reviewer — with one
        edit. It sits *below* where generations and spec rewrites are counted, so
        a pause costs no generation of the breaker's budget (what the P6 fix at
        ``review.py`` reached for and could only half-achieve). And the failed
        call never reached the model, so re-sending the same prompt to the same
        session is a replay, not a second attempt.

        One honest caveat: a limit can land *mid-round*, after some turns already
        committed to the transcript. A retried ``--resume`` then re-enters a
        session holding partial work — precisely what a manual ``resume`` does
        today, minus the human wait. A retried ``--fork-session`` discards that
        partial. Neither is new behaviour; both are now automatic.

        The retry cannot replay ``--session-id`` verbatim. The CLI *registers*
        the id before the call dies on the limit, so re-sending it fails with
        "Session ID <uuid> is already in use" — in the same second, after a wait
        of hours. A live run lost 3h42m to exactly that (run r20260812-202855,
        group g4, 2026-08-14) and every ``--session-id`` retry before this fix
        was guaranteed to fail that way. So each attempt mints a fresh id; the
        id the CLI actually used comes back on ``RoundResult.session_id``, which
        is read from the envelope, and callers that pre-registered the old id
        must reconcile against it (see ``ReviewLoop`` and plan U7).

        ``--resume`` needs no such treatment: resuming an existing session twice
        is legal, and the id must not change or the retry would address the
        wrong session.

        Exhausting ``max_attempts`` re-raises ``UsageLimit``, so today's
        INTERRUPTED path applies unchanged when the mechanism gives up.

        A 401 (``AuthExpired``) is handled here too, on its own budget,
        independent of the usage-limit one above (plan U4). The ladder's
        rungs (a)+(b) run first and unconditionally — a stale token gets one
        no-pause refresh attempt before anything is armed — and only a
        refresh that fails or cannot be attempted (the refresh token has also
        expired) falls to rung (c): ``auth_gate.pause`` arms a pause whose
        ``probe`` is the same ``recover`` call, so the pause self-releases the
        moment the credential is healthy again rather than on a fixed
        deadline. Either way the call is replayed under a fresh session id,
        same as a usage-limit retry — a 401 also lands after the CLI has
        registered the id.
        """
        gate = self.gate
        usage_budget = gate.max_attempts if gate is not None and gate.enabled else 1
        auth_gate = self.auth_gate
        auth_budget = auth_gate.max_attempts if auth_gate is not None and auth_gate.enabled else 1
        usage_attempt = 0
        auth_attempt = 0
        while True:
            try:
                return self._invoke(argv, prompt=prompt, cwd=cwd, context=context, on_turn=on_turn)
            except UsageLimit as exc:
                usage_attempt += 1
                if gate is None or not gate.enabled or usage_attempt >= usage_budget:
                    raise
                # A cancelled pause means the operator stopped the run: re-raise
                # rather than retry into a limit that is still active.
                if not gate.pause(exc.detail):
                    raise
                # Plan U4 rung (a): a long pause is exactly the moment a token
                # can have gone stale underneath the run. Best-effort — a
                # refresh here just means the next 401, if any, never happens.
                if self.auth_ladder is not None:
                    self.auth_ladder.recover()
                argv = _with_fresh_session_id(argv)
                context = _argv_context(argv)
            except AuthExpired as exc:
                if self.auth_ladder is not None and self.auth_ladder.recover():
                    # Rungs (a)+(b) fixed it — no pause needed.
                    argv = _with_fresh_session_id(argv)
                    context = _argv_context(argv)
                    continue
                auth_attempt += 1
                if auth_gate is None or not auth_gate.enabled or auth_attempt >= auth_budget:
                    raise
                if not auth_gate.pause(exc.detail):
                    raise
                argv = _with_fresh_session_id(argv)
                context = _argv_context(argv)

    def _invoke(
        self,
        argv: list[str],
        *,
        prompt: str,
        cwd: Path,
        context: str,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
    ) -> RoundResult:
        returncode, stdout, stderr, deny_signals = self._spawn(
            argv, cwd=cwd, context=context, on_turn=on_turn, prompt=prompt
        )
        if returncode != 0:
            detail = _error_detail(stdout, stderr)
            message = f"claude exited {returncode} ({context}): {detail}"
            # Typed, not just worded: `_reenter` needs to tell "this session is
            # unreachable, fork a new one" from "the account is out of budget,
            # forking changes nothing".
            if is_usage_limit(detail):
                raise UsageLimit(message, detail)
            if is_auth_error(detail):
                raise AuthExpired(message, detail)
            raise SessionError(message)
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
        spend = RoundSpend.from_envelope(envelope)
        self._usage.setdefault(session_id, SessionUsage()).add(usage, spend)
        return RoundResult(
            session_id=session_id,
            text=str(envelope["result"]),
            usage=usage,
            envelope=envelope,
            deny_signals=deny_signals,
        )

    def _spawn(
        self,
        argv: list[str],
        *,
        cwd: Path,
        context: str,
        on_turn: Callable[[TurnUsage, Callable[[str], None]], None] | None = None,
        prompt: str | None = None,
    ) -> tuple[int, str, str, list[str]]:
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
            # Re-asserted per spawn, not created once at startup: Landlock rules
            # address *existing* paths, so a cache dir deleted mid-run would drop
            # silently out of the ruleset and reproduce the original
            # cache-`EACCES` defect on the next round.
            worker_cache_dirs(self.cache_root, create=True)
            policy = build_policy(
                worktree=cwd,
                claude_home=self._claude_home(),
                system_paths=[*system_write_paths(), *self.extra_write_paths],
                cache_dirs=self._cache_dirs,
            )
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
        stream.start(prompt=prompt)
        try:
            outcome = stream.wait()
        except StreamError as exc:
            raise SessionError(f"claude stream failed ({context}): {exc}") from exc
        if outcome.envelope is None:
            if outcome.returncode == 0:
                raise SessionError(f"claude stream ended without a terminal result ({context})")
            return outcome.returncode, "", outcome.stderr, outcome.deny_signals
        return (
            outcome.returncode,
            json.dumps(outcome.envelope),
            outcome.stderr,
            outcome.deny_signals,
        )


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


def _with_fresh_session_id(argv: list[str]) -> list[str]:
    """A copy of ``argv`` with a new ``--session-id``, for a usage-limit retry.

    The id the first attempt carried is spent even though that attempt never
    reached the model: the CLI registers it, then fails on the limit. Only the
    explicit ``--session-id`` value is replaced — ``--resume`` ids address a
    session that must stay the same, and argv without ``--session-id`` (a plain
    new session) is returned unchanged.
    """
    if "--session-id" not in argv:
        return argv
    fresh = list(argv)
    fresh[fresh.index("--session-id") + 1] = str(uuid.uuid4())
    return fresh


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
