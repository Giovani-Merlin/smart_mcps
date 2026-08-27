"""F22 (log half): `log_event` stamps the run log in one labelled zone.

Before this, `log_event` timestamped every line in UTC while
`UsageLimitGate._until_phrase`/`phase_text` quoted the usage-limit reset
instant in the operator's local zone — the same log line could show a UTC
prefix next to a local-zone deadline with no label explaining either.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orchestrator.config import UsageLimitConfig
from orchestrator.execution.manifest import RunPaths, log_event
from orchestrator.execution.ratelimit import UsageLimitGate

BERLIN = ZoneInfo("Europe/Berlin")


class _FakeClock:
    """A clock the test advances by hand — mirrors test_ratelimit.py's own,
    so a multi-hour pause resolves in microseconds instead of hanging."""

    def __init__(self, start: datetime):
        self.now_value = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self.now_value

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now_value += timedelta(seconds=seconds)


def test_zone_is_stated_once_at_the_top_of_the_file(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    log_event(paths, "run r1: on_group_failure=halt")
    log_event(paths, "run r1 started (autonomous)")

    lines = paths.event_log_path.read_text().splitlines()
    zone_lines = [line for line in lines if "log timestamps below are local time" in line]
    assert len(zone_lines) == 1
    assert zone_lines[0] is lines[0] or lines.index(zone_lines[0]) == 0


def test_every_line_including_the_header_is_timestamped(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    log_event(paths, "run r1: on_group_failure=halt")
    log_event(paths, "run r1 started (autonomous)")

    lines = paths.event_log_path.read_text().splitlines()
    assert len(lines) == 3  # header + two events
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}T[^ ]+  ", line) for line in lines)


def test_header_appears_only_on_the_first_call_across_many(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    for i in range(5):
        log_event(paths, f"event {i}")

    lines = paths.event_log_path.read_text().splitlines()
    zone_lines = [line for line in lines if "log timestamps below are local time" in line]
    assert len(zone_lines) == 1


def test_prefix_and_reset_instant_share_the_operator_local_zone():
    """A `UsageLimitGate` reset instant is converted to the operator's local
    zone (the same zone `log_event`'s own timestamp prefix uses), even when
    the provider named a different zone in its detail string."""
    clock = _FakeClock(datetime(2026, 8, 13, 9, 30, tzinfo=BERLIN))
    gate = UsageLimitGate(
        UsageLimitConfig(skew_s=0),
        now=clock.now,
        sleep=clock.sleep,
    )
    gate.pause("You've hit your session limit · resets 1pm (Europe/Berlin)")
    state = gate.state()
    assert state is not None
    assert state.reset_at == datetime(2026, 8, 13, 13, tzinfo=BERLIN)

    phrase = gate._until_phrase(state)
    local_reset_at = state.reset_at.astimezone()
    assert local_reset_at.isoformat(timespec="minutes") in phrase
    # The reported instant carries the *local* offset, not necessarily Berlin's.
    assert phrase == f"until {local_reset_at.isoformat(timespec='minutes')}"


def test_provider_verbatim_wording_is_never_rezoned():
    """The provider's own prose (`detail`) is untouched by the local-zone
    conversion — only our own restatement of the deadline is converted."""
    clock = _FakeClock(datetime(2026, 8, 13, 9, 30, tzinfo=BERLIN))
    lines: list[str] = []
    gate = UsageLimitGate(
        UsageLimitConfig(skew_s=0),
        now=clock.now,
        sleep=clock.sleep,
        log=lines.append,
    )
    verbatim = "You've hit your session limit · resets 1pm (Europe/Berlin)"
    gate.pause(verbatim)
    assert any(verbatim in line for line in lines)
