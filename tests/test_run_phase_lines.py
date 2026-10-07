"""Run-phase lines: every Run Child command writes one ``run.log`` line."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.execution.scheduler import GroupFailure, GroupState
from tests.test_run_executor import (  # noqa: F401  (fake_claude_home is autouse)
    _run,
    fake_claude_home,
    make_deps,
    make_group,
    repo,
)


def _log(deps) -> str:
    return (deps.store.paths.run_dir / "logs" / "run.log").read_text()


def _in_order(log: str, *needles: str) -> None:
    pos = 0
    for needle in needles:
        found = log.find(needle, pos)
        assert found >= 0, f"{needle!r} missing or out of order in:\n{log}"
        pos = found + len(needle)


def test_two_command_unit_logs_attempt_and_each_command(tmp_path, repo):
    args = {
        "commands": [{"cmd": "true", "wall_clock_min": 1}, {"cmd": "true", "wall_clock_min": 1}]
    }
    deps = make_deps(repo, tmp_path / "run", repo)

    state, _ = asyncio.run(_run(deps, make_group(args)))

    assert state == GroupState.COMPLETED
    _in_order(
        _log(deps),
        "run recipe attempt 1 starting at command 1/2",
        "command 1/2: exit 0 (",
        "command 2/2: exit 0 (",
    )


def test_failing_command_logs_phase_before_failure_and_retry_names_its_start(tmp_path, repo):
    args = {
        "commands": [{"cmd": "true", "wall_clock_min": 1}, {"cmd": "exit 3", "wall_clock_min": 1}]
    }
    deps = make_deps(
        repo,
        tmp_path / "run",
        repo,
        triage=lambda p: {"verdict": "work_failure", "diagnosis": "command 2 failed"},
    )

    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, make_group(args)))

    _in_order(_log(deps), "command 2/2: exit 3 (", "command 2/2 ('exit 3') exited 3")

    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, make_group(args), generation=1))
    assert "attempt 2 starting at command 2/2" in _log(deps)
