"""Round heartbeat: facts on disk, and none of them a verdict (plan P3).

Three properties matter here. The file carries when the round started and keeps
saying when it last looked — that is the whole signal a reader needs to infer a
stall for itself. It never names the inference, because a persisted "stalled"
field is a state in everything but name. And it can never take a round down with
it: an unwritable directory loses the evidence, not the work.
"""

from __future__ import annotations

import json
import time

import pytest

from orchestrator.execution.heartbeat import (
    HEARTBEAT_NAME,
    RoundHeartbeat,
    heartbeat_path,
    read_heartbeat,
)
from orchestrator.execution.manifest import RunPaths

# Anything that would turn evidence into a de facto state.
FORBIDDEN_KEYS = ("stalled", "stall", "hung", "hang", "stuck", "is_alive", "healthy")


def _paths(tmp_path) -> RunPaths:
    return RunPaths(tmp_path, "r1")


def _read(paths: RunPaths) -> dict:
    return json.loads(heartbeat_path(paths, "g1").read_text())


def test_mark_round_writes_round_started_at_and_the_round_number(tmp_path):
    paths = _paths(tmp_path)
    hb = RoundHeartbeat(paths, "g1")
    hb.mark_round(generation=2, round_no=3)

    payload = _read(paths)
    assert payload["group_id"] == "g1"
    assert payload["generation"] == 2
    assert payload["round"] == 3
    assert payload["round_started_at"] is not None
    assert payload["updated_at"] >= payload["round_started_at"]


def test_a_new_round_moves_round_started_at_forward(tmp_path):
    paths = _paths(tmp_path)
    hb = RoundHeartbeat(paths, "g1")
    hb.mark_round(generation=1, round_no=1)
    first = _read(paths)["round_started_at"]
    time.sleep(0.01)
    hb.mark_round(generation=1, round_no=2)
    second = _read(paths)

    assert second["round"] == 2
    assert second["round_started_at"] > first


def test_the_daemon_thread_keeps_updated_at_advancing_during_a_round(tmp_path):
    """The point of the thread: a round that blocks for an hour still ticks."""
    paths = _paths(tmp_path)
    hb = RoundHeartbeat(paths, "g1", interval=0.01)
    hb.start()
    try:
        assert hb._thread is not None and hb._thread.daemon  # cannot hold the process open
        hb.mark_round(generation=1, round_no=1)
        started = _read(paths)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            later = _read(paths)
            if later["updated_at"] > started["updated_at"]:
                break
            time.sleep(0.01)
        else:  # pragma: no cover - only on a wedged writer
            pytest.fail("updated_at never advanced")
    finally:
        hb.stop()

    # The round itself did not restart just because the writer ticked.
    assert later["round_started_at"] == started["round_started_at"]
    assert later["round"] == 1


def test_an_unwritable_run_directory_cannot_fail_the_round(tmp_path):
    paths = _paths(tmp_path)
    # A plain file where the group directory should be: mkdir and write both fail.
    paths.group_dir("g1").parent.mkdir(parents=True, exist_ok=True)
    paths.group_dir("g1").write_text("not a directory")

    hb = RoundHeartbeat(paths, "g1", interval=0.01)
    hb.start()
    hb.mark_round(generation=1, round_no=1)  # must not raise
    hb.stop()

    assert read_heartbeat(paths, "g1") is None


def test_the_record_never_names_a_stall(tmp_path):
    paths = _paths(tmp_path)
    hb = RoundHeartbeat(paths, "g1")
    hb.mark_round(generation=1, round_no=1)

    text = heartbeat_path(paths, "g1").read_text().lower()
    payload = _read(paths)
    for key in FORBIDDEN_KEYS:
        assert key not in payload
        assert key not in text


def test_read_heartbeat_is_none_for_a_run_that_predates_the_file(tmp_path):
    paths = _paths(tmp_path)
    paths.group_dir("g1").mkdir(parents=True)
    assert read_heartbeat(paths, "g1") is None


def test_read_heartbeat_is_none_for_a_torn_file(tmp_path):
    paths = _paths(tmp_path)
    paths.group_dir("g1").mkdir(parents=True)
    (paths.group_dir("g1") / HEARTBEAT_NAME).write_text("{not json")
    assert read_heartbeat(paths, "g1") is None


# ------------------------------------------------------- periodic log presence


def test_phase_is_recorded_and_survives_into_the_snapshot(tmp_path):
    paths = _paths(tmp_path)
    hb = RoundHeartbeat(paths, "g1")
    hb.mark_phase("forking the base session")

    payload = hb.snapshot()
    assert payload["phase"] == "forking the base session"
    assert payload["phase_elapsed_s"] >= 0
    # Still evidence, not a verdict.
    assert not any(key in payload for key in FORBIDDEN_KEYS)


def test_starting_a_round_supersedes_the_previous_phase(tmp_path):
    hb = RoundHeartbeat(_paths(tmp_path), "g1")
    hb.mark_phase("forking the base session")
    hb.mark_round(generation=1, round_no=1)
    assert hb.snapshot()["phase"] == "running"


def test_a_long_phase_emits_periodic_log_lines_naming_what_it_is_doing(tmp_path):
    """The 21-minute silence this exists for: a group mid-fork must say so."""
    lines: list[str] = []
    hb = RoundHeartbeat(
        _paths(tmp_path), "g1", interval=0.01, log=lines.append, log_interval=0.05
    )
    hb.mark_phase("forking the base session")
    hb.start()
    try:
        deadline = time.monotonic() + 3.0
        while not lines and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        hb.stop()

    assert lines, "a long phase must produce at least one line"
    assert "still forking the base session" in lines[0]
    assert "group g1" in lines[0]
    assert "elapsed" in lines[0]
    # A phase report is not a stall verdict.
    assert not any(word in lines[0] for word in ("stalled", "stuck", "hung"))


def test_log_lines_are_rate_limited_far_below_the_file_tick(tmp_path):
    lines: list[str] = []
    hb = RoundHeartbeat(_paths(tmp_path), "g1", interval=0.01, log=lines.append, log_interval=30.0)
    hb.mark_phase("forking the base session")
    hb.start()
    try:
        time.sleep(0.3)  # ~30 file ticks
    finally:
        hb.stop()
    assert lines == [], "a 30s log interval must not fire within 0.3s of ticking"
    assert read_heartbeat(_paths(tmp_path), "g1") is not None  # the file still ticked


def test_a_failing_log_sink_never_takes_the_heartbeat_down(tmp_path):
    def boom(_line: str) -> None:
        raise RuntimeError("sink is broken")

    paths = _paths(tmp_path)
    hb = RoundHeartbeat(paths, "g1", interval=0.01, log=boom, log_interval=0.01)
    hb.mark_phase("forking the base session")
    hb.start()
    try:
        time.sleep(0.15)
    finally:
        hb.stop()
    # The file kept being written despite every log call raising.
    assert read_heartbeat(paths, "g1") is not None
