"""U7 tests: a `research` group's Findings Artifact refines its declared
downstream consumer's spec through the surprise board, without spending a
rewrite (plan U7 of the research/evaluate/optimize plan).
"""

from __future__ import annotations


import pytest

from orchestrator.config import ExecutionConfig
from orchestrator.execution.scheduler import GroupFailure, GroupState
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.model import ReviewIntensity, Surprise
from test_review_loop import (
    Harness,
    StubRunner,
    _prepare_research_workspace,
    findings_report,
    make_group,
)

OUTPUT = "docs/research/landlock.md"
REFINEMENT_TEXT = "use the noise floor from the ledger, not a fixed threshold"


def _downstream_groups():
    """g1 (research) -> g2 (downstream, owns u2-...) ; g3 unrelated (owns u3-...)."""
    g1 = make_group(
        "g1",
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="research",
        recipe_args={"question": "what noise floor should optimize use?", "output": OUTPUT},
        spec=f"Answer the question. Commit the finding at {OUTPUT}.",
    )
    g2 = make_group(
        "g2",
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=["g1"],
        tasks=["u2-optimize-recipe"],
    )
    g3 = make_group(
        "g3",
        intensity=ReviewIntensity.SELF_VERIFY,
        tasks=["u3-unrelated-recipe"],
    )
    return g1, g2, g3


def _wire_groups(harness: Harness, groups) -> None:
    """Give the harness a real group graph: the board must resolve task ids to
    their owning group, and the loop's downstream check reads `groups_by_id`."""
    harness.board = SurpriseBoard(groups=list(groups))
    harness.deps.board = harness.board
    harness.deps.groups_by_id = {g.id: g for g in groups}


@pytest.mark.asyncio
async def test_valid_target_marks_one_surprise_for_its_owning_group(tmp_path):
    g1, g2, g3 = _downstream_groups()
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [
                findings_report(
                    spec_refinement={
                        "target_task": "u2-optimize-recipe",
                        "refinement": REFINEMENT_TEXT,
                    }
                )
            ]
        }
    )
    harness = Harness(tmp_path, runner)
    _wire_groups(harness, [g1, g2, g3])
    _prepare_research_workspace(harness.workspace, OUTPUT)

    state = await harness.run(g1)

    assert state == GroupState.COMPLETED
    assert harness.merged == ["g1"]
    pending_g2 = harness.board.pending_for("g2")
    assert len(pending_g2) == 1
    surprise = pending_g2[0]
    assert surprise.kind == "spec_refinement"
    assert surprise.description == f"[from g1 artifact g1] {REFINEMENT_TEXT}"
    assert surprise.affected_groups == ["u2-optimize-recipe"]
    assert not harness.board.pending_for("g3")


@pytest.mark.asyncio
async def test_invalid_target_is_nudged_and_marks_nothing(tmp_path):
    g1, g2, g3 = _downstream_groups()
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [
                findings_report(
                    spec_refinement={
                        "target_task": "u3-unrelated-recipe",
                        "refinement": REFINEMENT_TEXT,
                    }
                ),
                # the corrected round: no refinement at all, just a plain finding.
                findings_report(),
            ]
        }
    )
    harness = Harness(tmp_path, runner)
    _wire_groups(harness, [g1, g2, g3])
    _prepare_research_workspace(harness.workspace, OUTPUT)

    state = await harness.run(g1)

    assert state == GroupState.COMPLETED
    assert harness.merged == ["g1"]
    assert not harness.board.pending_for("g2")
    assert not harness.board.pending_for("g3")

    coder_sid = runner.session_ids["r1-g1-coder-g1"]
    prompts = runner.prompts[coder_sid]
    assert len(prompts) == 2  # first-round prompt, then the nudge resume
    nudge = prompts[1]
    assert "u3-unrelated-recipe" in nudge
    assert "u2-optimize-recipe" in nudge  # the allowed target named in the nudge


@pytest.mark.asyncio
async def test_spec_refinement_pending_surprise_rewrite_not_counted(tmp_path):
    surprise = Surprise(
        kind="spec_refinement",
        description=f"[from g1 artifact g1] {REFINEMENT_TEXT}",
        affected_groups=["g2"],
    )
    runner = StubRunner({"r1-g2-coder-g1": [findings_report()], "r1-g2-reviewer-g1": []})
    harness = Harness(tmp_path, runner, execution=ExecutionConfig(max_rewrites=0))
    harness.board.mark(surprise, source_group="g1")

    def rewrite_spec_with_refinement(group, surprises):
        harness.rewritten.append(surprises)
        notes = "; ".join(s.description for s in surprises)
        return group.model_copy(update={"spec": f"{group.spec}\n\nRefinement: {notes}"})

    harness.deps.rewrite_spec = rewrite_spec_with_refinement

    coder_group = make_group("g2", intensity=ReviewIntensity.SELF_VERIFY)
    state = await harness.run(coder_group)

    assert state == GroupState.COMPLETED  # no GroupFailure despite max_rewrites=0
    assert harness.rewritten == [[surprise]]
    spec_gen_path = harness.store.paths.group_dir("g2") / "spec-gen1.json"
    assert spec_gen_path.is_file()
    assert REFINEMENT_TEXT in spec_gen_path.read_text()


@pytest.mark.asyncio
async def test_other_surprise_pending_rewrite_not_counted_still_exhausts_cap(tmp_path):
    surprise = Surprise(kind="other", description="g1 renamed the helper", affected_groups=["g2"])
    runner = StubRunner({"r1-g2-coder-g1": []})
    harness = Harness(tmp_path, runner, execution=ExecutionConfig(max_rewrites=0))
    harness.board.mark(surprise, source_group="g1")

    with pytest.raises(GroupFailure, match="rewrite cap"):
        await harness.run(make_group("g2", intensity=ReviewIntensity.SELF_VERIFY))
