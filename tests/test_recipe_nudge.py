"""Recipe reports are re-nudged toward their own contract, never a reviewer
verdict, and a recipe worker's first prompt carries its ``recipe_args``.

Regression for run r20260927-100604's live item g3-4: a ``research`` worker
whose first report put the findings in a separate JSON block was re-nudged
with the *reviewer* verdict skeleton (``_nudge_prompt`` tested
``model_cls is CoderReport``), obeyed it, and failed the group on
``FindingsReport`` validation. Its prompt also said "the <spec> block declares
the question" while the question lived only in ``recipe_args``.
"""

from __future__ import annotations

import json
import re

from orchestrator.execution.prompting import (
    render_coder_nudge_contract,
    render_coder_nudge_skeleton,
    render_coder_prompt,
    render_worker_prompt,
)
from orchestrator.execution.sessions import ReportError, _nudge_prompt, parse_report
from orchestrator.model import CoderReport, Group, ReviewerVerdict, ReviewIntensity
from orchestrator.recipes.optimize import OptimizeReport
from orchestrator.recipes.research import FindingsReport


def _group(**overrides: object) -> Group:
    fields: dict[str, object] = dict(
        id="g1",
        name="research-probe",
        summary="probe",
        spec="context only",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
    )
    fields.update(overrides)
    return Group(**fields)


def _filled(skeleton: str) -> str:
    """The skeleton with its placeholder values filled, as a worker would."""
    return skeleton.replace('"summary": "..."', '"summary": "done"').replace(
        '"claim": "..."', '"claim": "ABI 4 adds TCP rules"'
    )


def test_findings_report_nudges_carry_findings_not_a_verdict() -> None:
    exc = ReportError("status 'completed' requires at least one finding")
    first = _nudge_prompt(0, exc, FindingsReport, ["g1-1"])
    second = _nudge_prompt(1, exc, FindingsReport, ["g1-1"])
    for prompt in (first, second):
        assert '"findings"' in prompt
        assert "approved" not in prompt
        assert "required_changes" not in prompt
    report = parse_report(_filled(second), FindingsReport)
    assert report.status == "completed"
    assert report.findings[0].sources == ["https://... or a repo-relative path"]


def test_optimize_report_nudge_carries_candidate() -> None:
    second = _nudge_prompt(1, ReportError("x"), OptimizeReport, [])
    assert '"candidate"' in second and "approved" not in second
    assert parse_report(_filled(second), OptimizeReport).status == "completed"


def test_coder_and_reviewer_nudges_are_unchanged() -> None:
    exc = ReportError("x")
    assert _nudge_prompt(0, exc, CoderReport, ["a"]) == render_coder_nudge_contract("x", ["a"])
    assert _nudge_prompt(1, exc, CoderReport, ["a"]) == render_coder_nudge_skeleton(["a"])
    assert CoderReport.extra_fields_example() == {}
    verdict = _nudge_prompt(1, exc, ReviewerVerdict, [])
    assert '"status": "approved"' in verdict


def test_research_prompt_carries_recipe_args() -> None:
    args = {"question": "What does Landlock ABI 4 add?", "output": "docs/research/probe.md"}
    group = _group(recipe="research", recipe_args=args)
    prompt = render_worker_prompt("research", "r1", group)
    block = re.search(r"<recipe-args>\n(.*?)\n</recipe-args>", prompt, re.S)
    assert block is not None
    assert json.loads(block.group(1)) == args
    assert "<recipe-args> block above" in prompt
    assert "SAME JSON body" in prompt


def test_code_prompt_is_byte_identical_without_recipe_args() -> None:
    group = _group(name="plain")
    assert "<recipe-args>" not in render_worker_prompt("coder", "r1", group)
    assert render_worker_prompt("coder", "r1", group) == render_coder_prompt("r1", group)
