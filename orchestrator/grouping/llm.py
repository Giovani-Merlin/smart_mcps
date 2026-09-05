"""LLM call seam: `claude -p --json-schema` with bounded validation retries.

Every LLM touchpoint in the grouping engine goes through `call_llm_json` so tests
can stub the runner and never spend tokens (plan R24). Invalid output retries with
a corrective nudge, capped; persistent failure aborts with the raw output saved
for inspection (plan U4).
"""

from __future__ import annotations

import datetime
import json
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from orchestrator.execution.ratelimit import UsageLimitGate
from orchestrator.execution.sessions import is_usage_limit

T = TypeVar("T")

# (prompt, json_schema) → raw model text. The production runner shells the CLI;
# tests inject a canned function.
JsonRunner = Callable[[str, dict], str]


def _envelope_model(envelope: dict) -> str | None:
    """Which model answered, across the two shapes the envelope uses.

    The live CLI carries no top-level ``model`` key at all — the name is a key of
    ``modelUsage``. A1's mocked-subprocess tests supplied ``model`` directly, so
    every record written by the first real ``group`` run had a null model until
    this fallback existed. A multi-model envelope keeps the largest contributor,
    which is the one worth attributing a grouping decision to.
    """
    named = envelope.get("model")
    if named:
        return str(named)
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict) or not model_usage:
        return None

    def _output(entry: object) -> int:
        return int(entry.get("outputTokens", 0) or 0) if isinstance(entry, dict) else 0

    return max(model_usage, key=lambda name: _output(model_usage[name]))


@dataclass(frozen=True)
class LlmCallMeta:
    """CLI envelope metadata for one grouping call.

    Attribute names follow the OpenTelemetry GenAI semantic conventions where one
    exists, so the artifact can later be replayed into an OTel backend without a
    reshape. No OTel dependency is taken.
    """

    session_id: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    envelope: dict | None = field(default=None, repr=False)

    @classmethod
    def from_envelope(cls, envelope: dict, session_id: str | None, duration_ms: int) -> LlmCallMeta:
        usage = envelope.get("usage") or {}
        return cls(
            session_id=envelope.get("session_id") or session_id,
            model=_envelope_model(envelope),
            duration_ms=duration_ms,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
        )


class LlmCallResult(str):
    """Model text that also carries the envelope metadata that produced it.

    A ``str`` subclass on purpose: ``JsonRunner`` stays
    ``Callable[[str, dict], str]``, so every existing stub runner in the test
    suite keeps working untouched and any caller that just wants the text is
    unaffected. Callers that want provenance check for ``.meta``.
    """

    meta: LlmCallMeta

    def __new__(cls, text: str, meta: LlmCallMeta) -> LlmCallResult:
        obj = super().__new__(cls, text)
        obj.meta = meta
        return obj


def call_meta(raw: str) -> LlmCallMeta | None:
    """Metadata of a runner result, or None for a plain-``str`` (stub) runner."""
    return getattr(raw, "meta", None)


class LlmCallRecorder(Protocol):
    """Observes grouping LLM calls. Inert by contract — nothing it records is
    ever read back into a grouping decision (same contract as ``TraceRecorder``)."""

    def record_call(
        self,
        *,
        stage: str,
        attempt: int,
        prompt: str,
        schema: dict,
        raw: str,
        meta: LlmCallMeta | None,
        error: str | None,
        subject: dict | None = None,
    ) -> None: ...


DEFAULT_MAX_RETRIES = 2

# Thinking policy for the orchestrator's *own* reasoning calls (mapper/speccer),
# as distinct from worker sessions. These are the decisions a whole run is built
# on — a bad partition costs every group downstream — and there are only a handful
# of them per run, so they get the larger budget: `high` (10000) against the
# workers' `medium` (4000) in SessionConfig. `adaptive` on both, so a call that
# needs no reasoning pays for none (measured: adaptive 62 output tokens vs enabled
# 140 vs disabled 253 on one sonnet probe).
#
# NB: `--max-thinking-tokens` is hidden from `claude --help`. It works, but never
# add it to a preflight flag check.
ORCHESTRATOR_MAX_THINKING_TOKENS = 10000
ORCHESTRATOR_THINKING = "adaptive"


