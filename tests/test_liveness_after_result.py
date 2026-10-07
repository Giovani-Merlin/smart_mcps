"""`sign_of_life` against a real busy process and the real /proc: CPU ticks stop
counting once the child's newest stream event is `result`."""

import datetime
import subprocess
import sys
import time

from orchestrator.execution.liveness import ChildActivity, cpu_ticks, sign_of_life

WINDOW_S = 60.0


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).isoformat(timespec="milliseconds")


def _activity(pid: int, *, event_type: str, event_age_s: float) -> ChildActivity:
    now = time.time()
    return ChildActivity(
        pid=pid,
        session_id="s",
        cwd=".",
        spawned_at=_iso(now - 1000),
        last_event_at=_iso(now - event_age_s),
        last_event_type=event_type,
    )


def _two_ticks(child: ChildActivity):
    prev = cpu_ticks(child.pid)
    # Let the busy loop burn at least a couple of clock ticks.
    deadline = time.time() + 5
    while time.time() < deadline and (cpu_ticks(child.pid) or 0) <= (prev or 0):
        time.sleep(0.05)
    first = sign_of_life(child, prev_cpu=prev, now=time.time(), window_s=WINDOW_S)
    time.sleep(0.5)
    second = sign_of_life(child, prev_cpu=first.cpu_ticks, now=time.time(), window_s=WINDOW_S)
    return first, second


def test_cpu_counts_before_result_but_not_after():
    proc = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        stale = WINDOW_S * 10
        for result in _two_ticks(_activity(proc.pid, event_type="assistant", event_age_s=stale)):
            assert result.signal == "cpu"
        for result in _two_ticks(_activity(proc.pid, event_type="result", event_age_s=stale)):
            assert result.signal is None
            assert result.evidence == "idle after result"
            assert result.cpu_ticks is not None
    finally:
        proc.kill()
        proc.wait()


def test_a_result_inside_the_window_is_still_an_event():
    proc = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        child = _activity(proc.pid, event_type="result", event_age_s=1.0)
        result = sign_of_life(child, prev_cpu=None, now=time.time(), window_s=WINDOW_S)
        assert result.signal == "event"
        assert result.evidence.startswith("event result")
    finally:
        proc.kill()
        proc.wait()
