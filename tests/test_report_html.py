"""``render_html`` (plan U4, report v2 U1/U2/U4) — the synthetic-facts edge
cases the two real fixture runs don't exercise: a failed verification item,
an absent or present one-pager (rendered to HTML with pointer anchors), an
escalation with its operator answer, the pan/zoom diagram shell, and the
``StrictUndefined`` contract.
"""

from __future__ import annotations

import jinja2
import pytest

from orchestrator.report.diagrams import Diagrams, timeline_html
from orchestrator.report.facts import (
    GitRangeFacts,
    GroupFacts,
    RunFacts,
    SessionFacts,
    UnitFacts,
    VerificationFacts,
)
from orchestrator.report.html import _env, render_html, render_one_pager_html

RUN_ID = "r20260101-000000"


def _facts(*, failing: bool) -> RunFacts:
    status = "fail" if failing else "pass"
    return RunFacts(
        run_id=RUN_ID,
        plan_title="Synthetic report-html fixture",
        git_range=GitRangeFacts(available=False),
        groups=[
            GroupFacts(
                id="g1",
                name="Widget",
                state="completed",
                sessions=[
                    SessionFacts(
                        role="coder",
                        generation=1,
                        model="sonnet",
                        started_at="2026-01-01T00:00:00+00:00",
                        ended_at="2026-01-01T00:30:00+00:00",
                        tokens={"input": 10, "output": 20, "cache_creation": 30},
                        cache_read_tokens=1_000_000,
                    )
                ],
            )
        ],
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


def _diagrams(facts: RunFacts | None = None) -> Diagrams:
    return Diagrams(
        timeline=timeline_html(facts) if facts else '<table class="timeline"></table>',
        plan_outcome="flowchart LR\n    a --> b\n",
        architecture_delta="%% no python files changed",
    )


_ONE_PAGER = f"""# Synthetic — {RUN_ID}

## TL;DR

- The widget landed (g1)
- The unit is done (u1)
- The item passed (g1-1)

## Problems found

- None recorded (g1)

## Run notes

- Nothing was fixed by hand (g1/coder/gen1)

## Next steps

- Keep watching the widget file (widget.py)

<!-- valid pointers: g1, g1-1, u1, widget.py -->
"""


def test_fail_item_marks_cell_fail_and_header_shows_trouble_badge():
    html = render_html(_facts(failing=True), _diagrams())
    assert "cell-fail" in html
    assert "badge-trouble" in html


def test_pass_only_facts_show_no_fail_cell_and_no_trouble_badge():
    html = render_html(_facts(failing=False), _diagrams())
    assert 'class="cell-fail"' not in html
    assert 'class="badge badge-trouble"' not in html


# ---------------------------------------------------------------- one-pager


def test_one_pager_none_omits_summary_section():
    html = render_html(_facts(failing=False), _diagrams(), one_pager=None)
    assert '<div class="one-pager">' not in html
    assert "<h2>Summary</h2>" not in html


def test_one_pager_renders_as_html_with_pointer_anchors_and_no_comment():
    html = render_html(_facts(failing=False), _diagrams(), one_pager=_ONE_PAGER)
    assert "<h2>Summary</h2>" in html
    assert "<h2>TL;DR</h2>" in html
    assert "<li>" in html
    assert "valid pointers" not in html
    assert '<a href="#group-g1">g1</a>' in html
    assert '<a href="#unit-u1">u1</a>' in html
    assert '<a href="#item-g1-1">g1-1</a>' in html
    assert '<a href="#group-g1">g1/coder/gen1</a>' in html
    # A file path has no card: it stays plain text.
    assert "(widget.py)" in html
    # …and the anchors it points at exist.
    assert 'id="group-g1"' in html
    assert 'id="unit-u1"' in html
    assert 'id="item-g1-1"' in html


def test_one_pager_markdown_is_escaped_not_injected():
    facts = _facts(failing=False)
    rendered = str(render_one_pager_html("# T\n\n- <script>alert(1)</script> (g1)\n", facts))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


# -------------------------------------------------------------- escalations


def test_escalations_table_rendered_only_when_any_exist():
    clean = render_html(_facts(failing=False), _diagrams())
    assert "Escalations and answers" not in clean

    facts = _facts(failing=True)
    facts.groups[0].escalations = [
        {
            "id": "abcdef0123456789",
            "kind": "stuck",
            "generation": 1,
            "prompt": "p" * 300,
            "created_at": "2026-01-01T00:05:00+00:00",
            "request_path": "escalations/abcdef0123456789.json",
            "action": "retry",
            "answer": "Fixed the config by hand & relaunched",
        }
    ]
    html = render_html(facts, _diagrams())
    assert "Escalations and answers" in html
    assert 'id="escalation-abcdef012345"' in html
    assert "<td>stuck</td>" in html
    assert "<td>retry</td>" in html
    assert "Fixed the config by hand &amp; relaunched" in html
    # Prompt excerpt is capped at 200 chars plus an ellipsis.
    assert "p" * 200 + "…" in html
    assert "p" * 201 not in html


# ----------------------------------------------------------------- diagrams


def test_diagram_shell_pan_zoom_and_dialog_are_present():
    html = render_html(_facts(failing=False), _diagrams())
    assert "svg-pan-zoom@3.6.2/dist/svg-pan-zoom.min.js" in html
    assert "flowchart: { useMaxWidth: false }" in html
    assert "svgPanZoom(svg" in html
    assert html.count('<figure class="diagram">') == 2
    assert html.count('<button type="button" data-action="expand">') == 2
    assert html.count('<details class="diagram-source">') == 2
    assert '<dialog class="diagram-dialog"' in html
    assert "sequenceDiagram" not in html
    assert "How-to-use" not in html


def test_timeline_table_has_a_row_per_group_and_cost_splits_cache_reads():
    facts = _facts(failing=False)
    html = render_html(facts, _diagrams(facts))
    assert '<table class="timeline">' in html
    assert html.count('<th scope="row">') == len(facts.groups)
    assert "<td>60</td>" in html  # input + output + cache_creation
    assert "1,000,000" in html  # cache reads, listed apart


def test_section_order_is_bluf():
    html = render_html(_facts(failing=False), _diagrams(), one_pager=_ONE_PAGER)
    order = [
        'id="summary"',
        'id="evidence"',
        'id="requirements"',
        'id="groups"',
        'id="cost"',
        'id="timeline"',
        'id="architecture"',
        'id="plan-outcome"',
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_template_raises_on_undefined_variable_instead_of_rendering_blank():
    template = _env.get_template("report.html.j2")
    with pytest.raises(jinja2.UndefinedError):
        template.render(facts=_facts(failing=False))
