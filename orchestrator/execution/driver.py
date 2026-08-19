"""The driver lock: an authoritative answer to "is a process driving this run?"

Before this, the only signal was ``state.json``'s ``live_pids`` — worker
subprocess pids, absent between workers on a perfectly healthy run — and
``os.kill(pid, 0)``, which proves only that *some* process holds that pid, not
that it is the one that recorded it. Neither has a staleness window that closes
cleanly: a crash leaves stale evidence behind for a reader to misjudge, and a
recycled pid produces a false positive with no way to tell.

An advisory ``flock`` has neither problem. The kernel releases it the instant
the holding process exits for any reason, SIGKILL included, so "is it locked"
is always the current truth — no polling window, no interpretation. It also
makes the double-launch guard atomic: acquiring *is* the check, where the old
read-then-decide-then-launch sequence let two near-simultaneous launches both
pass the read.

The lock's fd is opened ``O_CLOEXEC`` and never handed to a worker subprocess.
This process spawns worker subprocesses continuously; an inherited fd would
share the lock's open file description, and closing the driver's own copy
would not release it while the worker still held the inherited one.

The lock proves aliveness, not progress — a wedged driver still holds it. The
driver record's ``updated_at`` and each group's own heartbeat are what answer
"is it actually doing something," which is why both keep being written
alongside the lock rather than being replaced by it.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import threading
import time

from orchestrator.execution.manifest import RunPaths, atomic_write_text

#: How often the driver record's `updated_at` is refreshed. Independent of the
#: heartbeat's tick rate — this is evidence a *driver* is alive, checked at a
#: much coarser grain than "is this group's round still moving".
RECORD_INTERVAL_SECONDS = 10.0

#: Past this many seconds since a heartbeat file's mtime, the driver is read as
#: alive-but-stalled rather than alive-and-progressing (Decisions, U11).
STALE_HEARTBEAT_SECONDS = 120.0


class DriverAlreadyRunning(Exception):
    """Another process already holds the driver lock for this run."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _open_lock_fd(paths: RunPaths) -> int:
    path = paths.driver_lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC at open time, not a later fcntl(F_SETFD) call: a fork()+exec()
    # racing between the two would inherit the fd unprotected for that window.
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o644)


def is_driving(paths: RunPaths) -> bool:
    """Whether some process currently holds the driver lock.

    Tries to take the lock itself, non-blocking: if that succeeds, nothing
    else was holding it, so it is released immediately before returning False.
    If it fails, something else holds it. This is the only correct way to read
    an ``flock`` from outside the process that may hold it — there is no
    "peek" syscall — and it is safe to call from a reader that is not the
    driver, because acquiring and instantly releasing changes nothing durable.
    """
    if not paths.driver_lock_path.is_file():
        return False
    fd = os.open(paths.driver_lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def read_driver_record(paths: RunPaths) -> dict | None:
    """The last-written driver record, or None if there has never been one."""
    try:
        payload = json.loads(paths.driver_record_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def newest_heartbeat_mtime(paths: RunPaths, group_ids: list[str]) -> float | None:
    """The most recent mtime among the named groups' heartbeat files, or None
    when none of them have written one yet (e.g. before any group has started).
    """
    mtimes = []
    for gid in group_ids:
        hb_path = paths.group_dir(gid) / "heartbeat.json"
        with contextlib.suppress(OSError):
            mtimes.append(hb_path.stat().st_mtime)
    return max(mtimes) if mtimes else None


class DriverLock:
    """Held by the process actually driving a run, for that process's lifetime.

    Use as a context manager so ``release`` runs even on an exception path:

        with DriverLock(paths):
            ...run the scheduler...

    ``acquire`` raises ``DriverAlreadyRunning`` rather than blocking — a second
    driver for the same run is an operator error (or a double-launched job),
    not a queue to wait in.
    """

    def __init__(self, paths: RunPaths, *, record_interval: float = RECORD_INTERVAL_SECONDS):
        self.paths = paths
        self.record_interval = record_interval
        self._fd: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: str | None = None

    def acquire(self) -> None:
        fd = _open_lock_fd(self.paths)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise DriverAlreadyRunning(
                f"run {self.paths.run_id} is already being driven by another process"
            ) from exc
        self._fd = fd
        self._started_at = _now()
        self._write_record()
        self._thread = threading.Thread(target=self._loop, name="driver-lock", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        if self._fd is not None:
            fd, self._fd = self._fd, None
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def __enter__(self) -> DriverLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def _loop(self) -> None:
        while not self._stop.wait(self.record_interval):
            self._write_record()

    def _write_record(self) -> None:
        """Best-effort, like the heartbeat: an unwritable run directory loses
        the evidence, not the run — the lock itself is still held either way."""
        try:
            record = {
                "pid": os.getpid(),
                "started_at": self._started_at,
                "updated_at": _now(),
            }
            atomic_write_text(self.paths.driver_record_path, json.dumps(record, indent=2) + "\n")
        except Exception:  # noqa: BLE001 - evidence is never worth the run
            pass


def driver_status_line(paths: RunPaths, *, active_group_ids: list[str]) -> str:
    """One human-readable line for `status`: whether a process is driving, and
    separately whether it looks like it is making progress.

    Progress is read from the freshest heartbeat mtime among the run's
    currently-active groups, not from the driver record's own `updated_at` —
    the record advances every tick just because the process is alive, which is
    exactly the "wedged but alive" case this is meant to catch instead of hide.
    """
    if not is_driving(paths):
        return "no process is driving this run"
    record = read_driver_record(paths)
    pid = record.get("pid") if record else None
    who = f"pid {pid}" if pid is not None else "unknown pid"
    mtime = newest_heartbeat_mtime(paths, active_group_ids)
    if mtime is None:
        return f"a process is driving this run ({who})"
    age = time.time() - mtime
    if age > STALE_HEARTBEAT_SECONDS:
        return f"a process is driving this run ({who}), but its heartbeat is stale ({age:.0f}s old)"
    return f"a process is driving this run ({who}), progressing ({age:.0f}s since last heartbeat)"


def _lock_fd_is_cloexec(fd: int) -> bool:
    """Test hook: whether an open fd carries FD_CLOEXEC. Not used by the lock
    itself — O_CLOEXEC is set at open time — this exists so a test can assert
    the property directly instead of trusting the open() flag was honoured."""
    return bool(fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
