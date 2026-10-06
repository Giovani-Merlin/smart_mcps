"""The verification-omission gate (plan U6): a `completed` report whose last
edit has no later verify action runs the check command before review."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import PreflightConfig
from orchestrator.execution.round_signals import RoundSignals
from tests.test_review_loop import (
    Harness,
    StubRunner,
    coder_report,
    make_group,
    run_log_lines,
    verdict,
)

CODER = "r1-g1-coder-g1"
REVIEWER = "r1-g1-reviewer-g1"


def _call(tool: str, ident: str, **tool_input) -> dict:
    block = {"type": "tool_use", "id": ident, "name": tool, "input": tool_input}
    return {"type": "assistant", "message": {"content": [block]}}


def _result(ident: str) -> dict:
    block = {"type": "tool_result", "tool_use_id": ident, "content": "ok"}
    return {"type": "user", "message": {"content": [block]}}


class SignalRunner(StubRunner):
    """A StubRunner whose rounds carry a scripted `RoundSignals`."""

    def __init__(self, scripts, calls: list[list[tuple[str, dict]]]):
        super().__init__(scripts)
        self.round_calls = list(calls)

    def _round(self, session_id):
        result = super()._round(session_id)
        if session_id == self.session_ids.get(CODER) and self.round_calls:
            signals = RoundSignals()
            for n, (tool, tool_input) in enumerate(self.round_calls.pop(0)):
                signals.observe(_call(tool, f"t{n}", **tool_input))
                signals.observe(_result(f"t{n}"))
            result = dataclasses.replace(result, signals=signals)
        return result


def _git_workspace(harness: Harness) -> None:
    subprocess.run(["git", "init", "-q"], cwd=harness.workspace, check=True)


def _harness(tmp_path: Path, runner, check: list[str]) -> Harness:
    harness = Harness(tmp_path, runner)
    _git_workspace(harness)
    harness.deps.preflight_config = PreflightConfig(check_command=check)
    return harness


EDIT_THEN_READ = [("Edit", {"file_path": "a.py"}), ("Read", {"file_path": "b.py"})]
EDIT_THEN_PYTEST = [("Edit", {"file_path": "a.py"}), ("Bash", {"command": "pytest -q"})]


@pytest.mark.asyncio
async def test_red_check_after_unverified_edit_is_changes_required_without_a_reviewer(tmp_path):
    runner = SignalRunner(
        {CODER: [coder_report(), coder_report()], REVIEWER: [verdict("approved")]},
        [EDIT_THEN_READ, EDIT_THEN_PYTEST],
    )
    harness = _harness(tmp_path, runner, ["sh", "-c", "echo boom-failure; exit 1"])
    await harness.run(make_group())
    lines = run_log_lines(harness)
    assert any(
        "verification omitted after the last edit — running check command" in x for x in lines
    )
    assert any("check command red" in x for x in lines)
    # Round 1 never reached the reviewer; the revision prompt carries the output.
    first_verdict = harness.store.paths.group_dir("g1") / "verdict-g1-r1.json"
    assert "boom-failure" in first_verdict.read_text()
    assert "verification omitted" in first_verdict.read_text()
    assert "boom-failure" in runner.prompts[runner.session_ids[CODER]][1]
    # Round 2 verified, so the reviewer was only spawned for that round.
    assert runner.forks.count(REVIEWER) == 1
    assert harness.merged == ["g1"]


@pytest.mark.asyncio
async def test_green_check_proceeds_to_the_reviewer(tmp_path):
    runner = SignalRunner(
        {CODER: [coder_report()], REVIEWER: [verdict("approved")]}, [EDIT_THEN_READ]
    )
    harness = _harness(tmp_path, runner, ["true"])
    await harness.run(make_group())
    assert REVIEWER in runner.forks
    assert harness.merged == ["g1"]
    lines = run_log_lines(harness)
    assert any("running check command" in x for x in lines)
    assert any("check command green" in x for x in lines)


@pytest.mark.asyncio
async def test_a_verified_edit_never_runs_the_check_command(tmp_path):
    runner = SignalRunner(
        {CODER: [coder_report()], REVIEWER: [verdict("approved")]}, [EDIT_THEN_PYTEST]
    )
    harness = _harness(tmp_path, runner, ["sh", "-c", "exit 1"])
    await harness.run(make_group())
    assert harness.merged == ["g1"]
    assert not any("running check command" in x for x in run_log_lines(harness))
