"""U3 tests: the merge gate routes a preflight failure by cause instead of
treating every failure identically (plan U3, merge-gate-triage).

Reuses ``test_review_loop.py``'s in-process ``Harness``/``StubRunner`` rig —
sessions are scripted, so a whole generation-and-rewrite scenario runs with
zero subprocesses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.execution.escalation import EscalationPolicy
from orchestrator.execution.preflight import PreflightBaseline, PreflightFailure
from orchestrator.execution.review import GroupFailure
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import EscalationKind

from tests.test_review_loop import (
    Harness,
    StubBroker,
    StubRunner,
    answer,
    coder_report,
    make_group,
    verdict,
)

JUNIT_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" tests="1" failures="1">
    <testcase classname="{classname}" name="{name}">
      <failure message="AssertionError">assert 1 == 2</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

SHORT_SUMMARY = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_foo.py::test_bar - AssertionError: assert 1 == 2\n"
    "======================== 1 failed, 5 passed in 1.23s =========================\n"
)


def _write_preflight_output(tmp_path: Path, gid: str, *, test_id: str) -> Path:
    """A check log + JUnit XML pair shaped like a real ``run_preflight`` failure
    (plan U1/U2), under the same directory a group's own preflight output would
    land in."""
    out_dir = tmp_path / "groups" / gid
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "preflight-check.log"
    log_path.write_text(f"...pytest run...\n{SHORT_SUMMARY}")
    classname, _, name = test_id.rpartition("::")
    (out_dir / "preflight-junit.xml").write_text(
        JUNIT_TEMPLATE.format(classname=classname, name=name)
    )
    return log_path


@pytest.mark.asyncio
async def test_env_failure_leaves_generation_and_rewrites_unchanged(tmp_path):
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(
        PreflightFailure("worktree is not clean: scratch.log", kind="env")
    )
    with pytest.raises(GroupFailure, match="env"):
        await harness.run(make_group())
    assert harness.generations == []  # no generation advance
    assert harness.rewritten == []  # no rewrite spent
    assert not harness.merged


@pytest.mark.asyncio
async def test_timeout_failure_leaves_generation_and_rewrites_unchanged(tmp_path):
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(
        PreflightFailure("check command timed out after 60s", kind="timeout")
    )
    with pytest.raises(GroupFailure, match="timeout"):
        await harness.run(make_group())
    assert harness.generations == []
    assert harness.rewritten == []


@pytest.mark.asyncio
async def test_pre_existing_failure_leaves_generation_unchanged(tmp_path):
    test_id = "tests/test_foo.py::test_bar"
    log_path = _write_preflight_output(tmp_path, "g1", test_id=test_id)
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner)
    harness.deps.preflight_baseline = PreflightBaseline(
        command=["uv", "run", "pytest"],
        commit_sha="abc123",
        exit_code=1,
        captured=True,
        tests={test_id: "failed"},
    )
    harness.merge_failures.append(
        PreflightFailure(
            f"check command uv run pytest exited 1 — output at {log_path}",
            kind="regression",
            output_path=log_path,
        )
    )
    with pytest.raises(GroupFailure, match="pre-existing"):
        await harness.run(make_group())
    assert harness.generations == []
    assert harness.rewritten == []


@pytest.mark.asyncio
async def test_new_attributable_failure_advances_generation_exactly_once(tmp_path):
    test_id = "tests/test_foo.py::test_bar"
    log_path = _write_preflight_output(tmp_path, "g1", test_id=test_id)
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict()],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict()],
        }
    )
    harness = Harness(tmp_path, runner)
    # No baseline captured at all — cannot be attributed as pre-existing, so
    # this is the "new and attributable" path, same as an absent baseline.
    harness.merge_failures.append(
        PreflightFailure(
            f"check command uv run pytest exited 1 — output at {log_path}",
            kind="regression",
            output_path=log_path,
        )
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.generations == [2]  # exactly one advance
    assert len(harness.rewritten) == 1
    # the surprise handed to the rewrite carries the short test summary tail,
    # not only the path to preflight-check.log.
    rewrite_surprises = harness.rewritten[0]
    assert any("FAILED tests/test_foo.py::test_bar" in s.description for s in rewrite_surprises)
    assert any(
        "short test summary info" not in s.description or "FAILED" in s.description
        for s in rewrite_surprises
    )


@pytest.mark.asyncio
async def test_new_failure_absent_from_baseline_is_attributable(tmp_path):
    test_id = "tests/test_foo.py::test_bar"
    log_path = _write_preflight_output(tmp_path, "g1", test_id=test_id)
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict()],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict()],
        }
    )
    harness = Harness(tmp_path, runner)
    harness.deps.preflight_baseline = PreflightBaseline(
        command=["uv", "run", "pytest"],
        commit_sha="abc123",
        exit_code=0,
        captured=True,
        tests={"tests/test_other.py::test_ok": "passed"},
    )
    harness.merge_failures.append(
        PreflightFailure(
            f"check command uv run pytest exited 1 — output at {log_path}",
            kind="regression",
            output_path=log_path,
        )
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.generations == [2]
    assert len(harness.rewritten) == 1


@pytest.mark.asyncio
async def test_preflight_escalation_kind_is_distinguishable_from_merge_conflict(tmp_path):
    broker = StubBroker({EscalationKind.PREFLIGHT_FAILED: answer("noted")})
    policy = EscalationPolicy("on_stuck", "workers_via_orchestrator")
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner, broker=broker, policy=policy)
    harness.merge_failures.append(PreflightFailure("worktree is not clean: x", kind="env"))
    with pytest.raises(GroupFailure):
        await harness.run(make_group())
    assert len(broker.raised) == 1
    request = broker.raised[0]
    assert request.kind == EscalationKind.PREFLIGHT_FAILED
    assert request.kind != EscalationKind.MERGE_CONFLICT


@pytest.mark.asyncio
async def test_env_failure_diagnosis_is_readable_in_group_failure(tmp_path):
    # GroupFailure's message is exactly what the scheduler writes into
    # `state.json`'s `groups[gid].failure` (via `_classify`) — so this is the
    # diagnosis an operator reads without opening the check log.
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(
        PreflightFailure("worktree is not clean: scratch.log", kind="env")
    )
    with pytest.raises(GroupFailure) as excinfo:
        await harness.run(make_group())
    message = str(excinfo.value)
    assert "env" in message
    assert "scratch.log" in message
