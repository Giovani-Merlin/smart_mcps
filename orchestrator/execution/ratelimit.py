"""Pause-in-place for account usage limits: read the deadline, wait, retry.

Until this existed, a usage limit ended a run. ``SessionRunner`` classified it
(``UsageLimit``), the scheduler collapsed it to ``INTERRUPTED``, the CLI printed
``resume with: …`` and exited 2 — and the reset time the classifier's own regex
had just matched was thrown away. One recorded run took ~2.7 days, "mostly
rate-limit-reset waits" (docs/handoffs/2026-07-25-…-followups.md).

Two pieces live here, both deliberately free of subprocesses and files so they
unit-test on a fake clock:

- ``parse_reset_at`` — the deadline, out of the limit prose. Every accepted
  wording is one that was *observed*; ``None`` is a supported answer and means
  "poll instead", never "guess".
- ``UsageLimitGate`` — one gate per run, shared by every group, that arms from
  the prose and blocks until the limit releases.

Why threading and not asyncio: ``SessionRunner._call`` runs inside
``asyncio.to_thread``, so the thing that has to block is an OS thread. An
``asyncio`` primitive here would be blocking the wrong thing (and would have to
be reached from a thread that has no running loop).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestrator.config import UsageLimitConfig

#: How often an armed gate says it is still waiting. Slow on purpose: this line
#: goes to `run.log`, which a human reads (and the Observatory streams), and the
#: only question it answers is "is this paused or wedged?".
COUNTDOWN_INTERVAL_S = 300.0

#: The longest a waiter sleeps before re-checking. It bounds how coarse
#: cancellation is, and cancellation has to be prompt for a reason that is not
#: obvious: ``SessionRunner._call`` runs in an ``asyncio.to_thread`` pool thread,
#: and ``asyncio.run`` joins that pool on the way out. A waiter sleeping for five
#: hours would therefore hold the whole process open after Ctrl-C — turning a
#: paused run into an unkillable one, which is worse than the problem this module
#: solves. Five seconds is imperceptible against a reset measured in hours.
MAX_SLEEP_SLICE_S = 5.0

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

#: `usage limit reached|1700000000` — the oldest observed form, an epoch after a
#: pipe. Accepts seconds or milliseconds; nothing else on the line matters.
_EPOCH_RE = re.compile(r"\|\s*(\d{9,13})\b")

#: `… limit · resets 1pm (Europe/Berlin)`, `resets 1:30pm`, `resets 13:00`,
#: `resets Monday 9am`. The day word is optional and, when present, makes this a
#: weekly-style deadline rather than a same/next-day one.
_RESETS_RE = re.compile(
    r"reset(?:s|ting)?\s+(?:at\s+)?"
    r"(?:(?P<day>" + "|".join(_WEEKDAYS) + r")\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>[ap]\.?m\.?)?",
    re.IGNORECASE,
)

#: The `(Europe/Berlin)` suffix. Only an IANA-shaped name is accepted — an
#: abbreviation like `(CEST)` is ambiguous and `zoneinfo` cannot resolve it, so
#: it degrades to local time rather than to a wrong instant.
_ZONE_RE = re.compile(r"\((?P<zone>[A-Za-z]+(?:/[A-Za-z_+\-0-9]+)+)\)")


def parse_reset_at(detail: str, *, now: datetime) -> datetime | None:
    """The instant a usage limit says it releases, or ``None`` if it doesn't say.

    ``now`` must be timezone-aware; the result is aware too, in the zone the
    prose named (or ``now``'s zone when it named none).

    ``None`` is not a failure. The gate falls back to polling on it, which is
    strictly better than inventing a deadline: a guess that lands early burns a
    retry, and one that lands late holds the run past the reset.
    """
    epoch = _EPOCH_RE.search(detail)
    if epoch is not None:
        raw = int(epoch.group(1))
        # 13 digits is milliseconds; anything shorter is seconds. Both have been
        # seen in the wild across CLI versions.
        seconds = raw / 1000 if raw > 10**11 else raw
        return datetime.fromtimestamp(seconds, tz=UTC).astimezone(now.tzinfo)

    match = _RESETS_RE.search(detail)
    if match is None:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").replace(".", "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    tz = _zone_of(detail) or now.tzinfo
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    day = match.group("day")
    if day is not None:
        # A day-qualified wording (the weekly limit's shape; the exact prose is
        # still unconfirmed) means the next occurrence of that weekday — today
        # only if the time has not already passed.
        ahead = (_WEEKDAYS[day.lower()] - candidate.weekday()) % 7
        candidate += timedelta(days=ahead)
        if candidate <= local_now:
            candidate += timedelta(days=7)
        return candidate

    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def _zone_of(detail: str) -> ZoneInfo | None:
    match = _ZONE_RE.search(detail)
    if match is None:
        return None
    try:
        return ZoneInfo(match.group("zone"))
    except (ZoneInfoNotFoundError, ValueError):
        return None


@dataclass(frozen=True)
class UsageLimitState:
    """What the gate is doing, as the UI and ``usage-limit.json`` see it.

    ``released_at`` set is the terminal shape: the pause is over and this record
    is history, which is what lets the banner clear itself without a second file.
    """

    armed_at: datetime
    detail: str
    attempt: int = 1
    reset_at: datetime | None = None
    #: When the gate will actually stop waiting — ``reset_at`` plus the skew, or
    #: the polling fallback. Distinct from ``reset_at`` because a reader should
    #: see the limit's own claim, not our padding of it.
    wake_at: datetime | None = None
    released_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "armed_at": self.armed_at.isoformat(),
            "detail": self.detail,
            "attempt": self.attempt,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "wake_at": self.wake_at.isoformat() if self.wake_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
        }


class PhaseOverlay(Protocol):
    """What the gate needs of a heartbeat: shadow the phase, then give it back."""

    def push_phase(self, phase: str) -> None: ...

    def pop_phase(self) -> None: ...


def _default_now() -> datetime:
    return datetime.now(UTC).astimezone()


class UsageLimitGate:
    """One pause, shared by every caller that hits the same account limit.

    ``pause`` arms the gate from the limit prose and blocks until it releases.
    The first caller arms and logs; concurrent callers *join* that pause rather
    than each sleeping their own copy of it. A later hit only ever **extends**
    the deadline — a second limit message with an earlier (or unparseable) reset
    must not shorten a wait that a clearer message already justified.

    There is no probe protocol and no staggered wake: on release everyone
    retries, and a retry that hits the limit again simply re-arms the gate. At
    the default ``concurrency = 1`` there is at most one waiter anyway, and a
    thundering herd of two is cheaper than a handshake to avoid it.

    ``now`` / ``sleep`` are injected so tests drive a fake clock. The wait is a
    sleep loop rather than ``Condition.wait(timeout)`` for exactly that reason —
    a fake clock cannot make a real condition variable time out. The condition
    is still the mutex, and is notified on every state change so a waiter
    re-reads a deadline that moved.
    """

    def __init__(
        self,
        config: UsageLimitConfig | None = None,
        *,
        now: Callable[[], datetime] = _default_now,
        sleep: Callable[[float], None] | None = None,
        log: Callable[[str], None] | None = None,
        on_change: Callable[[UsageLimitState], None] | None = None,
        probe: Callable[[], bool] | None = None,
        label: str = "usage limit",
    ) -> None:
        self.config = config or UsageLimitConfig()
        self._now = now
        if sleep is None:
            import time as _time

            sleep = _time.sleep
        self._sleep = sleep
        self._log = log
        self._on_change = on_change
        # Plan U4 (auth-refresh-ladder): when set, the wake deadline is not
        # itself a release condition — it is when to *ask* whether the pause
        # should end. ``None`` restores the exact pre-U4 behaviour (a usage
        # limit releases unconditionally once its deadline passes), which is
        # why every usage-limit test still passes unmodified: this gate class
        # is reused as-is for the auth pause (probe=AuthLadder.recover),
        # rather than duplicating the arm/poll/release machinery.
        self._probe = probe
        self._label = label
        self._cond = threading.Condition()
        self._state: UsageLimitState | None = None
        self._wake_at: datetime | None = None
        self._epoch = 0
        self._last_countdown: datetime | None = None
        #: Every pause this gate has served, for the "exactly one pause" assertion
        #: a test wants and for the run summary. Counts arms, not waiters.
        self.pauses = 0
        self._heartbeats: list[PhaseOverlay] = []
        self._cancelled = threading.Event()

    # ------------------------------------------------------------------ policy

    @property
    def enabled(self) -> bool:
        return self.config.auto_resume

    @property
    def max_attempts(self) -> int:
        return max(1, self.config.max_attempts)

    def state(self) -> UsageLimitState | None:
        with self._cond:
            return self._state

    def cancel(self) -> None:
        """Release every waiter without waiting for the limit — the operator has
        asked the run to stop.

        One-way and permanent for this gate's life: a cancelled run is on its way
        out, and re-arming after the operator said stop would be the opposite of
        what they asked for. Waiters return ``False`` from ``pause`` so the caller
        re-raises instead of retrying into a limit that is still active.
        """
        self._cancelled.set()
        with self._cond:
            self._cond.notify_all()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    # -------------------------------------------------------------- heartbeats

    def watch(self, heartbeat: PhaseOverlay) -> None:
        """Show this gate's pause on a live group heartbeat.

        Every registered heartbeat gets the overlay, not just the group whose
        call happened to raise. That is the honest reading: an account-level
        limit blocks every group in the run, and a board showing one group
        "paused" while its siblings still claim to be running would be lying
        about the other cards. A gate already armed pushes immediately, so a
        group that starts mid-pause is not the one card without the banner.
        """
        with self._cond:
            self._heartbeats.append(heartbeat)
            phase = self.phase_text()
        if phase is not None:
            self._push_one(heartbeat, phase)

    def unwatch(self, heartbeat: PhaseOverlay) -> None:
        with self._cond:
            if heartbeat in self._heartbeats:
                self._heartbeats.remove(heartbeat)

    def _push_one(self, heartbeat: PhaseOverlay, phase: str) -> None:
        try:
            heartbeat.push_phase(phase)
        except Exception:  # noqa: BLE001 — evidence is never worth a run
            pass

    def _overlay_all(self, phase: str | None) -> None:
        with self._cond:
            heartbeats = list(self._heartbeats)
        for heartbeat in heartbeats:
            try:
                if phase is None:
                    heartbeat.pop_phase()
                else:
                    heartbeat.push_phase(phase)
            except Exception:  # noqa: BLE001 — see above
                pass

    # ------------------------------------------------------------------- pause

    def pause(self, detail: str) -> bool:
        """Arm (or join) the pause and block until the limit should have released.

        Returns ``True`` when the wait ran its course — the deadline passed,
        another thread released it, or ``max_wait_s`` bounded it — and the caller
        should retry. Returns ``False`` only when the gate was cancelled, where
        retrying would spend an attempt on a limit that is still active and delay
        a shutdown the operator asked for. Never raises.
        """
        if self._cancelled.is_set():
            return False
        with self._cond:
            now = self._now()
            reset_at = parse_reset_at(detail, now=now)
            wake_at = self._wake_for(reset_at, now)
            if self._state is None or self._state.released_at is not None:
                self._epoch += 1
                self.pauses += 1
                self._state = UsageLimitState(
                    armed_at=now, detail=detail, reset_at=reset_at, wake_at=wake_at
                )
                self._wake_at = wake_at
                self._last_countdown = now
                first = True
            else:
                first = False
                extended = self._wake_at is None or wake_at > self._wake_at
                self._state = replace(
                    self._state,
                    attempt=self._state.attempt + 1,
                    # Only a *later* deadline is adopted, along with the prose
                    # that justified it; a vaguer second message never shortens
                    # a wait a precise first one already earned.
                    detail=detail if extended else self._state.detail,
                    reset_at=reset_at if extended else self._state.reset_at,
                    wake_at=wake_at if extended else self._state.wake_at,
                )
                if extended:
                    self._wake_at = wake_at
            epoch = self._epoch
            state = self._state
            self._cond.notify_all()

        if first:
            self._emit(f"{self._label}: pausing this run {self._until_phrase(state)} — {detail}")
        self._publish(state)
        self._overlay_all(self.phase_text())
        return self._wait(epoch)

    def _wait(self, epoch: int) -> bool:
        while True:
            if self._cancelled.is_set():
                self._overlay_all(None)
                return False
            with self._cond:
                if self._state is None or self._epoch != epoch or self._state.released_at:
                    return True  # someone else already released this pause
                now = self._now()
                if self._max_wait_reached(now):
                    self._release_locked(now, bounded=True)
                    return True
                remaining = (self._wake_at - now).total_seconds() if self._wake_at else 0.0
                due = remaining <= 0
                probe = self._probe if due else None
                if due and probe is None:
                    self._release_locked(now, bounded=False)
                    return True
                line = None if due else self._due_countdown_locked(now, remaining)
                nap = 0.0 if due else min(remaining, COUNTDOWN_INTERVAL_S, MAX_SLEEP_SLICE_S)
            if probe is not None:
                # Run outside the lock: a probe may refresh a token or hit the
                # network, and holding the condition through that would block
                # every other waiter and every heartbeat push for no reason.
                healthy = self._run_probe(probe)
                with self._cond:
                    if self._state is None or self._epoch != epoch or self._state.released_at:
                        return True
                    if healthy:
                        self._release_locked(self._now(), bounded=False)
                        return True
                    self._wake_at = self._now() + timedelta(seconds=self.config.fallback_poll_s)
                    self._cond.notify_all()
                self._emit(f"{self._label}: still paused — re-checking later")
                continue
            if line:
                self._emit(line)
            self._sleep(max(nap, 0.0))

    def _run_probe(self, probe: Callable[[], bool]) -> bool:
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 — a broken probe pauses, it never crashes the run
            return False

    def _release_locked(self, now: datetime, *, bounded: bool) -> None:
        state = replace(self._state, released_at=now)  # type: ignore[arg-type]
        self._state = state
        self._wake_at = None
        self._cond.notify_all()
        waited = int((now - state.armed_at).total_seconds())
        why = "max_wait_s reached" if bounded else "limit should have reset"
        self._emit(f"{self._label}: resuming after {_humanize(waited)} ({why}); retrying the call")
        self._publish(state)
        self._overlay_all(None)

    def _due_countdown_locked(self, now: datetime, remaining: float) -> str | None:
        if self._last_countdown is not None:
            since = (now - self._last_countdown).total_seconds()
            if since < COUNTDOWN_INTERVAL_S:
                return None
        self._last_countdown = now
        return f"usage limit: still paused, ~{_humanize(int(remaining))} to go"

    # ------------------------------------------------------------------ deadline

    def _wake_for(self, reset_at: datetime | None, now: datetime) -> datetime:
        if reset_at is None:
            # No parseable deadline: poll. Retrying on a fixed interval costs one
            # cheap failed call per period and needs no guess about the reset.
            return now + timedelta(seconds=self.config.fallback_poll_s)
        wake = reset_at + timedelta(seconds=self.config.skew_s)
        # A reset already in the past (clock skew, a stale message) still waits
        # the skew, so a retry never fires into the same instant that failed.
        return max(wake, now + timedelta(seconds=self.config.skew_s))

    def _max_wait_reached(self, now: datetime) -> bool:
        if self.config.max_wait_s <= 0 or self._state is None:
            return False
        return (now - self._state.armed_at).total_seconds() >= self.config.max_wait_s

    def _until_phrase(self, state: UsageLimitState) -> str:
        if state.reset_at is None:
            return f"and re-checking every {_humanize(int(self.config.fallback_poll_s))}"
        # F22: the log line's own timestamp prefix (`log_event`) is stamped in
        # the operator's local zone, so the quoted reset instant is converted
        # to that same zone here — `reset_at` may otherwise carry whatever zone
        # the provider named (`parse_reset_at`'s `_zone_of`), which would print
        # a second, different offset on the same line. The provider's verbatim
        # wording (``detail``) is untouched — this only re-zones our own
        # restatement of the deadline, never the quoted prose itself.
        local_reset_at = state.reset_at.astimezone()
        return f"until {local_reset_at.isoformat(timespec='minutes')}"

    def phase_text(self) -> str | None:
        """What the heartbeat should say while this gate is armed, or ``None``."""
        state = self.state()
        if state is None or state.released_at is not None:
            return None
        if state.reset_at is None:
            return f"paused: {self._label} (no reset time given; polling)"
        local_reset_at = state.reset_at.astimezone()
        return f"paused: {self._label} until {local_reset_at.isoformat(timespec='minutes')}"

    # --------------------------------------------------------------------- sinks

    def _emit(self, line: str) -> None:
        """Log sinks are evidence, never control flow — a broken one is swallowed
        for the same reason the heartbeat swallows its own writes."""
        if self._log is None:
            return
        try:
            self._log(line)
        except Exception:  # noqa: BLE001 — a log sink must never fail a run
            pass

    def _publish(self, state: UsageLimitState) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(state)
        except Exception:  # noqa: BLE001 — see _emit
            pass


def _humanize(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"
