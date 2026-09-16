"""U1 tests: the in-process ``ActivityRegistry`` and its wiring into
``SessionRunner`` (plan U1). U2/U3 tests: the three Sign of Life signals, the
``[liveness]`` config section, the ``LivenessProbe``, and the reader-side
``liveness_line``/``cures_exhausted_line`` helpers (plan U2/U3).

Every test runs against ``tests/fake_claude.py`` — zero live CLI calls, zero
tokens (plan R24), matching the convention ``tests/test_streaming.py`` uses.
The real-`/proc` oracle tests are Linux-only, same as the platform this
orchestrator runs on.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.config import LivenessConfig, load_config
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.liveness import (
    ActivityRegistry,
    ChildActivity,
    LivenessProbe,
    SuspendFacts,
    SuspendMonitor,
    cpu_ticks,
    cures_exhausted_line,
    descendants,
    kill_tree,
    liveness_line,
    read_suspend_facts,
    should_cure,
    sign_of_life,
)
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.sessions import SessionError, SessionRunner, SuspendCured
from orchestrator.execution.streaming import StreamingProcess

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def make_runner(fake_home: Path, **kwargs) -> SessionRunner:
    env = {"FAKE_CLAUDE_HOME": str(fake_home), **kwargs.pop("env", {})}
    kwargs.setdefault("transcript_root", fake_home / "projects")
    return SessionRunner(claude_bin=[sys.executable, str(FAKE_CLAUDE)], env=env, **kwargs)


STREAM_ARGV = [
    sys.executable,
    str(FAKE_CLAUDE),
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--input-format",
    "stream-json",
]


def script(fake_home: Path, entry: dict) -> None:
    with (fake_home / "script.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# --------------------------------------------------------------- registry


def test_registry_current_returns_the_newest_live_child_for_a_cwd():
    registry = ActivityRegistry()
    registry.spawned(101, session_id="s1", cwd="/work/g1")
    registry.spawned(102, session_id="s2", cwd="/work/g1")

    current = registry.current("/work/g1")
    assert current is not None
    assert current.pid == 102
    assert current.session_id == "s2"


def test_registry_current_distinguishes_by_cwd():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    registry.spawned(2, session_id="s2", cwd="/work/g2")

    assert registry.current("/work/g1").pid == 1
    assert registry.current("/work/g2").pid == 2
    assert registry.current("/work/g3") is None


def test_registry_current_is_none_after_exit():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    assert registry.current("/work/g1") is not None

    registry.exited(1)
    assert registry.current("/work/g1") is None


def test_registry_note_event_for_an_unknown_pid_is_a_noop():
    registry = ActivityRegistry()
    # Never raises, and there is nothing to read back for a pid never spawned.
    registry.note_event(9999, "assistant")
    registry.note_assistant_text(9999, "hello")
    registry.mark_cured(9999)
    assert registry.was_cured(9999) is False


def test_registry_note_event_stamps_the_live_child():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    registry.note_event(1, "assistant")

    current = registry.current("/work/g1")
    assert current is not None
    assert current.last_event_type == "assistant"
    assert current.last_event_at is not None


def test_registry_was_cured_survives_exit():
    """`_invoke` checks `was_cured` *after* the child has exited — the whole
    point of the flag — so it must still answer for a pid no longer live."""
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    registry.mark_cured(1)
    registry.exited(1)

    assert registry.was_cured(1) is True
    assert registry.current("/work/g1") is None  # exit still ends its "live" status


def test_registry_was_cured_defaults_false():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    assert registry.was_cured(1) is False


# ----------------------------------------------------------------- runner


class _SpyRegistry(ActivityRegistry):
    """Records every `spawned`/`exited` call it sees, on top of the real
    registry behaviour — so a test can assert on the calls a runner made
    without reaching into the registry's private state."""

    def __init__(self) -> None:
        super().__init__()
        self.spawned_calls: list[tuple[int, str, str]] = []
        self.exited_calls: list[int] = []

    def spawned(self, pid: int, *, session_id: str, cwd: str) -> None:
        self.spawned_calls.append((pid, session_id, cwd))
        super().spawned(pid, session_id=session_id, cwd=cwd)

    def exited(self, pid: int) -> None:
        self.exited_calls.append(pid)
        super().exited(pid)


