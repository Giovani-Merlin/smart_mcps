"""The usage-limit gate and the reset-time parser, on a fake clock.

Both halves are deliberately free of subprocesses and files, so everything here
runs in milliseconds and the "wait 5 hours" cases are exact rather than
approximated with a short sleep.

The parse table's entries are the strings that were actually observed (see
``tests/test_sessions.py`` for where each was seen), plus the shape variations
of those strings. The **weekly** wording is the one exception and is flagged as
such: it is unconfirmed, so the test pins the *behaviour we chose* for a
day-qualified reset rather than claiming to know the prose.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orchestrator.config import UsageLimitConfig
from orchestrator.execution.ratelimit import UsageLimitGate, parse_reset_at
from orchestrator.execution.sessions import RoundResult, SessionRunner, SessionUsage, UsageLimit

BERLIN = ZoneInfo("Europe/Berlin")


def berlin(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BERLIN)


# ------------------------------------------------------------------- parsing


def test_epoch_suffix_is_read_as_seconds():
    """The oldest observed form: `Claude AI usage limit reached|1700000000`."""
    now = datetime(2023, 11, 1, tzinfo=UTC)
    parsed = parse_reset_at("Claude AI usage limit reached|1700000000", now=now)
    assert parsed == datetime.fromtimestamp(1700000000, tz=UTC)


def test_epoch_suffix_in_milliseconds_is_recognized():
    now = datetime(2023, 11, 1, tzinfo=UTC)
    parsed = parse_reset_at("usage limit reached|1700000000000", now=now)
    assert parsed == datetime.fromtimestamp(1700000000, tz=UTC)


def test_named_zone_wall_clock():
    """The live-tier string from 2026-08-13, verbatim."""
    now = berlin(2026, 8, 13, 9, 30)
    parsed = parse_reset_at("You've hit your session limit · resets 1pm (Europe/Berlin)", now=now)
    assert parsed == berlin(2026, 8, 13, 13)


def test_minutes_are_kept():
    now = berlin(2026, 8, 13, 9, 30)
    assert parse_reset_at("limit · resets 1:30pm (Europe/Berlin)", now=now) == berlin(
        2026, 8, 13, 13, 30
    )


def test_twenty_four_hour_clock_without_a_meridiem():
    now = berlin(2026, 8, 13, 9, 30)
    assert parse_reset_at("limit · resets 13:00 (Europe/Berlin)", now=now) == berlin(
        2026, 8, 13, 13
    )


def test_an_absent_zone_falls_back_to_the_clock_we_are_reading_it_on():
    now = berlin(2026, 8, 13, 9, 30)
    assert parse_reset_at("session limit · resets 1pm", now=now) == berlin(2026, 8, 13, 13)


def test_a_time_already_past_rolls_to_tomorrow():
    """Reading `resets 1pm` at 4pm can only mean tomorrow — a same-day answer
    would put the deadline in the past and burn the retry immediately."""
    now = berlin(2026, 8, 13, 16)
    assert parse_reset_at("limit · resets 1pm (Europe/Berlin)", now=now) == berlin(2026, 8, 14, 13)


def test_midnight_meridiem_edge_cases():
    now = berlin(2026, 8, 13, 9)
    assert parse_reset_at("resets 12am (Europe/Berlin)", now=now) == berlin(2026, 8, 14, 0)
    assert parse_reset_at("resets 12pm (Europe/Berlin)", now=now) == berlin(2026, 8, 13, 12)


def test_a_day_qualified_reset_lands_on_the_next_such_weekday():
    """The weekly limit's shape. NB the prose here is *unconfirmed* — capture the
    verbatim string the first time a real weekly limit fires and pin it."""
    now = berlin(2026, 8, 13, 9)  # a Thursday
    assert parse_reset_at("weekly limit · resets Monday 9am (Europe/Berlin)", now=now) == berlin(
        2026, 8, 17, 9
    )


def test_a_day_qualified_reset_later_today_stays_today():
    now = berlin(2026, 8, 13, 7)  # Thursday, before 9am
    assert parse_reset_at("weekly limit · resets Thursday 9am (Europe/Berlin)", now=now) == berlin(
        2026, 8, 13, 9
    )


@pytest.mark.parametrize(
    "detail",
    [
        "",
        "Claude AI usage limit reached",  # a limit, but it says nothing about when
        "claude exited 1: Segmentation fault",
        "resets soon",
        "limit · resets 99:99",
        "rate limited",
    ],
)
def test_unparseable_deadlines_return_none(detail):
    """None is a supported answer: the gate polls on it. Guessing a deadline is
    strictly worse — early burns an attempt, late holds the run past the reset."""
    assert parse_reset_at(detail, now=datetime.now(UTC)) is None


def test_an_unresolvable_zone_abbreviation_degrades_to_local_rather_than_a_wrong_instant():
    now = berlin(2026, 8, 13, 9)
    assert parse_reset_at("limit · resets 1pm (CEST)", now=now) == berlin(2026, 8, 13, 13)


# ---------------------------------------------------------------------- gate


class FakeClock:
    """A clock the test advances by hand. ``sleep`` jumps rather than waits, so
    a five-hour pause takes microseconds and lands on an exact instant."""

    def __init__(self, start: datetime):
        self.now_value = start
        self.slept: list[float] = []
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self.now_value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.slept.append(seconds)
            self.now_value += timedelta(seconds=seconds)


def make_gate(start: datetime, **config) -> tuple[UsageLimitGate, FakeClock, list[str]]:
    clock = FakeClock(start)
    lines: list[str] = []
    gate = UsageLimitGate(
        UsageLimitConfig(**config),
        now=clock.now,
        sleep=clock.sleep,
        log=lines.append,
    )
    return gate, clock, lines


def test_pause_waits_until_the_reset_plus_the_skew():
    start = berlin(2026, 8, 13, 12)
    gate, clock, lines = make_gate(start, skew_s=60)
    gate.pause("session limit · resets 1pm (Europe/Berlin)")
    assert clock.now() == berlin(2026, 8, 13, 13) + timedelta(seconds=60)
    assert gate.pauses == 1
    assert any("pausing this run" in line for line in lines)
    assert any("resuming after" in line for line in lines)


def test_the_pause_is_released_and_the_gate_re_arms_cleanly():
    """A retry that hits the limit again arms a *second* pause rather than
    joining the finished first one — no probe protocol, by design."""
    gate, clock, _ = make_gate(berlin(2026, 8, 13, 12))
    gate.pause("limit · resets 1pm (Europe/Berlin)")
    gate.pause("limit · resets 3pm (Europe/Berlin)")
    assert gate.pauses == 2
    assert clock.now() >= berlin(2026, 8, 13, 15)


def test_a_countdown_line_is_emitted_while_waiting():
    gate, _, lines = make_gate(berlin(2026, 8, 13, 12), skew_s=0)
    gate.pause("limit · resets 3pm (Europe/Berlin)")  # three hours
    countdowns = [line for line in lines if "still paused" in line]
    # 5-minute cadence over three hours, minus the arm tick.
    assert len(countdowns) >= 30


def test_an_unparseable_deadline_falls_back_to_polling():
    gate, clock, lines = make_gate(berlin(2026, 8, 13, 12), fallback_poll_s=900)
    gate.pause("Claude AI usage limit reached")
    assert clock.now() == berlin(2026, 8, 13, 12) + timedelta(seconds=900)
    assert any("re-checking every" in line for line in lines)


def test_max_wait_bounds_the_pause():
    gate, clock, lines = make_gate(berlin(2026, 8, 13, 12), max_wait_s=600, skew_s=0)
    gate.pause("limit · resets 11pm (Europe/Berlin)")  # eleven hours away
    assert clock.now() <= berlin(2026, 8, 13, 12) + timedelta(seconds=900)
    assert any("max_wait_s reached" in line for line in lines)


def test_two_threads_share_one_pause():
    """The second caller *joins* rather than starting its own wait — otherwise
    two limited groups would serialize their sleeps and wait twice as long."""
    clock = FakeClock(berlin(2026, 8, 13, 12))
    lines: list[str] = []
    # Both callers are held inside the wait until the test has seen both of them
    # there — a fake clock's instant sleep would otherwise let the first pause
    # finish before the second one had a chance to join it.
    entered = threading.Semaphore(0)
    release = threading.Event()

    def sleep(seconds: float) -> None:
        entered.release()
        assert release.wait(timeout=5)
        clock.sleep(seconds)

    gate = UsageLimitGate(UsageLimitConfig(skew_s=0), now=clock.now, sleep=sleep, log=lines.append)
    threads = [
        threading.Thread(target=lambda: gate.pause("limit · resets 1pm (Europe/Berlin)"))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.acquire(timeout=5) and entered.acquire(timeout=5)
    release.set()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert gate.pauses == 1
    assert len([line for line in lines if "pausing this run" in line]) == 1


def test_a_later_hit_extends_the_deadline_and_a_vaguer_one_never_shortens_it():
    """Both directions matter, and they are one mechanism.

    A sibling group hitting the same limit mid-wait is modelled here by pausing
    again from inside the sleep — the same thing two threads do, made
    deterministic. The 5pm hit must move the deadline out; the message after it
    carries no reset time at all, and must leave the 5pm deadline alone rather
    than collapsing it to a 60-second poll.
    """
    clock = FakeClock(berlin(2026, 8, 13, 12))
    later_hits = ["limit · resets 5pm (Europe/Berlin)", "Claude AI usage limit reached"]

    def sleep(seconds: float) -> None:
        if later_hits:
            gate.pause(later_hits.pop(0))
        clock.sleep(seconds)

    gate = UsageLimitGate(
        UsageLimitConfig(skew_s=0, fallback_poll_s=60), now=clock.now, sleep=sleep
    )
    gate.pause("limit · resets 1pm (Europe/Berlin)")

    state = gate.state()
    assert state is not None
    assert state.reset_at == berlin(2026, 8, 13, 17)
    assert "5pm" in state.detail
    assert clock.now() >= berlin(2026, 8, 13, 17)
    assert gate.pauses == 1  # one pause, joined — not three


def test_phase_text_reads_as_paused_not_wedged():
    gate, _, _ = make_gate(berlin(2026, 8, 13, 12))
    assert gate.phase_text() is None  # nothing armed
    watched = _Overlay()
    gate.watch(watched)
    gate.pause("limit · resets 1pm (Europe/Berlin)")
    # Armed and released within the call, so the overlay was pushed then popped.
    assert watched.pushed and "paused: usage limit until" in watched.pushed[0]
    assert watched.popped == 1


def test_a_heartbeat_registered_mid_pause_gets_the_overlay_immediately():
    """A group that starts while the account is already limited must not be the
    one card on the board without the banner."""
    clock = FakeClock(berlin(2026, 8, 13, 12))
    proceed = threading.Event()

    def sleep(seconds: float) -> None:
        assert proceed.wait(timeout=5)
        clock.sleep(seconds)

    gate = UsageLimitGate(UsageLimitConfig(skew_s=0), now=clock.now, sleep=sleep)
    late = _Overlay()
    thread = threading.Thread(target=lambda: gate.pause("limit · resets 1pm (Europe/Berlin)"))
    thread.start()

    deadline = time.monotonic() + 5
    while gate.phase_text() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    gate.watch(late)  # registered while the gate is already armed
    assert late.pushed

    proceed.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_a_disabled_gate_reports_itself_disabled():
    gate = UsageLimitGate(UsageLimitConfig(auto_resume=False))
    assert gate.enabled is False


class _Overlay:
    def __init__(self):
        self.pushed: list[str] = []
        self.popped = 0

    def push_phase(self, phase: str) -> None:
        self.pushed.append(phase)

    def pop_phase(self) -> None:
        self.popped += 1


def test_cancelling_releases_every_waiter_promptly():
    """Ctrl-C must not be held hostage by a five-hour pause.

    Worker calls run in `asyncio.to_thread` pool threads and `asyncio.run` joins
    that pool on the way out, so a waiter that ignored cancellation would keep
    the whole process alive long after the operator asked it to stop — turning a
    paused run into an unkillable one.
    """
    clock = FakeClock(berlin(2026, 8, 13, 12))
    entered = threading.Semaphore(0)
    holding = threading.Event()

    def sleep(seconds: float) -> None:
        entered.release()
        assert holding.wait(timeout=5)
        clock.sleep(seconds)

    gate = UsageLimitGate(UsageLimitConfig(), now=clock.now, sleep=sleep)
    result: list[bool] = []
    thread = threading.Thread(
        target=lambda: result.append(gate.pause("limit · resets 11pm (Europe/Berlin)"))
    )
    thread.start()
    assert entered.acquire(timeout=5)

    gate.cancel()
    holding.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    # False, so the caller re-raises instead of retrying into a live limit.
    assert result == [False]
    # And the clock never reached the deadline: it stopped waiting, it did not
    # wait it out.
    assert clock.now() < berlin(2026, 8, 13, 23)


def test_a_cancelled_gate_refuses_to_arm_again():
    gate, clock, _ = make_gate(berlin(2026, 8, 13, 12))
    gate.cancel()
    assert gate.pause("limit · resets 1pm (Europe/Berlin)") is False
    assert gate.pauses == 0
    assert clock.now() == berlin(2026, 8, 13, 12)  # nothing waited at all


def test_the_wait_is_sliced_so_cancellation_cannot_be_coarse():
    """Regression guard on the slice bound: a single `sleep(remaining)` would
    make cancellation as coarse as the pause itself."""
    from orchestrator.execution.ratelimit import MAX_SLEEP_SLICE_S

    gate, clock, _ = make_gate(berlin(2026, 8, 13, 12), skew_s=0)
    gate.pause("limit · resets 1pm (Europe/Berlin)")  # an hour
    assert max(clock.slept) <= MAX_SLEEP_SLICE_S
    assert clock.now() == berlin(2026, 8, 13, 13)  # sliced, but exact in total


# ---------------------------------------------- the retry's argv (regression)
#
# Everything above this line runs on a fake clock with no subprocess, which is
# exactly why it could not catch the bug these tests pin: the gate's *waiting*
# was correct and its *retry* could never succeed. On a live run
# (r20260812-202855, group g4, 2026-08-14) the gate waited 3h42m and the retry
# failed in the same second with "Session ID <uuid> is already in use", because
# the CLI registers the id before failing on the limit and `_call_with_retry`
# re-sent the spent one. `tests/fake_claude.py` has no such rejection, so no
# stub-based test can catch it either — hence a direct assertion on the argv.


class _RecordingRunner(SessionRunner):
    """A runner whose `_invoke` records argv and raises `UsageLimit` on cue."""

    def __init__(self, limits: int, **kwargs):
        super().__init__(claude_bin="/nonexistent-claude", **kwargs)
        self.seen: list[list[str]] = []
        self._limits = limits

    def _invoke(self, argv, *, prompt, cwd, context, on_turn=None):
        self.seen.append(list(argv))
        if len(self.seen) <= self._limits:
            raise UsageLimit("You've hit your session limit · resets 11pm (Europe/Berlin)")
        return RoundResult(
            text="ok", session_id=_session_id_of(argv), usage=SessionUsage(), envelope={}
        )


def _session_id_of(argv: list[str]) -> str:
    return argv[argv.index("--session-id") + 1] if "--session-id" in argv else "new"


def _instant_gate() -> UsageLimitGate:
    """A real gate on the file's jump-ahead clock, so each pause costs microseconds
    and lands on an exact instant. The waiting itself is covered above; these tests
    care only about the argv the retry then sends."""
    gate, _clock, _lines = make_gate(berlin(2026, 8, 13, 19), skew_s=0)
    return gate


def test_a_retried_session_id_is_fresh_because_the_first_one_is_already_spent():
    runner = _RecordingRunner(limits=1, gate=_instant_gate())

    result = runner._call_with_retry(
        ["claude", "--session-id", "11111111-1111-1111-1111-111111111111"],
        prompt="p",
        cwd=Path("/tmp"),
        context="--session-id 11111111-1111-1111-1111-111111111111",
    )

    first, second = (_session_id_of(argv) for argv in runner.seen)
    assert first == "11111111-1111-1111-1111-111111111111"
    assert second != first, "the retry replayed the spent id and would die 'already in use'"
    uuid.UUID(second)  # a real uuid, not a mangled string
    # The caller learns which id actually ran — the manifest depends on it.
    assert result.session_id == second


def test_every_attempt_after_a_limit_gets_its_own_id():
    runner = _RecordingRunner(limits=3, gate=_instant_gate())

    runner._call_with_retry(
        ["claude", "--session-id", "11111111-1111-1111-1111-111111111111"],
        prompt="p",
        cwd=Path("/tmp"),
        context="c",
    )

    ids = [_session_id_of(argv) for argv in runner.seen]
    assert len(ids) == 4
    assert len(set(ids)) == 4, f"ids repeated across attempts: {ids}"


def test_a_resume_id_is_never_rewritten_or_the_retry_addresses_the_wrong_session():
    runner = _RecordingRunner(limits=1, gate=_instant_gate())

    runner._call_with_retry(
        ["claude", "--resume", "22222222-2222-2222-2222-222222222222"],
        prompt="p",
        cwd=Path("/tmp"),
        context="c",
    )

    assert runner.seen[0] == runner.seen[1], "a --resume retry must re-enter the same session"


def test_argv_without_a_session_id_is_retried_unchanged():
    runner = _RecordingRunner(limits=1, gate=_instant_gate())

    runner._call_with_retry(["claude", "--print"], prompt="p", cwd=Path("/tmp"), context="c")

    assert runner.seen[0] == runner.seen[1]
