"""``render_html`` (plan U4) — the synthetic-facts edge cases the two real
fixture runs don't exercise: a failed verification item, an absent
one-pager, and the ``StrictUndefined`` contract. See
``docs/plans/2026-09-02-001-feat-run-report-plan.md`` U4.
"""

from __future__ import annotations

import jinja2
import pytest

from orchestrator.report.diagrams import Diagrams
from orchestrator.report.facts import (
    GitRangeFacts,
    GroupFacts,
    RunFacts,
    UnitFacts,
    VerificationFacts,
)
from orchestrator.report.html import _env, render_html

RUN_ID = "r20260101-000000"


def _facts(*, failing: bool) -> RunFacts:
    status = "fail" if failing else "pass"
    return RunFacts(
        run_id=RUN_ID,
        plan_title="Synthetic report-html fixture",
        git_range=GitRangeFacts(available=False),
        groups=[GroupFacts(id="g1", name="Widget", state="completed")],
        units=[
            UnitFacts(
                unit_id="u1",
                task_id="u1-widget",
                group_id="g1",
                title="Widget",
                summary="A widget.",
                verification=[
                    VerificationFacts(
                        item_id="g1-1",
                        description="does the thing",
                        status=status,
                        evidence="ran it",
                    )
                ],
                landed=not failing,
            )
        ],
        trouble=failing,
    )


def _diagrams() -> Diagrams:
    return Diagrams(
        timeline="gantt\n    section g1\n",
        plan_outcome="flowchart LR\n    a --> b\n",
        architecture_delta="%% no python files changed",
        howto_sequences=[],
        howto_note="no entry points",
    )


def test_fail_item_marks_cell_fail_and_header_shows_trouble_badge():
    html = render_html(_facts(failing=True), _diagrams())
    assert "cell-fail" in html
    assert "badge-trouble" in html


def test_pass_only_facts_show_no_fail_cell_and_no_trouble_badge():
    html = render_html(_facts(failing=False), _diagrams())
    assert 'class="cell-fail"' not in html
    assert 'class="badge badge-trouble"' not in html


def test_one_pager_none_omits_narrative_section():
    html = render_html(_facts(failing=False), _diagrams(), one_pager=None)
    assert '<div class="one-pager">' not in html
    assert "<h2>Summary</h2>" not in html


def test_one_pager_present_renders_narrative_section():
    html = render_html(_facts(failing=False), _diagrams(), one_pager="A capped narrative.")
    assert "A capped narrative." in html
    assert "<h2>Summary</h2>" in html


def test_template_raises_on_undefined_variable_instead_of_rendering_blank():
    template = _env.get_template("report.html.j2")
    with pytest.raises(jinja2.UndefinedError):
        template.render(facts=_facts(failing=False))