def test_runner_registers_spawn_with_session_id_and_cwd_then_exit(fake_home, tmp_path):
    script(fake_home, {"result": "ok"})
    registry = _SpyRegistry()
    runner = make_runner(fake_home, activity=registry)

    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    assert len(registry.spawned_calls) == 1
    pid, session_id, cwd = registry.spawned_calls[0]
    assert session_id == result.session_id
    assert cwd == str(tmp_path)
    assert registry.exited_calls == [pid]
    # The round has finished, so the child is no longer live for this cwd.
    assert registry.current(str(tmp_path)) is None


def test_runner_note_event_sees_live_activity_during_the_round(fake_home, tmp_path):
    """A registry wired into a runner observes the child while its round is in
    flight, via the same `on_turn` seam the stream already exposes."""
    script(fake_home, {"turns": [{"input_tokens": 1, "output_tokens": 1}], "result": "done"})
    registry = ActivityRegistry()
    runner = make_runner(fake_home, activity=registry)
    seen_types: list[str] = []

    def on_turn(_usage, _send) -> None:
        current = registry.current(str(tmp_path))
        if current is not None and current.last_event_type is not None:
            seen_types.append(current.last_event_type)

    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path, on_turn=on_turn)
    assert "assistant" in seen_types


def test_runner_raises_suspend_cured_when_the_exit_was_a_cured_kill(fake_home, tmp_path):
    registry = ActivityRegistry()
    runner = make_runner(fake_home, activity=registry)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    # Force the *next* spawned pid to be marked cured before the runner checks
    # it: monkeypatch-free by wrapping `spawned` isn't needed — we mark it via
    # the registry's own `spawned` hook, observed through a one-shot wrapper.
    original_spawned = registry.spawned

    def spawned_and_cure(pid, *, session_id, cwd):
        original_spawned(pid, session_id=session_id, cwd=cwd)
        registry.mark_cured(pid)

    registry.spawned = spawned_and_cure  # type: ignore[method-assign]

    script(fake_home, {"exit_code": 7, "stderr": "killed"})
    with pytest.raises(SuspendCured) as excinfo:
        runner.start_fork(base_id=base.session_id, prompt="go", name="worker-1", cwd=tmp_path)
    assert excinfo.value.session_id  # the fresh --session-id this attempt used


def test_runner_raises_plain_session_error_without_the_cured_mark(fake_home, tmp_path):
    registry = ActivityRegistry()
    runner = make_runner(fake_home, activity=registry)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    script(fake_home, {"exit_code": 3, "stderr": "boom"})
    with pytest.raises(SessionError) as excinfo:
        runner.start_fork(base_id=base.session_id, prompt="go", name="worker-1", cwd=tmp_path)
    assert not isinstance(excinfo.value, SuspendCured)


def test_runner_with_no_registry_wired_behaves_exactly_as_before(fake_home, tmp_path):
    """`activity=None` (the default) must not change today's behaviour at
    all: a nonzero exit is a plain `SessionError`, never `SuspendCured`."""
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    script(fake_home, {"exit_code": 3, "stderr": "boom"})
    with pytest.raises(SessionError) as excinfo:
        runner.start_fork(base_id=base.session_id, prompt="go", name="worker-1", cwd=tmp_path)
    assert not isinstance(excinfo.value, SuspendCured)


# =========================================================== plan U2 / U3


# ------------------------------------------------------------ /proc fixtures


def _write_proc_stat(
    proc_root: Path,
    pid: int,
    *,
    ppid: int,
    state: str = "R",
    comm: str = "proc",
    utime: int = 0,
    stime: int = 0,
) -> None:
    """A fake ``/proc/<pid>/stat`` line, with a parenthesised ``comm`` that may
    itself contain a space — the case that makes naive whitespace-splitting of
    the whole line wrong and forces splitting after the last ``)`` instead."""
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    rest = [
        state,
        str(ppid),
        "1",
        "1",
        "0",
        "-1",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(utime),
        str(stime),
        "0",
        "0",
        "20",
        "0",
        "1",
        "0",
        "1000",
    ]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")


