"""U9 tests: the decision ledger carried verbatim into every later prompt of a
group, and the reviewer's amendment rule (plan U9).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.config import BreakerConfig
from orchestrator.execution.decisions import OperatorDecision, render_decisions_section
from orchestrator.execution.escalation import answer_escalation
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.prompting import (
    render_coder_prompt,
    render_handoff_prompt,
    render_re_review_prompt,
    render_reviewer_prompt,
)
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import (
    EscalationResponse,
    GroupingResult,
    HumanAction,
)

from test_review_loop import Harness, StubRunner, coder_report, make_group, on_stuck, verdict

FIXTURE_GROUPS = Path(__file__).parent / "fixtures" / "observatory" / "run-modern" / "groups.json"

# Deliberately hostile to naive Template re-parsing: "$5" is not a valid
# placeholder (digits can't open an identifier), backticks and braces are plain
# text to `string.Template`, but all three are exactly the characters an
# operator's free-text answer is likely to contain.
_TRICKY_LEDGER = [
    OperatorDecision(
        escalation_id="esc-aaa111",
        at="2026-09-16T10:00:00Z",
        generation=1,
        question="how many chapters?",
        answer="four chapters, not six — budget is $5 per `chapter`, slot {0} first",
    ),
    OperatorDecision(
        escalation_id="esc-bbb222",
        at="2026-09-16T11:00:00Z",
        generation=1,
        question="which voice?",
        answer="use the `narrator` voice, cost ${env.VOICE_COST}",
    ),
]


def _make_group(**overrides):
    return make_group(**overrides)


class TestRenderEmptyAndPopulated:
    """[g12-1] Each renderer with decisions="" is inert; with a ledger, the
    section appears verbatim exactly once."""

    # The static ground-rules prose (coder.md/handoff.md's "binding" line,
    # reviewer.md's amendment paragraph) mentions "Operator decisions" and even
    # the literal heading text unconditionally, so "no section" is checked by
    # the absence of actually-rendered ledger content (the "Decision:" label
    # `render_decisions_section` emits per entry), not by the absence of any
    # mention of the word "decisions". A populated ledger is checked by the
    # exact rendered block — header, entries and all — appearing verbatim
    # exactly once, which cannot collide with the static prose.

    def test_coder_prompt_render_empty_decisions_has_no_section(self):
        group = _make_group()
        empty = render_coder_prompt("r1", group, decisions="")
        assert "Decision:" not in empty
        assert "$decisions" not in empty

    def test_coder_prompt_render_with_ledger_contains_section_once(self):
        group = _make_group()
        section = render_decisions_section(_TRICKY_LEDGER)
        rendered = render_coder_prompt("r1", group, decisions=section)
        assert rendered.count(section) == 1

    def test_handoff_prompt_render_empty_decisions_has_no_section(self):
        group = _make_group()
        empty = render_handoff_prompt(
            "r1",
            group,
            generation=2,
            retirement_reason="round threshold",
            last_report="{}",
            outstanding="- fix x",
            diff_summary="+1 -0",
            decisions="",
        )
        assert "Decision:" not in empty
        assert "$decisions" not in empty

    def test_handoff_prompt_render_with_ledger_contains_section_once(self):
        group = _make_group()
        section = render_decisions_section(_TRICKY_LEDGER)
        rendered = render_handoff_prompt(
            "r1",
            group,
            generation=2,
            retirement_reason="round threshold",
            last_report="{}",
            outstanding="- fix x",
            diff_summary="+1 -0",
            decisions=section,
        )
        assert rendered.count(section) == 1

    def test_reviewer_prompt_render_empty_decisions_has_no_section(self):
        group = _make_group()
        empty = render_reviewer_prompt(
            "r1",
            group,
            report_path="report.json",
            base_ref="main",
            scratch_dir=".review-scratch",
            decisions="",
        )
        assert "Decision:" not in empty
        assert "$decisions" not in empty

    def test_reviewer_prompt_render_with_ledger_contains_section_once(self):
        group = _make_group()
        section = render_decisions_section(_TRICKY_LEDGER)
        rendered = render_reviewer_prompt(
            "r1",
            group,
            report_path="report.json",
            base_ref="main",
            scratch_dir=".review-scratch",
            decisions=section,
        )
        assert rendered.count(section) == 1

    def test_re_review_prompt_render_empty_decisions_has_no_section(self):
        empty = render_re_review_prompt("report.json", decisions="")
        assert "Decision:" not in empty
        assert "$decisions" not in empty

    def test_re_review_prompt_render_with_ledger_contains_section_once(self):
        section = render_decisions_section(_TRICKY_LEDGER)
        rendered = render_re_review_prompt("report.json", decisions=section)
        assert rendered.count(section) == 1


class FileBackedBroker:
    """A broker that persists the request/response to disk like the real
    ``EscalationBroker`` + ``answer_escalation`` pair, so ``decision_ledger``
    (which always reads fresh from disk) sees the answer immediately."""

    def __init__(
        self,
        paths: RunPaths,
        *,
        action: HumanAction = HumanAction.ANSWER,
        answer_text: str = "",
        binding: bool = True,
    ):
        self.paths = paths
        self.action = action
        self.answer_text = answer_text
        self.binding = binding
        self.raised = []
        self.escalation_ids: list[str] = []

    def raise_escalation(self, request) -> EscalationResponse:
        self.raised.append(request)
        atomic_write_text(
            self.paths.escalations_dir / f"request-{request.id}.json",
            request.model_dump_json() + "\n",
        )
        answer_escalation(
            self.paths, request.id, self.action, self.answer_text, binding=self.binding
        )
        self.escalation_ids.append(request.id)
        return EscalationResponse.model_validate_json(
            (self.paths.escalations_dir / f"response-{request.id}.json").read_text()
        )

    def trigger_abort(self) -> None:
        pass


def _spec_hashes_by_generation(harness: Harness) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for session in harness.manifest.groups["g1"].sessions:
        if session.role.value == "coder" and session.spec_sha256:
            out.setdefault(session.generation, set()).add(session.spec_sha256)
    return out


@pytest.mark.asyncio
async def test_carries_binding_answer_into_generation_two_handoff_and_reviewer_prompts(tmp_path):
    # [g12-2] generation 1's coder asks a question; the broker answers with a
    # binding decision; the coder then retires on the round-threshold breaker;
    # generation 2's handoff AND its reviewer's first prompt both carry the
    # decision verbatim under the ledger heading, with the escalation id.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [
                coder_report("needs_input", question="how many chapters?"),
                coder_report(),
            ],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["apply the chapter count"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(
        tmp_path,
        runner,
        breaker=BreakerConfig(max_rounds_per_generation=1, max_generations=3),
        policy=on_stuck(),
    )
    broker = FileBackedBroker(harness.store.paths, answer_text="four chapters, not six")
    harness.deps.broker = broker

    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert len(broker.escalation_ids) == 1
    esc_id = broker.escalation_ids[0]

    handoff_prompt = runner.prompts[runner.session_ids["r1-g1-coder-g2"]][0]
    reviewer_prompt = runner.prompts[runner.session_ids["r1-g1-reviewer-g2"]][0]
    for prompt in (handoff_prompt, reviewer_prompt):
        assert "## Operator decisions (binding)" in prompt
        assert "four chapters, not six" in prompt
        assert esc_id in prompt

    spec_hashes = _spec_hashes_by_generation(harness)
    assert spec_hashes[1] == spec_hashes[2]


@pytest.mark.asyncio
async def test_guidance_only_answer_carries_nothing_into_later_prompts(tmp_path):
    # [g12-3] same scenario, but the answer is marked non-binding (`--guidance`):
    # neither the generation-2 handoff nor the reviewer's first prompt mentions it.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [
                coder_report("needs_input", question="how many chapters?"),
                coder_report(),
            ],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["apply the chapter count"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(
        tmp_path,
        runner,
        breaker=BreakerConfig(max_rounds_per_generation=1, max_generations=3),
        policy=on_stuck(),
    )
    broker = FileBackedBroker(
        harness.store.paths, answer_text="four chapters, not six", binding=False
    )
    harness.deps.broker = broker

    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED

    handoff_prompt = runner.prompts[runner.session_ids["r1-g1-coder-g2"]][0]
    reviewer_prompt = runner.prompts[runner.session_ids["r1-g1-reviewer-g2"]][0]
    for prompt in (handoff_prompt, reviewer_prompt):
        assert "Decision:" not in prompt
        assert "four chapters, not six" not in prompt


_KNOWN_PLACEHOLDERS = (
    "$identity_block",
    "$group_name",
    "$verification",
    "$report_contract",
    "$decisions",
    "$report_path",
    "$base_ref",
    "$scratch_dir",
    "$generation",
    "$retirement_reason",
    "$last_report",
    "$outstanding",
    "$diff_summary",
)


class TestFixtureGroupsOracle:
    """[g12-5] Every group loaded from the tracked run-modern fixture substitutes
    cleanly through all four shipped templates with a hostile two-entry ledger."""

    def test_all_fixture_groups_substitute_cleanly_through_all_renderers(self):
        data = json.loads(FIXTURE_GROUPS.read_text())
        result = GroupingResult.model_validate(data)
        assert result.groups, "fixture must contain at least one group"
        section = render_decisions_section(_TRICKY_LEDGER)

        for group in result.groups:
            rendered = [
                render_coder_prompt("r1", group, decisions=section),
                render_handoff_prompt(
                    "r1",
                    group,
                    generation=2,
                    retirement_reason="round threshold",
                    last_report="{}",
                    outstanding="- fix x",
                    diff_summary="+1 -0",
                    decisions=section,
                ),
                render_reviewer_prompt(
                    "r1",
                    group,
                    report_path="report.json",
                    base_ref="main",
                    scratch_dir=".review-scratch",
                    decisions=section,
                ),
                render_re_review_prompt("report.json", decisions=section),
            ]
            for output in rendered:
                for decision in _TRICKY_LEDGER:
                    assert decision.answer in output
                for placeholder in _KNOWN_PLACEHOLDERS:
                    assert placeholder not in output
