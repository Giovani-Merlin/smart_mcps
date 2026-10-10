"""Mutual exclusion between the merge gate and driver data steps.

``[workspace] data_dirs`` are symlinked into every worktree from the single copy
under the repo root, so a driver step that writes ``data/corpus.db`` is visible
to a gate running the server tests over it instantly (r20261007-100412, ESC
fedc4a348855). One run-level ``flock`` — ``<run_dir>/data-step.lock`` — is held
by ``merge_group`` around the preflight and by ``run_data_step`` around the
driver's command, so the two never overlap. The kernel drops an ``flock`` on any
process death, so a killed data step cannot wedge the gate.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from orchestrator.config import WorkspaceConfig
from orchestrator.execution.manifest import RunPaths, atomic_write_text, log_event

#: Seconds between "still waiting" lines. Module-level so a test can shrink it.
DATA_STEP_REMINDER_S = 300
_POLL_S = 0.1


def _now() -> float:
    return time.time()


def _record_cmd(paths: RunPaths) -> str:
    try:
        payload = json.loads(paths.data_step_record_path.read_text())
    except (OSError, ValueError):
        return "unknown step"
    cmd = payload.get("cmd") if isinstance(payload, dict) else None
    return str(cmd) if cmd else "unknown step"


def _format_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


@contextlib.contextmanager
def _locked(
    paths: RunPaths,
    on_wait: Callable[[], None],
    on_remind: Callable[[float], None],
) -> Iterator[None]:
    """Hold the exclusive lock; call ``on_wait`` once if it is not free at once
    and ``on_remind(age_s)`` every ``DATA_STEP_REMINDER_S`` until it is. No upper
    bound on the wait, by decision."""
    path = paths.data_step_lock_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC: a child process must not inherit (and so keep holding) the lock.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            on_wait()
            started = last_reminder = time.monotonic()
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    time.sleep(_POLL_S)
                now = time.monotonic()
                if now - last_reminder >= DATA_STEP_REMINDER_S:
                    last_reminder = now
                    on_remind(now - started)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


@contextlib.contextmanager
def hold_data_step_lock(paths: RunPaths, label: str, log: Callable[[str], None]) -> Iterator[None]:
    """Held by the merge gate around its checks. ``label`` prefixes the log
    lines (``group g3: gate``)."""

    def _wait() -> None:
        log(f"{label} waiting for driver data step ({_record_cmd(paths)})")

    def _remind(age: float) -> None:
        log(
            f"{label} still waiting for driver data step ({_record_cmd(paths)}, {_format_age(age)})"
        )

    with _locked(paths, _wait, _remind):
        yield


def run_data_step(repo_root: Path, run_id: str, cmd: str) -> int:
    """Run ``cmd`` (``sh -c``, in the repo root, output passed through) while
    holding the data-step lock; returns the command's exit status."""
    paths = RunPaths(repo_root, run_id)

    def log(text: str) -> None:
        log_event(paths, text)

    with _locked(
        paths,
        lambda: log("data step waiting for the merge gate"),
        lambda age: log(f"data step still waiting for the merge gate ({_format_age(age)})"),
    ):
        atomic_write_text(
            paths.data_step_record_path,
            json.dumps({"cmd": cmd, "pid": os.getpid(), "started_at": _now()}) + "\n",
        )
        log(f"data step started: {cmd}")
        try:
            code = subprocess.run(["sh", "-c", cmd], cwd=repo_root, check=False).returncode
        finally:
            paths.data_step_record_path.unlink(missing_ok=True)
        log(f"data step finished (exit {code}): {cmd}")
        return code


def data_dirs_gate_warning(workspace: WorkspaceConfig | None, run_id: str) -> str | None:
    """The launch-time warning when data dirs are shared live with the gate."""
    if workspace is None or not workspace.data_dirs:
        return None
    return (
        f"data layer: [workspace] data_dirs {', '.join(workspace.data_dirs)} are read live by "
        "the merge gate — run every driver data step through: "
        f"smart-mcps-orchestrate data-step {run_id} -- <cmd>"
    )
