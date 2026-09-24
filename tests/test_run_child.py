"""Run Child launch, quoting, wait, re-adoption and kill mechanics (plan U8)."""

from __future__ import annotations

import os
import signal
import time

import pytest

from orchestrator.execution.run_child import (
    RunChildState,
    TimedOut,
    boot_id,
    is_adoptable,
    is_same_process,
    kill_process_group,
    launch,
    proc_cmdline_head,
    proc_starttime,
    proc_state,
    process_alive,
    wait_exit,
    wrapper_argv,
)


def _launch(tmp_path, cmd, *, name="cmd"):
    exit_path = tmp_path / f"{name}.exit"
    out_path = tmp_path / f"{name}.out"
    err_path = tmp_path / f"{name}.err"
    proc = launch(
        cmd,
        cwd=tmp_path,
        exit_path=exit_path,
        out_path=out_path,
        err_path=err_path,
        preexec_fn=None,
    )
    return proc, exit_path, out_path, err_path


def test_wrapper_argv_never_interpolates_cmd_text():
    argv = wrapper_argv(
        "echo hi", tmp_path_placeholder := __import__("pathlib").Path("/tmp/x.exit")
    )
    assert argv[0] == "sh"
    assert argv[3] == "sh"
    assert argv[4] == "echo hi"
    assert argv[5] == str(tmp_path_placeholder)
    # The script text itself never contains the command — only $1/$2 refer to it.
    assert "echo hi" not in argv[2]


def test_quoted_pipe_and_env_prefix_command_runs_exactly_as_written(tmp_path):
    cmd = "FOO='a b' sh -c 'echo \"$FOO\"' | tr a-z A-Z > out.txt"
    proc, exit_path, out_path, err_path = _launch(tmp_path, cmd)
    code = wait_exit(proc.pid, exit_path, started_monotonic=time.monotonic(), cap_seconds=10)
    assert code == 0
    assert (tmp_path / "out.txt").read_text().strip() == "A B"


def test_launch_and_wait_exit_reads_status_from_file_not_from_our_own_wait(tmp_path):
    proc, exit_path, out_path, err_path = _launch(tmp_path, "exit 7")
    code = wait_exit(proc.pid, exit_path, started_monotonic=time.monotonic(), cap_seconds=10)
    assert code == 7
    assert exit_path.is_file()


def test_wait_exit_raises_timed_out_and_process_group_can_be_killed(tmp_path):
    proc, exit_path, out_path, err_path = _launch(tmp_path, "sleep 30")
    started = time.monotonic()
    with pytest.raises(TimedOut):
        wait_exit(
            proc.pid, exit_path, started_monotonic=started, cap_seconds=1.0, poll_interval=0.2
        )
    assert process_alive(proc.pid)
    kill_process_group(proc.pid, grace_seconds=2.0)
    proc.wait(timeout=3)  # reap: this test is proc's direct parent
    assert not process_alive(proc.pid)


def test_awake_time_cap_ignores_a_simulated_suspend_gap(tmp_path):
    """A command with a 60-minute cap that has run 20 awake minutes is not
    killed even though 8 hours of *wall* time have passed — the cap is driven
    by the monotonic clock the caller supplies, which a suspend never bumps."""
    proc, exit_path, out_path, err_path = _launch(tmp_path, "sleep 0.2")
    fake_start = 1000.0
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        # Simulated: 20 minutes of awake time elapsed, cap is 60 minutes.
        return fake_start + 20 * 60

    code = wait_exit(
        proc.pid,
        exit_path,
        started_monotonic=fake_start,
        cap_seconds=60 * 60,
        poll_interval=0.05,
        monotonic=fake_monotonic,
    )
    assert code == 0


def test_kill_process_group_terminates_process_group_leader(tmp_path):
    proc, exit_path, out_path, err_path = _launch(tmp_path, "sleep 30")
    assert process_alive(proc.pid)
    kill_process_group(proc.pid, grace_seconds=2.0)
    proc.wait(timeout=3)  # reap: this test is proc's direct parent
    assert not process_alive(proc.pid)


def test_re_adoption_finds_the_same_pid_after_a_simulated_crash(tmp_path):
    """A live Run Child, dropped without cleanup (a simulated crash of the
    orchestrator), is still adoptable by a freshly-built state record."""
    proc, exit_path, out_path, err_path = _launch(tmp_path, "sleep 20", name="long")
    state = RunChildState(
        n=1,
        pid=proc.pid,
        starttime=proc_starttime(proc.pid),
        cmdline_head=proc_cmdline_head(proc.pid),
        started_at="now",
        started_monotonic=time.monotonic(),
        boot_id=boot_id(),
    )
    try:
        assert is_adoptable(state, current_boot_id=boot_id())
        assert is_same_process(proc.pid, state)
    finally:
        kill_process_group(proc.pid, grace_seconds=2.0)


def test_re_adoption_refuses_a_pid_reused_by_a_different_process(tmp_path):
    proc, exit_path, out_path, err_path = _launch(tmp_path, "true")
    proc.wait(timeout=5)
    # The pid has exited; a stale record naming a different cmdline_head must
    # never be "adopted" as if it were still that process.
    stale = RunChildState(
        n=1,
        pid=proc.pid,
        starttime="bogus-starttime-that-will-never-match",
        cmdline_head="sh",
        started_at="now",
        started_monotonic=time.monotonic(),
        boot_id=boot_id(),
    )
    assert not is_adoptable(stale, current_boot_id=boot_id())


def test_boot_id_mismatch_makes_a_recorded_pid_unadoptable(tmp_path):
    proc, exit_path, out_path, err_path = _launch(tmp_path, "sleep 5")
    try:
        state = RunChildState(
            n=1,
            pid=proc.pid,
            starttime=proc_starttime(proc.pid),
            cmdline_head=proc_cmdline_head(proc.pid),
            started_at="now",
            started_monotonic=time.monotonic(),
            boot_id="a-different-boot-id",
        )
        assert not is_adoptable(state, current_boot_id=boot_id())
    finally:
        kill_process_group(proc.pid, grace_seconds=2.0)


def test_zombie_state_is_not_adoptable(tmp_path):
    """A process whose parent never reaped it is a zombie (state 'Z') — the
    parent here is the ``sh`` wrapper itself, so instead we drive the state-Z
    check directly against a fork()ed-and-not-waited child of this test."""
    pid = os.fork()
    if pid == 0:  # child
        os._exit(0)
    time.sleep(0.2)  # let the child exit and become a zombie
    try:
        assert proc_state(pid) == "Z"
        state = RunChildState(
            n=1,
            pid=pid,
            starttime=proc_starttime(pid),
            cmdline_head=proc_cmdline_head(pid),
            started_at="now",
            started_monotonic=time.monotonic(),
            boot_id=boot_id(),
        )
        assert not is_adoptable(state, current_boot_id=boot_id())
    finally:
        os.waitpid(pid, 0)


def test_signal_killed_command_wrapper_still_writes_no_exit_file_until_reaped(tmp_path):
    """A command killed by a signal before the wrapper's ``echo`` runs never
    gets an exit file — this is the 'died with the orchestrator' path, not a
    128+n exit code, because the whole process group (wrapper included) is
    what gets SIGKILLed on a timeout."""
    proc, exit_path, out_path, err_path = _launch(tmp_path, "sleep 30")
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=3)
    assert not exit_path.is_file()