def _write_proc_cmdline(proc_root: Path, pid: int, parts: list[str]) -> None:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\x00".join(part.encode() for part in parts) + b"\x00")


def _child(pid: int = 999, **overrides) -> ChildActivity:
    defaults = dict(
        pid=pid,
        session_id="s1",
        cwd="/work/g1",
        spawned_at="2020-01-01T00:00:00.000+00:00",
    )
    defaults.update(overrides)
    return ChildActivity(**defaults)


# ------------------------------------------------------------- sign_of_life


def test_sign_of_life_b_fires_only_for_the_live_nonzombie_child_with_matching_ppid(tmp_path):
    proc_root = tmp_path / "proc"
    parent_pid = 100
    _write_proc_stat(proc_root, parent_pid, ppid=1, comm="parent proc")
    _write_proc_stat(proc_root, 101, ppid=parent_pid, state="Z", comm="dead child")
    _write_proc_stat(proc_root, 102, ppid=parent_pid, state="R", comm="sleep")
    _write_proc_cmdline(proc_root, 102, ["sleep", "30"])
    # A pid with a different ppid must never be picked up.
    _write_proc_stat(proc_root, 103, ppid=999, state="R", comm="unrelated")

    child = _child(pid=parent_pid)
    result = sign_of_life(child, prev_cpu=None, now=time.time(), window_s=600, proc_root=proc_root)

    assert result.signal == "tool_child"
    assert "sleep 30" in result.evidence


def test_sign_of_life_b_finds_nothing_when_only_a_zombie_child_exists(tmp_path):
    proc_root = tmp_path / "proc"
    parent_pid = 200
    _write_proc_stat(proc_root, parent_pid, ppid=1)
    _write_proc_stat(proc_root, 201, ppid=parent_pid, state="Z", comm="dead")

    child = _child(pid=parent_pid)
    result = sign_of_life(child, prev_cpu=None, now=time.time(), window_s=600, proc_root=proc_root)

    assert result.signal is None


def test_sign_of_life_c_fires_when_cpu_ticks_advance_and_not_when_flat(tmp_path):
    proc_root = tmp_path / "proc"
    pid = 300
    _write_proc_stat(proc_root, pid, ppid=1, utime=10, stime=10)  # total 20

    child = _child(pid=pid)
    advancing = sign_of_life(child, prev_cpu=15, now=time.time(), window_s=600, proc_root=proc_root)
    assert advancing.signal == "cpu"
    assert advancing.cpu_ticks == 20

    flat = sign_of_life(child, prev_cpu=20, now=time.time(), window_s=600, proc_root=proc_root)
    assert flat.signal is None


def test_sign_of_life_a_fires_for_a_fresh_event_ahead_of_the_other_two_signals(tmp_path):
    proc_root = tmp_path / "proc"
    now = time.time()
    fresh = datetime.datetime.fromtimestamp(now - 5, tz=datetime.UTC).isoformat(
        timespec="milliseconds"
    )
    child = _child(last_event_at=fresh, last_event_type="assistant")

    result = sign_of_life(child, prev_cpu=None, now=now, window_s=600, proc_root=proc_root)
    assert result.signal == "event"
    assert "assistant" in result.evidence


