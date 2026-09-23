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
from orchestrator.execution.scheduler import GroupFailure
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import EscalationKind

from tests.test_review_loop import (
    Harness,
    StubBroker,
    StubRunner,
    answer,
    coder_report,
    make_group,
    retry,
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
    # Queued twice: an autonomous gate re-runs once on a suspected flake, and a
    # regression is one that fails the re-run too.
    for _ in range(2):
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
    for _ in range(2):  # survives the one automatic flake re-run
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
async def test_autonomous_regression_that_passes_on_rerun_is_a_flake(tmp_path):
    """A flaky test used to cost a rewrite plus a coder generation. In
    autonomous mode the gate re-runs once; a pass on the re-run merges with no
    LLM call, no generation advance, no rewrite."""
    test_id = "tests/test_foo.py::test_bar"
    log_path = _write_preflight_output(tmp_path, "g1", test_id=test_id)
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(
        PreflightFailure(
            f"check command uv run pytest exited 1 — output at {log_path}",
            kind="regression",
            output_path=log_path,
        )
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.merged == ["g1"]
    assert harness.generations == []
    assert harness.rewritten == []
    assert runner.forks == ["r1-g1-coder-g1", "r1-g1-reviewer-g1"]  # no extra session
    log = harness.store.paths.event_log_path.read_text()
    assert "re-running gate once (suspected flake)" in log


@pytest.mark.asyncio
async def test_flake_rerun_is_capped_at_one_per_generation(tmp_path):
    """Three identical failures: the re-run happens once, then the regression is
    real and takes the escalate-then-rewrite path — never a rerun loop."""
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
    for _ in range(2):
        harness.merge_failures.append(
            PreflightFailure(
                f"check command uv run pytest exited 1 — output at {log_path}",
                kind="regression",
                output_path=log_path,
            )
        )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert len(harness.rewritten) == 1
    log = harness.store.paths.event_log_path.read_text()
    assert log.count("suspected flake") == 1


@pytest.mark.asyncio
async def test_hitl_regression_does_not_rerun_on_its_own(tmp_path):
    """With an operator on the kind, the flake re-run is theirs to ask for
    (`retry` with no text) — the gate escalates on the first failure."""
    broker = StubBroker({EscalationKind.PREFLIGHT_FAILED: answer("noted")})
    policy = EscalationPolicy("on_stuck", "workers_via_orchestrator")
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
    harness = Harness(tmp_path, runner, broker=broker, policy=policy)
    harness.merge_failures.append(
        PreflightFailure(
            f"check command uv run pytest exited 1 — output at {log_path}",
            kind="regression",
            output_path=log_path,
        )
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert len(broker.raised) == 1
    assert len(harness.rewritten) == 1


@pytest.mark.asyncio
async def test_retry_with_no_text_reruns_the_gate_without_a_session(tmp_path):
    """`answer --action retry` with no text on a preflight_failed escalation:
    the operator fixed the world by hand and the tree is unchanged — refresh,
    preflight, merge again; no coder, no rewrite."""
    broker = StubBroker({EscalationKind.PREFLIGHT_FAILED: retry("")})
    policy = EscalationPolicy("on_stuck", "workers_via_orchestrator")
    test_id = "tests/test_foo.py::test_bar"
    log_path = _write_preflight_output(tmp_path, "g1", test_id=test_id)
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner, broker=broker, policy=policy)
    harness.merge_failures.append(
        PreflightFailure(
            f"check command uv run pytest exited 1 — output at {log_path}",
            kind="regression",
            output_path=log_path,
        )
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.merged == ["g1"]
    assert runner.forks == ["r1-g1-coder-g1", "r1-g1-reviewer-g1"]
    assert harness.generations == []
    assert harness.rewritten == []
    assert len(broker.raised) == 1


@pytest.mark.asyncio
async def test_retry_with_text_relaunches_the_same_spec_with_the_text(tmp_path):
    """`retry` *with* text: the operator changed something the coder must know
    about (a test) — a fresh coder on the unchanged spec with the text as its
    operator note. No rewrite, no speccer."""
    broker = StubBroker({EscalationKind.PREFLIGHT_FAILED: retry("I fixed the fixture")})
    policy = EscalationPolicy("on_stuck", "workers_via_orchestrator")
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
    harness = Harness(tmp_path, runner, broker=broker, policy=policy)
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
    assert harness.rewritten == []
    second_prompt = runner.prompts[runner.session_ids["r1-g1-coder-g2"]][0]
    assert "## Operator note" in second_prompt
    assert "I fixed the fixture" in second_prompt


@pytest.mark.asyncio
async def test_retry_with_no_text_on_a_non_attributable_failure_reruns_the_gate(tmp_path):
    """The same cheap path for an `env` failure the operator repaired by hand."""
    broker = StubBroker({EscalationKind.PREFLIGHT_FAILED: retry("")})
    policy = EscalationPolicy("on_stuck", "workers_via_orchestrator")
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner, broker=broker, policy=policy)
    harness.merge_failures.append(PreflightFailure("collection failed: ImportError", kind="env"))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.merged == ["g1"]
    assert runner.forks == ["r1-g1-coder-g1", "r1-g1-reviewer-g1"]


def _untracked(*paths: str) -> PreflightFailure:
    return PreflightFailure(
        f"worktree has untracked files: {', '.join(paths)}", kind="untracked", paths=list(paths)
    )


@pytest.mark.asyncio
async def test_first_untracked_failure_relaunches_the_same_spec_with_a_note(tmp_path):
    """Rung 1 of the untracked ladder: attributable, but a relaunch (fresh
    coder, unchanged spec, note naming the paths) — never a rewrite."""
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict()],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict()],
        }
    )
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(_untracked("out/verify.log", "tmp_probe.py"))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.generations == [2]
    assert harness.rewritten == []
    second_prompt = runner.prompts[runner.session_ids["r1-g1-coder-g2"]][0]
    assert "## Operator note" in second_prompt
    assert "untracked files left in the worktree: out/verify.log, tmp_probe.py" in second_prompt
    assert ".coder-scratch/" in second_prompt
    assert harness.merged == ["g1"]


