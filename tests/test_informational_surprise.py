"""U13 tests: the informational surprise kind briefs the next generation
without spending a rewrite or calling the speccer.
"""

from __future__ import annotations

import pytest

from orchestrator.config import ExecutionConfig
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import Surprise
from test_review_loop import Harness, StubRunner, coder_report, make_group, verdict


@pytest.mark.asyncio
async def test_group_consuming_only_informational_surprises_spends_no_rewrite(tmp_path):
    runner = StubRunner(
        {"r1-g2-coder-g1": [coder_report()], "r1-g2-reviewer-g1": [verdict("approved")]}
    )
    harness = Harness(tmp_path, runner)
    note = Surprise(kind="informational", description="baseline changed", affected_groups=["g2"])
    harness.board.mark(note, source_group="g1")
    state = await harness.run(make_group("g2"))
    assert state == GroupState.COMPLETED
    assert not harness.rewritten  # no speccer call
    assert GroupState.REWRITING not in harness.states  # rewrite budget untouched
    assert runner.forks[0] == "r1-g2-coder-g1"  # still generation 1


@pytest.mark.asyncio
async def test_informational_surprise_text_reaches_the_next_generations_briefing(tmp_path):
    runner = StubRunner(
        {"r1-g2-coder-g1": [coder_report()], "r1-g2-reviewer-g1": [verdict("approved")]}
    )
    harness = Harness(tmp_path, runner)
    note = Surprise(
        kind="informational",
        description="the preflight baseline now has 3 pre-existing failures",
        affected_groups=["g2"],
    )
    harness.board.mark(note, source_group="g1")
    await harness.run(make_group("g2"))
    coder_prompt = runner.prompts[runner.session_ids["r1-g2-coder-g1"]][0]
    assert "the preflight baseline now has 3 pre-existing failures" in coder_prompt


@pytest.mark.asyncio
async def test_mixed_informational_and_rewrite_worthy_increments_rewrites_once(tmp_path):
    runner = StubRunner(
        {
            "r1-g2-coder-g1": [coder_report()],
            "r1-g2-reviewer-g1": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    info = Surprise(kind="informational", description="fyi", affected_groups=["g2"])
    rewrite_worthy = Surprise(
        kind="missing_dependency", description="g1 renamed the helper", affected_groups=["g2"]
    )
    harness.board.mark(info, source_group="g1")
    harness.board.mark(rewrite_worthy, source_group="g1")
    state = await harness.run(make_group("g2"))
    assert state == GroupState.COMPLETED
    assert len(harness.rewritten) == 1  # exactly one rewrite
    # both surprises reached the speccer in the single rewrite call
    assert info in harness.rewritten[0] and rewrite_worthy in harness.rewritten[0]


@pytest.mark.asyncio
async def test_sixteen_informational_surprises_leave_the_rewrite_budget_untouched(tmp_path):
    # max_rewrites default is 2 (orchestrator/config.py). Sixteen informational
    # surprises must not eat into that budget, so two later genuine failures
    # can each still be rewritten before the group gives up.
    runner = StubRunner(
        {
            "r1-g2-coder-g1": [coder_report(status="blocked")],
            "r1-g2-coder-g2": [coder_report(status="blocked")],
            "r1-g2-coder-g3": [coder_report()],
            "r1-g2-reviewer-g3": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner, execution=ExecutionConfig(max_rewrites=2))
    for i in range(16):
        harness.board.mark(
            Surprise(kind="informational", description=f"note {i}", affected_groups=["g2"]),
            source_group="g1",
        )
    state = await harness.run(make_group("g2"))
    assert state == GroupState.COMPLETED
    # two coder-stuck rewrites, neither charged against the informational budget
    assert len(harness.rewritten) == 2