def test_sign_of_life_a_pid_that_vanishes_between_listing_and_reading_yields_no_signal(tmp_path):
    """A directory that disappears mid-probe must read as absent evidence,
    never raise — races between listing `/proc` and opening a file inside it
    are exactly as real on the kernel as they are contrived here."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    child = _child(pid=404)

    result = sign_of_life(child, prev_cpu=50, now=time.time(), window_s=600, proc_root=proc_root)

    assert result.signal is None
    assert result.cpu_ticks is None


# ----------------------------------------------------------------- config


def test_config_liveness_defaults_with_no_file():
    config = load_config(None)
    assert config.liveness.window_seconds == 600
    assert config.liveness.suspend_gap_seconds == 60
    assert config.liveness.kill_grace_seconds == 10
    assert config.liveness.max_cures_per_generation == 2


def test_config_liveness_window_seconds_overridden_from_toml(tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_text("[liveness]\nwindow_seconds = 20\n")

    config = load_config(toml_path)

    assert config.liveness.window_seconds == 20
    # Everything not overridden keeps its default.
    assert config.liveness.suspend_gap_seconds == 60


# -------------------------------------------------------------- real /proc


@pytest.mark.skipif(sys.platform != "linux", reason="the real /proc oracle is Linux-only")
def test_sign_of_life_real_proc_reports_tool_child_naming_sleep():
    # `& wait` keeps `sh` alive as the parent instead of exec-optimizing
    # straight into `sleep`, so there is a real parent/child pair to probe.
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
    try:
        time.sleep(0.3)
        child = _child(pid=proc.pid)
        result = sign_of_life(child, prev_cpu=None, now=time.time(), window_s=600)
        assert result.signal == "tool_child"
        assert "sleep" in result.evidence
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="the real /proc oracle is Linux-only")
def test_sign_of_life_real_proc_reports_flat_cpu_when_stopped_and_advancing_once_continued():
    proc = subprocess.Popen([sys.executable, "-c", "while True: pass"])
    try:
        pid = proc.pid
        time.sleep(0.2)
        os.kill(pid, signal.SIGSTOP)
        time.sleep(0.2)
        first_cpu = cpu_ticks(pid)
        time.sleep(0.2)
        second_cpu = cpu_ticks(pid)

        child = _child(pid=pid)
        flat = sign_of_life(child, prev_cpu=first_cpu, now=time.time(), window_s=600)
        assert flat.signal is None

        os.kill(pid, signal.SIGCONT)
        time.sleep(0.3)
        advancing = sign_of_life(child, prev_cpu=second_cpu, now=time.time(), window_s=600)
        assert advancing.signal == "cpu"
    finally:
        proc.kill()
        proc.wait(timeout=5)


# --------------------------------------------------------------- transitions


def test_transitions_log_exactly_once_entering_and_exactly_once_leaving_not_live(tmp_path):
    """40 ticks, 15s apart, window 60s: the child goes silent at tick 5 and an
    event returns at tick 30. Exactly one `not live for` line and exactly one
    `live again` line, nothing in between."""
    paths = RunPaths(tmp_path, "r1")
    hb = RoundHeartbeat(paths, "g1")
    config = LivenessConfig(window_seconds=60)
    logs: list[str] = []
    state = {"i": 0}

    def clock() -> float:
        return state["i"] * 15.0

    def activity_provider() -> ChildActivity:
        i = state["i"]
        # Silent from tick 5 onward (frozen at tick 4's timestamp) until an
        # event returns at tick 30.
        event_tick = i if (i < 5 or i >= 30) else 4
        at = datetime.datetime.fromtimestamp(event_tick * 15.0, tz=datetime.UTC).isoformat(
            timespec="milliseconds"
        )
        return _child(pid=1, spawned_at=at, last_event_at=at, last_event_type="assistant")

    probe = LivenessProbe(
        hb, config, activity_provider, log=logs.append, proc_root=tmp_path / "proc", clock=clock
    )

    for i in range(40):
        state["i"] = i
        probe.tick()

    not_live_lines = [line for line in logs if "not live for" in line]
    live_again_lines = [line for line in logs if "live again" in line]
    assert len(not_live_lines) == 1
    assert len(live_again_lines) == 1
    assert not_live_lines[0] == (
        "group g1 generation 0: not live for 1m15s in unknown phase — no signal"
    )
    assert live_again_lines[0].startswith("group g1 generation 0: live again: event ")


# ------------------------------------------------------------ liveness_line


def test_liveness_line_four_shapes():
    now = 1_700_000_000.0

    def iso(epoch: float) -> str:
        return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).isoformat(
            timespec="milliseconds"
        )

    live_hb = {
        "phase": "round 1 running",
        "child_pid": 123,
        "liveness_window_s": 600,
        "last_sign_of_life_at": iso(now - 5),
        "sign_of_life_signal": "event",
    }
    assert liveness_line(live_hb, now=now) == "live: event 5s ago"

    not_live_hb = {
        "phase": "round 1 running",
        "child_pid": 456,
        "liveness_window_s": 600,
        "last_sign_of_life_at": iso(now - 1200),
        "sign_of_life_signal": "event",
        "sign_of_life_evidence": "event assistant 20m ago",
    }
    line = liveness_line(not_live_hb, now=now)
    assert line.startswith("NOT LIVE for")
    assert "round 1 running" in line
    assert "event assistant 20m ago" in line

    no_child_hb = {"phase": "merging into integration", "liveness_window_s": 600}
    assert liveness_line(no_child_hb, now=now) == "no worker child (merging into integration)"

    pre_liveness_hb = {"phase": "round 1 running", "child_pid": 789}
    assert liveness_line(pre_liveness_hb, now=now) == "(no liveness facts)"


def test_liveness_line_cures_exhausted_line():
    driver_pid = os.getpid()
    pgid = os.getpgid(driver_pid)

    below_cap = {"cures": 1, "max_cures_per_generation": 2}
    assert cures_exhausted_line(below_cap, "r1", driver_pid) is None

    at_cap = {"cures": 2, "max_cures_per_generation": 2}
    line = cures_exhausted_line(at_cap, "r1", driver_pid)
    assert line == (
        f"cures exhausted (2/2 this generation) — "
        f"kill -INT -{pgid} then smart-mcps-orchestrate resume r1"
    )


# ---------------------------------------------------------- suspend detection


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _make_monitor(paths, *, gap_s=60.0, interval_s=10.0, mono=0.0, boot=0.0, wall=0.0, log=None):
    mono_clock = _FakeClock(mono)
    boot_clock = _FakeClock(boot)
    wall_clock = _FakeClock(wall)
    monitor = SuspendMonitor(
        paths,
        gap_s=gap_s,
        interval_s=interval_s,
        log=log if log is not None else (lambda message: None),
        clock=mono_clock,
        boottime=boot_clock,
        wall=wall_clock,
    )
    return monitor, mono_clock, boot_clock, wall_clock


def test_suspend_detected_from_monotonic_vs_boottime_divergence(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    monitor, mono_clock, boot_clock, wall_clock = _make_monitor(paths, gap_s=60.0, interval_s=10.0)

    assert monitor.sample() is None  # baseline only

    mono_clock.value += 10.0
    boot_clock.value += 400.0
    wall_clock.value += 10.0

    gap = monitor.sample()
    assert gap == pytest.approx(390.0)


def test_suspend_detected_from_wall_clock_fallback_when_clocks_are_frozen(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    monitor, mono_clock, boot_clock, wall_clock = _make_monitor(paths, gap_s=60.0, interval_s=10.0)

    assert monitor.sample() is None

    wall_clock.value += 200.0  # mono and boot stay frozen (WSL2 path)

    gap = monitor.sample()
    assert gap == pytest.approx(200.0)


def test_no_suspend_when_monotonic_and_boottime_advance_together(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    monitor, mono_clock, boot_clock, wall_clock = _make_monitor(paths, gap_s=60.0, interval_s=10.0)

    assert monitor.sample() is None

    mono_clock.value += 10.0
    boot_clock.value += 10.0
    wall_clock.value += 10.0

    assert monitor.sample() is None


def test_a_detection_writes_last_wake_at_and_increments_suspends(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    logged = []
    monitor, mono_clock, boot_clock, wall_clock = _make_monitor(
        paths, gap_s=60.0, interval_s=10.0, log=logged.append
    )

    assert monitor.sample() is None
    assert read_suspend_facts(paths) is None

    mono_clock.value += 10.0
    boot_clock.value += 400.0
    wall_clock.value += 10.0
    monitor.sample()

    facts = read_suspend_facts(paths)
    assert facts is not None
    assert facts.last_wake_at is not None
    assert facts.suspends == 1
    assert len(logged) == 1
    assert "machine suspend detected" in logged[0]

    mono_clock.value += 10.0
    boot_clock.value += 400.0
    wall_clock.value += 10.0
    monitor.sample()

    facts = read_suspend_facts(paths)
    assert facts.suspends == 2
    assert len(logged) == 2


def test_awake_host_default_monitor_reports_no_suspend():
    paths = RunPaths(Path("/nonexistent-does-not-matter"), "r1")
    log_calls = []
    monitor = SuspendMonitor(paths, gap_s=60.0, interval_s=1.0, log=log_calls.append)

    assert monitor.sample() is None
    time.sleep(1.0)
    assert monitor.sample() is None
    assert log_calls == []


def test_real_clock_oracle_boottime_minus_monotonic_is_stable_across_one_second():
    first = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    time.sleep(1.0)
    second = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    assert abs(second - first) < 0.5


# =========================================================== plan U5


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC).isoformat(
        timespec="milliseconds"
    )


# ------------------------------------------------------- cure predicate


def test_cure_predicate_no_wake_recorded_is_never_a_cure_even_after_two_windows():
    """R7's last sentence: Not Live alone, with no machine wake behind it, is
    never a cure — no matter how long the silence has run."""
    now = 10_000.0
    window = 60.0
    assert (
        should_cure(
            last_wake_at=None,
            last_sign_of_life_at=_iso(now - 3 * window),
            child_spawned_at=_iso(now - 3 * window),
            now=now,
            window_s=window,
        )
        is False
    )


def test_cure_predicate_sign_of_life_after_the_wake_is_never_a_cure():
    now = 10_000.0
    window = 60.0
    wake = now - 2 * window
    assert (
        should_cure(
            last_wake_at=_iso(wake),
            last_sign_of_life_at=_iso(wake + 5),  # life seen after the wake
            child_spawned_at=_iso(wake - 100),
            now=now,
            window_s=window,
        )
        is False
    )


def test_cure_predicate_fires_after_a_whole_window_of_silence_since_the_wake():
    now = 10_000.0
    window = 60.0
    wake = now - window - 1
    assert (
        should_cure(
            last_wake_at=_iso(wake),
            last_sign_of_life_at=_iso(wake - 10),  # last life was before the wake
            child_spawned_at=_iso(wake - 200),
            now=now,
            window_s=window,
        )
        is True
    )


def test_cure_predicate_at_the_cap_kills_nothing_and_logs_exactly_one_exhausted_line(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    hb = RoundHeartbeat(paths, "g1")
    config = LivenessConfig(window_seconds=60, max_cures_per_generation=1)
    now = 10_000.0
    wake = now - 61  # past the window since the wake — otherwise eligible

    def clock() -> float:
        return now

    def activity_provider() -> ChildActivity:
        return _child(
            pid=1,
            spawned_at=_iso(wake - 200),
            last_event_at=_iso(wake - 100),  # too old to count as Sign of Life
            last_event_type="assistant",
        )

    logs: list[str] = []
    killed: list[int] = []

    probe = LivenessProbe(
        hb,
        config,
        activity_provider,
        log=logs.append,
        proc_root=tmp_path / "proc",
        clock=clock,
        suspend_facts_provider=lambda: SuspendFacts(
            last_wake_at=_iso(wake), last_suspend_gap_s=100.0, suspends=1
        ),
        cures_provider=lambda: 1,  # already at the cap (max_cures_per_generation=1)
        kill=lambda pid, sig: killed.append(pid),
    )

    probe.tick()

    assert killed == []
    exhausted = [line for line in logs if "cures exhausted" in line]
    assert len(exhausted) == 1
    assert "1/1" in exhausted[0]

    # A second tick must not repeat the line — logged exactly once.
    probe.tick()
    exhausted = [line for line in logs if "cures exhausted" in line]
    assert len(exhausted) == 1


def test_cure_predicate_below_the_cap_kills_and_calls_on_cure(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    hb = RoundHeartbeat(paths, "g1")
    config = LivenessConfig(window_seconds=60, max_cures_per_generation=2)
    now = 10_000.0
    wake = now - 61

    def clock() -> float:
        return now

    def activity_provider() -> ChildActivity:
        return _child(
            pid=42,
            session_id="cured-session",
            spawned_at=_iso(wake - 200),
            last_event_at=_iso(wake - 100),
            last_event_type="assistant",
        )

    killed: list[tuple[int, int]] = []
    cured_calls: list[tuple[int, int, str]] = []
    registry = ActivityRegistry()
    registry.spawned(42, session_id="cured-session", cwd="/work/g1")

    probe = LivenessProbe(
        hb,
        config,
        activity_provider,
        log=lambda line: None,
        proc_root=tmp_path / "proc",
        clock=clock,
        suspend_facts_provider=lambda: SuspendFacts(
            last_wake_at=_iso(wake), last_suspend_gap_s=100.0, suspends=1
        ),
        cures_provider=lambda: 0,
        on_cure=lambda pid, generation, session_id: cured_calls.append(
            (pid, generation, session_id)
        ),
        activity=registry,
        kill=lambda pid, sig: killed.append((pid, sig)),
    )

    probe.tick()

    assert killed  # SIGTERM reached at least the child itself
    assert killed[0][0] == 42
    assert cured_calls == [(42, 0, "cured-session")]
    assert registry.was_cured(42) is True


# --------------------------------------------------------------- kill_tree


@pytest.mark.skipif(sys.platform != "linux", reason="the real /proc oracle is Linux-only")
def test_kill_tree_signals_the_shell_and_both_sleeps_and_all_three_pids_vanish():
    proc = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60; wait"])
    try:
        time.sleep(0.3)
        parent_pid = proc.pid
        kids = descendants(parent_pid)
        assert kids  # at least one `sleep` found as a descendant before the kill

        signalled = kill_tree(parent_pid, grace_s=5.0)
        assert parent_pid in signalled

        proc.wait(timeout=10)
        time.sleep(0.2)
        for pid in [parent_pid, *[k.pid for k in kids]]:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_kill_tree_skips_a_pid_that_already_exited_without_raising():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)

    signalled = kill_tree(proc.pid, grace_s=1.0)

    assert signalled == []


# ---------------------------------------------------- real kernel cure oracle


@pytest.mark.skipif(sys.platform != "linux", reason="the real /proc oracle is Linux-only")
def test_cure_returns_kill_tree_of_a_real_worker_child_makes_wait_nonzero_and_registry_cured(
    fake_home, tmp_path
):
    """The `_is_same_process`/`kill_tree` interplay g8-6 asks for: a real
    `claude`-shaped child (`fake_claude.py` spawned through `StreamingProcess`,
    exactly the way `SessionRunner._spawn` does it) is killed by `kill_tree`,
    and both halves of what the review loop relies on hold — the stream
    reports a nonzero exit promptly, and the activity registry (marked cured
    the same way the probe would) still answers for the dead pid."""
    script(fake_home, {"result": "OK", "delay_s": 30})
    argv = [*STREAM_ARGV, "--session-id", "66666666-6666-6666-6666-666666666666"]
    env = {**os.environ, "FAKE_CLAUDE_HOME": str(fake_home)}
    grace_s = 2.0

    registry = ActivityRegistry()
    stream = StreamingProcess(argv, cwd=tmp_path, env=env)
    stream.start(prompt="go")
    registry.spawned(stream.pid, session_id="s1", cwd=str(tmp_path))
    time.sleep(0.3)  # let the child actually start sleeping on its delay

    start = time.monotonic()
    kill_tree(stream.pid, grace_s=grace_s)
    registry.mark_cured(stream.pid)
    outcome = stream.wait()
    elapsed = time.monotonic() - start

    assert outcome.returncode != 0
    assert elapsed < grace_s + 2.0
    assert registry.was_cured(stream.pid) is True
