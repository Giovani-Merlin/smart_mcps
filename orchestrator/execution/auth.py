"""Auth-refresh ladder (plan U4): a stale token pauses the run, not the account.

Before this existed a 401 was a bare ``SessionError`` — it interrupted the group
and, under ``on_group_failure=halt``, set ``RUN_HALTED`` for every group in the
run. The two incidents that motivated this were a refreshed token that could not
be *written* (fixed separately: ``confinement.py:428`` now grants
``~/.claude/.credentials.json`` a single-file read-write rule), not a login that
had lapsed — so the right response is to try to fix the credential before
bothering an operator, not to demote to a group-level failure or to prompt for
something the operator mostly cannot supply.

Three rungs, each cheaper than the next:

(a) Read ``expiresAt`` from ``~/.claude/.credentials.json``. No network call —
    both fields are epoch milliseconds, so staleness is a local comparison.
(b) If stale, the **unconfined orchestrator process** (this one — confinement is
    per-worker-session, see ``sessions.SessionRunner``) triggers a refresh and
    re-reads the file to confirm ``expiresAt`` advanced.
(c) If ``refreshTokenExpiresAt`` has also passed, or the refresh does not
    advance ``expiresAt``, or a live call still 401s: arm a pause and notify
    (``ratelimit.UsageLimitGate``, reused with a health ``probe`` instead of a
    fixed deadline — see that module), re-probing periodically and clearing
    itself once ``recover()`` reports healthy again.

This module implements rungs (a) and (b) as ``AuthLadder.recover()``. Rung (c)
is the caller's ``UsageLimitGate`` — constructed with ``probe=ladder.recover``
— so the exact same "arm, poll, self-release" machinery that already exists for
usage limits serves both.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

#: How the CLI reports a dead access token mid-round: a non-zero exit whose
#: envelope `result` (or stderr, see `_error_detail`) carries this wire text.
#: Distinct from `is_usage_limit` (`sessions.py`) — a 401 is not a rate limit
#: and must never be treated as one (retrying it changes nothing).
_AUTH_ERROR_RE = re.compile(r"401\b.*re-?authenticate", re.IGNORECASE | re.DOTALL)


def is_auth_error(detail: str) -> bool:
    """Whether a process-failure detail is an expired/invalid credential."""
    return bool(_AUTH_ERROR_RE.search(detail))


@dataclass(frozen=True)
class CredentialStatus:
    """The two expiry fields, epoch-ms, exactly as ``.credentials.json`` stores
    them. Either may be ``None`` when the field was absent or non-numeric."""

    expires_at_ms: int | None
    refresh_token_expires_at_ms: int | None

    def is_stale(self, *, now_ms: int) -> bool:
        return self.expires_at_ms is not None and self.expires_at_ms <= now_ms

    def refresh_token_expired(self, *, now_ms: int) -> bool:
        return (
            self.refresh_token_expires_at_ms is not None
            and self.refresh_token_expires_at_ms <= now_ms
        )


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_credential_status(path: Path) -> CredentialStatus | None:
    """Rung (a): the two expiry fields, with **no network call**.

    ``None`` means the file is absent, unreadable, or has no ``claudeAiOauth``
    object — a caller degrades to "cannot self-heal" (and falls to rung (c))
    rather than misreading absence as either staleness or health.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
    if not isinstance(oauth, dict):
        return None
    return CredentialStatus(
        expires_at_ms=_as_int(oauth.get("expiresAt")),
        refresh_token_expires_at_ms=_as_int(oauth.get("refreshTokenExpiresAt")),
    )


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _default_refresh(claude_bin: Sequence[str] = ("claude",)) -> bool:
    """Rung (b)'s default trigger: make the CLI validate its own credentials.

    Any print-mode call forces the CLI to check — and, if stale, refresh — its
    stored OAuth token before it can make the underlying request; what the
    prompt says and whether the call itself succeeds are both irrelevant, only
    that the process ran (unconfined, as the orchestrator is) and touched the
    credentials file. A non-zero exit or a spawn failure both count as "did not
    confirm" — the caller re-reads ``expiresAt`` either way rather than trusting
    this return value alone.
    """
    try:
        result = subprocess.run(
            [*claude_bin, "--print", "--output-format", "json", "ping"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class AuthLadder:
    """Rungs (a) and (b): read expiry, refresh in place, confirm it advanced.

    Deliberately ignorant of pausing — that is rung (c), on whatever gate the
    caller drives with ``recover`` as its ``probe`` (see module docstring and
    ``ratelimit.UsageLimitGate``). Keeping this class to "check, maybe refresh,
    report health" is what lets it serve both the reactive path (a live 401)
    and the proactive one (a routine check after any long pause, before the
    first retry) with the same method.
    """

    def __init__(
        self,
        *,
        credentials_path: Path | None = None,
        refresh: Callable[[], bool] | None = None,
        now_ms: Callable[[], int] = _now_ms,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.credentials_path = credentials_path or DEFAULT_CREDENTIALS_PATH
        self._refresh = refresh or _default_refresh
        self._now_ms = now_ms
        self._log = log

    def status(self) -> CredentialStatus | None:
        return read_credential_status(self.credentials_path)

    def is_stale(self) -> bool:
        """Rung (a) alone: no network call, no refresh attempt."""
        status = self.status()
        return status is not None and status.is_stale(now_ms=self._now_ms())

    def recover(self) -> bool:
        """Rungs (a)+(b): stale? try to refresh; confirm ``expiresAt`` advanced.

        Returns ``True`` when the credential is healthy — either it was never
        stale, or the refresh advanced ``expiresAt``. Returns ``False`` when
        rung (c) must take over: the refresh token has also expired, the
        refresh call did not succeed, or it ran but did not advance the
        expiry. Never raises — a broken refresh path is a reason to pause, not
        a reason to crash the run.
        """
        status = self.status()
        if status is None:
            return False
        now_ms = self._now_ms()
        if not status.is_stale(now_ms=now_ms):
            return True
        if status.refresh_token_expired(now_ms=now_ms):
            self._emit("auth: refresh token has also expired — cannot self-heal")
            return False
        before = status.expires_at_ms
        try:
            refreshed = self._refresh()
        except Exception:  # noqa: BLE001 — a broken refresh call is evidence, not a crash
            refreshed = False
        if not refreshed:
            self._emit("auth: token refresh call did not succeed")
            return False
        after = self.status()
        advanced = (
            after is not None
            and after.expires_at_ms is not None
            and (before is None or after.expires_at_ms > before)
        )
        if advanced:
            self._emit("auth: token refreshed, expiresAt advanced")
        else:
            self._emit("auth: refresh ran but expiresAt did not advance")
        return advanced

    def _emit(self, line: str) -> None:
        """Log sinks are evidence, never control flow (mirrors ratelimit._emit)."""
        if self._log is None:
            return
        try:
            self._log(line)
        except Exception:  # noqa: BLE001
            pass
