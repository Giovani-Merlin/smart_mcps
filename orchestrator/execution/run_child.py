"""Run Child launch, re-adoption and wall-clock cap mechanics (plan U8).

A Run Child is a detached subprocess (``start_new_session=True``) launched
through a positional-argument wrapper so the command text is never
interpolated into a shell script: ``sh -c 'sh -c "$1"; rc=$?; echo "$rc" >
"$2.tmp" && mv "$2.tmp" "$2"' sh <cmd> <exit_file>``. Its exit status is
read from that exit file, written atomically (write-then-rename), never from
this process's own ``wait()`` — a re-adopted child was never our fork child,
so we cannot ``wait()`` it at all.

Re-adoption identifies the *same* process the way the scheduler already does
for worker subprocesses (``_is_same_process`` in ``scheduler.py``): the
kernel process start time (field 22 of ``/proc/<pid>/stat``) must match, and
argv[0]'s basename must match. On top of that a Run Child also checks the
recorded ``boot_id`` (a pid + start time pair can theoretically recur across
a reboot on a long-lived host) and that ``/proc/<pid>/stat``'s state is not
``Z`` — a zombie has exited but not yet been reaped by PID 1, and counts as
gone, not adoptable.

The wall-clock cap measures **awake** time (``time.monotonic()`` deltas),
never a ``/proc``-derived boot-time clock, so a laptop suspend does not count
against a long render (deepen edge case).
"""

from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long

# x86_64 syscall number; pidfd_open landed in Linux 5.3.
_SYS_PIDFD_OPEN = 434

DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_KILL_GRACE_S = 5.0


class RunChildState(BaseModel):
    """What is persisted to ``attempt-<k>/<n>.state.json`` at launch — never
    to ``live_pids``, which is the worker-subprocess registry the scheduler
    reaps unconditionally on a crashed-orchestrator restart."""

    n: int
    pid: int
    starttime: str | None = None
    cmdline_head: str | None = None
    started_at: str
    started_monotonic: float
    boot_id: str | None = None


def boot_id() -> str | None:
    """A per-boot identifier, or ``None`` when unreadable (non-Linux, or the
    file is hidden by an outer sandbox) — a record with no boot id is never
    rejected for that reason alone."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return value or None


def proc_cmdline_head(pid: int) -> str | None:
    """argv[0] of ``/proc/<pid>/cmdline``, or ``None`` when unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    head = raw.split(b"\0", 1)[0].decode(errors="replace")
    return head or None


def proc_starttime(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat``, or ``None`` when unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    return fields[19] if len(fields) > 19 else None


def proc_state(pid: int) -> str | None:
    """The single-character state field (field 3) of ``/proc/<pid>/stat`` —
    ``"Z"`` is a zombie: exited but not yet reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    return fields[0] if fields else None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def is_same_process(pid: int, state: RunChildState) -> bool:
    """Mirrors ``scheduler._is_same_process``: same kernel start time, same
    argv[0] basename."""
    head = proc_cmdline_head(pid)
    if head is None:
        return False
    if state.starttime is not None and proc_starttime(pid) != state.starttime:
        return False
    if state.cmdline_head is not None:
        return Path(head).name == Path(state.cmdline_head).name
    return True


def is_adoptable(state: RunChildState, *, current_boot_id: str | None) -> bool:
    """Whether the pid recorded in ``state`` may be re-adopted as the Run
    Child that survived a crashed orchestrator."""
    if (
        state.boot_id is not None
        and current_boot_id is not None
        and state.boot_id != current_boot_id
    ):
        return False
    if not is_same_process(state.pid, state):
        return False
    return proc_state(state.pid) != "Z"


def wrapper_argv(cmd: str, exit_path: Path) -> list[str]:
    """The launch wrapper's argv. ``cmd`` and ``exit_path`` are passed as
    positional arguments (``$1``/``$2``), never interpolated into the wrapper
    script text, so quoting inside ``cmd`` is inert to the wrapper itself."""
    script = 'sh -c "$1"; rc=$?; echo "$rc" > "$2.tmp" && mv "$2.tmp" "$2"'
    return ["sh", "-c", script, "sh", cmd, str(exit_path)]


def launch(
    cmd: str,
    *,
    cwd: Path,
    exit_path: Path,
    out_path: Path,
    err_path: Path,
    preexec_fn: Callable[[], None] | None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Launch one Run Child. Detached (``start_new_session=True``) so it
    survives the orchestrator process; its exit status is never read from
    this ``Popen`` (see module docstring)."""
    exit_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as out_f, err_path.open("wb") as err_f:
        return subprocess.Popen(
            wrapper_argv(cmd, exit_path),
            cwd=str(cwd),
            stdout=out_f,
            stderr=err_f,
            start_new_session=True,
            preexec_fn=preexec_fn,
            env=env,
            close_fds=True,
        )


def pidfd_open(pid: int) -> int:
    fd = _LIBC.syscall(_SYS_PIDFD_OPEN, pid, 0)
    if fd < 0:
        raise OSError(f"pidfd_open({pid}) failed")
    return fd


class TimedOut(Exception):
    """The command exceeded its wall-clock cap and was killed."""


def wait_exit(
    pid: int,
    exit_path: Path,
    *,
    started_monotonic: float,
    cap_seconds: float,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    monotonic: Callable[[], float] = time.monotonic,
) -> int | None:
    """Block until ``exit_path`` appears (returns the exit code) or the
    **awake** cap is exceeded (raises ``TimedOut``). Returns ``None`` when
    the process vanished with no exit file — it "died with the orchestrator"
    (killed out from under the wrapper, e.g. by a host reboot) rather than
    completing on its own.

    Prefers ``pidfd_open`` + ``select`` (works for a process this orchestrator
    did not fork, e.g. a re-adopted child); falls back to a plain sleep-poll
    when ``pidfd_open`` is unavailable. The exit status always comes from the
    exit file, never from the pidfd wait itself.
    """
    fd: int | None
    try:
        fd = pidfd_open(pid)
    except OSError:
        fd = None
    try:
        while True:
            if exit_path.is_file():
                text = exit_path.read_text().strip()
                return int(text) if text else None
            elapsed = monotonic() - started_monotonic
            if elapsed > cap_seconds:
                raise TimedOut(f"exceeded wall_clock_min cap of {cap_seconds / 60:.2f} minutes")
            if fd is not None:
                select.select([fd], [], [], poll_interval)
            else:
                time.sleep(poll_interval)
                if not process_alive(pid) and not exit_path.is_file():
                    return None
    finally:
        if fd is not None:
            os.close(fd)


def kill_process_group(pid: int, *, grace_seconds: float = DEFAULT_KILL_GRACE_S) -> None:
    """SIGTERM the Run Child's process group (it is its own session/group
    leader, via ``start_new_session=True``), then SIGKILL after a grace
    period if it has not exited."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
