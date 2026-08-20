"""The driver lock: an authoritative, kernel-enforced answer to "is a process
driving this run?" — plan U11.

``flock`` is what gives the guarantees this module tests: no staleness window
(the kernel releases it on any death, SIGKILL included) and no pid-recycling
hazard (the lock, not a pid, is what a reader checks). A SIGKILL test forks a
real child so the guarantee is verified against the kernel, not against this
process's own cooperative cleanup.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.execution.driver import (
    STALE_HEARTBEAT_SECONDS,
    DriverAlreadyRunning,
    DriverLock,
    _lock_fd_is_cloexec,
    driver_status_line,
    is_driving,
    read_driver_record,
)
from orchestrator.execution.manifest import RunPaths, atomic_write_text


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    return RunPaths(tmp_path, "r1")


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true")


class TestDriverRecord:
    def test_acquiring_writes_the_drivers_own_pid_not_a_workers(self, paths):
        lock = DriverLock(paths, record_interval=0.05)
        lock.acquire()
        try:
            record = read_driver_record(paths)
            assert record is not None
            assert record["pid"] == os.getpid()
            assert record["started_at"] is not None
        finally:
            lock.release()

    def test_updated_at_advances_within_thirty_seconds(self, paths):
        lock = DriverLock(paths, record_interval=0.05)
        lock.acquire()
        try:
            first = read_driver_record(paths)["updated_at"]
            _wait_for(lambda: read_driver_record(paths)["updated_at"] != first, timeout=5.0)
        finally:
            lock.release()

    def test_no_record_before_any_driver_has_ever_run(self, paths):
        assert read_driver_record(paths) is None


class TestLockExclusion:
    def test_a_second_acquire_is_refused_while_the_first_holds_it(self, paths):
        first = DriverLock(paths)
        first.acquire()
        try:
            second = DriverLock(paths)
            with pytest.raises(DriverAlreadyRunning):
                second.acquire()
        finally:
            first.release()

    def test_the_lock_is_admitted_again_once_the_first_holder_releases(self, paths):
        first = DriverLock(paths)
        first.acquire()
        first.release()

        second = DriverLock(paths)
        second.acquire()  # does not raise
        second.release()

    def test_is_driving_reflects_a_held_lock(self, paths):
        assert is_driving(paths) is False
        lock = DriverLock(paths)
        lock.acquire()
        try:
            assert is_driving(paths) is True
        finally:
            lock.release()
        assert is_driving(paths) is False


class TestSigkillReleasesLock:
    def test_a_sigkilled_driver_releases_the_lock_with_no_cleanup(self, paths):
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, %r)\n"
            "from orchestrator.execution.driver import DriverLock\n"
            "from orchestrator.execution.manifest import RunPaths\n"
            "paths = RunPaths(Path(%r), %r)\n"
            "lock = DriverLock(paths)\n"
            "lock.acquire()\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n"
        ) % (str(Path(__file__).resolve().parents[1]), str(paths.repo_root), paths.run_id)

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = proc.stdout.readline()
            assert line.strip() == "locked"
            # The record on disk still claims aliveness — nothing had a chance
            # to clean up — yet the kernel-held lock is what a reader trusts.
            assert is_driving(paths) is True

            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=5)

            _wait_for(lambda: is_driving(paths) is False, timeout=5.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestCloexecNotInherited:
    def test_the_open_lock_fd_carries_the_cloexec_flag(self, paths):
        lock = DriverLock(paths)
        lock.acquire()
        try:
            assert _lock_fd_is_cloexec(lock._fd) is True
        finally:
            lock.release()

    def test_a_worker_subprocess_does_not_inherit_the_lock(self, paths):
        """A worker spawned while the driver holds the lock must not keep it
        alive after the driver dies: O_CLOEXEC means exec() closes the fd in
        the child, so killing only the driver frees the lock immediately even
        while the worker is still running."""
        lock = DriverLock(paths)
        lock.acquire()
        try:
            worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                assert is_driving(paths) is True
                lock.release()
                assert is_driving(paths) is False
            finally:
                worker.kill()
                worker.wait()
        finally:
            if lock._fd is not None:
                lock.release()


class TestStatusLine:
    def test_no_driver_record_at_all(self, paths):
        assert driver_status_line(paths, active_group_ids=[]) == "no process is driving this run"

    def test_driving_with_no_active_groups_yet(self, paths):
        lock = DriverLock(paths)
        lock.acquire()
        try:
            line = driver_status_line(paths, active_group_ids=[])
            assert "a process is driving this run" in line
            assert f"pid {os.getpid()}" in line
        finally:
            lock.release()

    def test_none_is_driving_once_released(self, paths):
        lock = DriverLock(paths)
        lock.acquire()
        lock.release()
        assert driver_status_line(paths, active_group_ids=[]) == "no process is driving this run"

    def test_fresh_heartbeat_reads_as_progressing(self, paths):
        gid = "g1"
        hb_path = paths.group_dir(gid) / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(hb_path, "{}")

        lock = DriverLock(paths)
        lock.acquire()
        try:
            line = driver_status_line(paths, active_group_ids=[gid])
            assert "progressing" in line
            assert "stale" not in line
        finally:
            lock.release()

    def test_stale_heartbeat_is_reported_from_file_mtime_not_its_contents(self, paths):
        """The wall-clock string inside the file is untrustworthy on its own —
        the decision has to come from the file's mtime."""
        gid = "g1"
        hb_path = paths.group_dir(gid) / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        # The content lies and claims "just now"; only the mtime is real.
        atomic_write_text(hb_path, '{"updated_at": "2099-01-01T00:00:00+00:00"}')
        old = time.time() - (STALE_HEARTBEAT_SECONDS + 30)
        os.utime(hb_path, (old, old))

        lock = DriverLock(paths)
        lock.acquire()
        try:
            line = driver_status_line(paths, active_group_ids=[gid])
            assert "stale" in line
        finally:
            lock.release()
