"""The data-step lock: the merge gate and driver data steps never overlap."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from orchestrator.config import WorkspaceConfig
from orchestrator.execution import data_step
from orchestrator.execution.data_step import (
    data_dirs_gate_warning,
    hold_data_step_lock,
    run_data_step,
)
from orchestrator.execution.manifest import RunPaths

RUN = "r-ds"


def _spawn_step(repo, cmd: str) -> subprocess.Popen:
    code = (
        "import sys; from pathlib import Path; "
        "from orchestrator.execution.data_step import run_data_step; "
        f"sys.exit(run_data_step(Path(sys.argv[1]), {RUN!r}, sys.argv[2]))"
    )
    return subprocess.Popen([sys.executable, "-c", code, str(repo), cmd])


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_gate_blocks_until_a_running_data_step_ends(tmp_path):
    paths = RunPaths(tmp_path, RUN)
    proc = _spawn_step(tmp_path, "sleep 2")
    try:
        _wait_for(paths.data_step_record_path.is_file)
        lines: list[str] = []
        started = time.monotonic()
        with hold_data_step_lock(paths, "group g1: gate", lines.append):
            assert time.monotonic() - started >= 1.0
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
    waits = [line for line in lines if "still" not in line]
    assert waits == ["group g1: gate waiting for driver data step (sleep 2)"]
    assert not paths.data_step_record_path.exists()
    log = paths.event_log_path.read_text()
    assert "data step started: sleep 2" in log
    assert "data step finished (exit 0): sleep 2" in log


def test_data_step_blocks_while_the_gate_holds_the_lock(tmp_path):
    paths = RunPaths(tmp_path, RUN)
    result: list[int] = []
    with hold_data_step_lock(paths, "group g1: gate", lambda _t: None):
        thread = threading.Thread(
            target=lambda: result.append(run_data_step(tmp_path, RUN, "true"))
        )
        thread.start()
        _wait_for(
            lambda: (
                paths.event_log_path.is_file()
                and "data step waiting for the merge gate" in paths.event_log_path.read_text()
            )
        )
        assert result == []
        assert not paths.data_step_record_path.exists()
    thread.join(timeout=10)
    assert result == [0]
    assert paths.event_log_path.read_text().count("data step waiting for the merge gate") == 1


def test_record_is_present_during_the_step_and_exit_status_is_returned(tmp_path):
    paths = RunPaths(tmp_path, RUN)
    seen: list[bool] = []
    probe = f"test -f {paths.data_step_record_path}"
    assert run_data_step(tmp_path, RUN, probe) == 0
    assert run_data_step(tmp_path, RUN, "exit 3") == 3
    assert not paths.data_step_record_path.exists()
    assert "data step finished (exit 3): exit 3" in paths.event_log_path.read_text()
    assert seen == []


def test_reminder_repeats_while_the_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(data_step, "DATA_STEP_REMINDER_S", 0.2)
    paths = RunPaths(tmp_path, RUN)
    proc = _spawn_step(tmp_path, "sleep 1.5")
    try:
        _wait_for(paths.data_step_record_path.is_file)
        lines: list[str] = []
        with hold_data_step_lock(paths, "group g1: gate", lines.append):
            pass
    finally:
        proc.kill()
    first = [line for line in lines if "still waiting" not in line]
    reminders = [line for line in lines if "still waiting" in line]
    assert len(first) == 1
    assert len(reminders) >= 2
    assert all("sleep 1.5" in line for line in reminders)


def test_a_killed_data_step_releases_the_lock(tmp_path):
    paths = RunPaths(tmp_path, RUN)
    proc = _spawn_step(tmp_path, "sleep 600")
    try:
        _wait_for(paths.data_step_record_path.is_file)
    finally:
        proc.kill()
        proc.wait()
    lines: list[str] = []
    with hold_data_step_lock(paths, "group g1: gate", lines.append):
        pass
    assert lines == []


def test_data_dirs_gate_warning(tmp_path):
    assert data_dirs_gate_warning(None, RUN) is None
    assert data_dirs_gate_warning(WorkspaceConfig(), RUN) is None
    text = data_dirs_gate_warning(WorkspaceConfig(data_dirs=["data", "models"]), RUN)
    assert text is not None
    assert "data, models" in text and f"data-step {RUN} --" in text


@pytest.fixture(autouse=True)
def _quiet_poll(monkeypatch):
    monkeypatch.setattr(data_step, "_POLL_S", 0.02)