class LlmError(Exception):
    """The model never produced output that passed validation."""


class LlmProcessError(LlmError):
    """The ``claude -p`` process itself died — a non-zero exit, not bad output.

    Same envelope-failure class as ``SessionError`` on the session path: the CLI or
    API went away under the caller, so the work was never attempted and re-running
    it is free. Usage limits land here, with an empty stderr and exit 1. Kept
    distinct from its parent so the scheduler can classify it ``INTERRUPTED``
    (resumable) rather than ``FAILED`` (terminal) — a validation exhaustion is the
    model failing and stays terminal.
    """


class LlmUsageLimit(LlmProcessError):
    """The one-shot ``claude -p`` path hit the account's usage limit.

    Until this existed, this path could not tell a limit from any other process
    death: ``claude_json_runner`` raised a bare ``LlmProcessError`` and
    ``is_usage_limit`` — sitting one module away in ``execution.sessions`` — was
    never called on it at all. So the grouping stage and every run-time spec
    rewrite treated a reset-in-40-minutes exactly like a segfault.

    A subclass rather than a new branch: the scheduler already classifies
    ``LlmProcessError`` as INTERRUPTED, and that stays true when the retry gives
    up. ``detail`` carries the limit prose unwrapped, for ``parse_reset_at``.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail or message


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


# Set False the first time the installed CLI rejects `--session-id` alongside
# `--json-schema`. The flag is what gives a grouping call a locatable transcript
# on disk, but it is strictly an observability nicety: if the pairing is
# unsupported, grouping must still work exactly as it did before.
_SESSION_ID_SUPPORTED = True


def _rejects_session_id(stderr: str) -> bool:
    lowered = stderr.lower()
    return "session-id" in lowered and ("unknown" in lowered or "unrecognized" in lowered)


def claude_json_runner(prompt: str, schema: dict, *, model: str | None = None) -> LlmCallResult:
    """Production runner: one blocking `claude -p` call with a JSON schema.

    Grouping-stage calls are stateless one-shots (no session to *resume*), but a
    session id is still minted and passed so the call leaves a transcript jsonl
    under ``~/.claude/projects/``. Without it the orchestrator's own reasoning —
    the decisions a whole run is built on — is unrecoverable after the fact.

    ``model`` (plan U17) is the speccer/grouper model — bound via
    ``functools.partial`` by the caller (``cli.py``) rather than defaulted here,
    so this function stays a plain two-arg ``JsonRunner`` where nothing binds it.
    """
    global _SESSION_ID_SUPPORTED
    session_id = str(uuid.uuid4())
    base = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--max-thinking-tokens",
        str(ORCHESTRATOR_MAX_THINKING_TOKENS),
        "--thinking",
        ORCHESTRATOR_THINKING,
    ]
    if model:
        base += ["--model", model]
    started = time.monotonic()
    argv = [*base, "--session-id", session_id] if _SESSION_ID_SUPPORTED else base
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0 and _SESSION_ID_SUPPORTED and _rejects_session_id(result.stderr):
        _SESSION_ID_SUPPORTED = False
        session_id = None  # type: ignore[assignment]
        result = subprocess.run(base, capture_output=True, text=True)
    duration_ms = int((time.monotonic() - started) * 1000)
    if result.returncode != 0:
        # Classify *before* raising. The session path has always done this; this
        # path never did, which is the standing gap that made a usage limit
        # indistinguishable from a crashed CLI for `group` and for run-time spec
        # rewrites alike.
        detail = _error_detail(result.stdout, result.stderr)
        message = f"claude -p failed ({result.returncode}): {detail}"
        if is_usage_limit(detail):
            raise LlmUsageLimit(message, detail)
        raise LlmProcessError(message)
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LlmError(f"claude -p emitted a non-JSON envelope: {exc}") from exc
    if not isinstance(envelope, dict) or "result" not in envelope:
        raise LlmError("claude -p envelope is missing the 'result' field")
    meta = LlmCallMeta.from_envelope(envelope, session_id, duration_ms)
    return LlmCallResult(str(envelope["result"]), meta)


def with_usage_limit_retry(runner: JsonRunner, gate: UsageLimitGate | None) -> JsonRunner:
    """Wrap a one-shot runner so a usage limit pauses and re-sends the same call.

    The session path gets this inside ``SessionRunner._call``; this is the same
    mechanism for the stateless path, which the ``group`` command and every
    run-time spec rewrite go through. ``gate=None`` (or a disabled gate) returns
    the runner untouched, so nothing changes for a caller that has not opted in.

    A one-shot call is the easy case: nothing was resumed and no transcript was
    appended to, so a retry after the reset is byte-identical to the call that
    was refused.
    """
    if gate is None or not gate.enabled:
        return runner

    def wrapped(prompt: str, schema: dict) -> str:
        for attempt in range(1, gate.max_attempts + 1):
            try:
                return runner(prompt, schema)
            except LlmUsageLimit as exc:
                if attempt == gate.max_attempts or not gate.pause(exc.detail):
                    raise  # exhausted, or the operator cancelled the run
        raise AssertionError("unreachable")

    return wrapped


def transcript_path_for(session_id: str, transcript_root: Path | None = None) -> Path | None:
    """Locate a grouping call's transcript by UUID, mirroring ``SessionRunner``."""
    root = transcript_root or Path.home() / ".claude" / "projects"
    matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def call_llm_json(
    runner: JsonRunner,
    prompt: str,
    schema: dict,
    validate: Callable[[dict], T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    failure_dir: Path | None = None,
    recorder: LlmCallRecorder | None = None,
    subject: dict | None = None,
) -> T:
    """Run the LLM, parse JSON, validate; retry with a corrective nudge, capped.

    ``subject`` is what the call is ABOUT — for the rewrite speccer, which group
    is being rewritten and why. It is recorded alongside the call so a consumer
    can attribute it without parsing the prompt; nothing reads it back."""
    stage = schema.get("title", "llm")
    attempt_prompt = prompt
    last_raw = ""
    last_error: Exception | None = None
    for attempt in range(1 + max_retries):
        last_raw = runner(attempt_prompt, schema)
        try:
            payload = json.loads(last_raw)
            validated = validate(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            last_error = exc
            if recorder is not None:
                recorder.record_call(
                    stage=stage,
                    attempt=attempt,
                    prompt=attempt_prompt,
                    schema=schema,
                    raw=last_raw,
                    meta=call_meta(last_raw),
                    error=str(exc),
                    subject=subject,
                )
            attempt_prompt = (
                f"{prompt}\n\nYour previous output failed validation: {exc}.\n"
                "Return ONLY valid JSON matching the schema — no prose, no fences."
            )
        else:
            if recorder is not None:
                recorder.record_call(
                    stage=stage,
                    attempt=attempt,
                    prompt=attempt_prompt,
                    schema=schema,
                    raw=last_raw,
                    meta=call_meta(last_raw),
                    error=None,
                    subject=subject,
                )
            return validated
    saved = _save_failure(failure_dir, stage, last_raw)
    location = f"; raw output saved to {saved}" if saved else ""
    raise LlmError(
        f"{stage} output failed validation after {1 + max_retries} attempts: {last_error}{location}"
    )


def _save_failure(failure_dir: Path | None, stage: str, raw: str) -> Path | None:
    if failure_dir is None:
        return None
    failure_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
    path = failure_dir / f"{stage}-{stamp}.txt"
    path.write_text(raw)
    return path