@pytest.mark.asyncio
async def test_second_consecutive_untracked_failure_archives_and_merges(tmp_path):
    """Rung 2: the relaunched coder left the litter again — move it to the
    group's `untracked/` archive, surface a surprise, re-run the gate, merge."""
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict()],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict()],
        }
    )
    harness = Harness(tmp_path, runner)
    (harness.workspace / "out").mkdir()
    (harness.workspace / "out" / "verify.log").write_text("13k lines of scratch\n")
    (harness.workspace / "tmp_probe.py").write_text("print('probe')\n")
    harness.merge_failures.append(_untracked("out/", "tmp_probe.py"))
    harness.merge_failures.append(_untracked("out/", "tmp_probe.py"))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.merged == ["g1"]
    assert harness.generations == [2]  # one relaunch, then archived — no third coder
    assert harness.rewritten == []
    archive = harness.store.paths.untracked_archive_dir("g1")
    assert (archive / "out" / "verify.log").read_text() == "13k lines of scratch\n"
    assert (archive / "tmp_probe.py").is_file()
    assert not (harness.workspace / "out").exists()
    assert not (harness.workspace / "tmp_probe.py").exists()
    log = harness.store.paths.event_log_path.read_text()
    assert "UNTRACKED FILES ARCHIVED" in log


@pytest.mark.asyncio
async def test_untracked_failure_with_operator_retry_no_text_reruns_the_gate(tmp_path):
    """The operator cleaned the tree by hand: `retry` with no text re-runs the
    gate, no relaunch."""
    broker = StubBroker({EscalationKind.PREFLIGHT_FAILED: retry("")})
    policy = EscalationPolicy("on_stuck", "workers_via_orchestrator")
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict()]})
    harness = Harness(tmp_path, runner, broker=broker, policy=policy)
    harness.merge_failures.append(_untracked("stray.log"))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.generations == []
    assert runner.forks == ["r1-g1-coder-g1", "r1-g1-reviewer-g1"]


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
