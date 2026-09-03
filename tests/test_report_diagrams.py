"""Diagram renderers in ``orchestrator/report/diagrams.py`` (plan U2, report
v2 U1) — the synthetic-facts edge cases the two real fixture runs don't
exercise: an unavailable git range, the HTML timeline's escaping and bar
count, and the structural minimum every renderer's output must meet.
"""

from __future__ import annotations

from pathlib import Path

import orchestrator.report.diagrams as diagrams_module
from orchestrator.report.diagrams import (
    Diagrams,
    architecture_delta,
    plan_outcome_flowchart,
    render_all,
    timeline_html,
)
from orchestrator.report.facts import (
    GitRangeFacts,
    GroupFacts,
    RunFacts,
    SessionFacts,
    TimelineEvent,
    UnitFacts,
    VerificationFacts,
)

RUN_ID = "r20260101-000000"


def _facts(**overrides) -> RunFacts:
    base = dict(
        run_id=RUN_ID,
        git_range=GitRangeFacts(available=False),
        groups=[
            GroupFacts(
                id="g1",
                name="Widget",
                state="completed",
                verdict_status="approved",
                sessions=[
                    SessionFacts(
                        role="coder",
                        generation=1,
                        started_at="2026-01-01T00:00:00+00:00",
                        ended_at="2026-01-01T00:10:00+00:00",
                    ),
                    SessionFacts(
                        role="reviewer",
                        generation=1,
                        started_at="2026-01-01T00:10:00+00:00",
                        ended_at="2026-01-01T00:20:00+00:00",
                    ),
                ],
            )
        ],
        units=[
            UnitFacts(
                unit_id="u1",
                task_id="u1-widget",
                group_id="g1",
                title="Widget",
                verification=[
                    VerificationFacts(item_id="g1-1", description="does the thing", status="pass")
                ],
                landed=True,
            )
        ],
    )
    base.update(overrides)
    return RunFacts(**base)


def test_architecture_delta_notes_when_git_range_unavailable():
    facts = _facts()
    output = architecture_delta(facts, Path("."))
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0].startswith("%%")


def test_howto_sequences_are_gone():
    assert not hasattr(diagrams_module, "howto_sequences")
    assert not hasattr(diagrams_module, "timeline_gantt")
    assert "howto_sequences" not in Diagrams.model_fields
    assert "howto_note" not in Diagrams.model_fields


# ----------------------------------------------------------------- timeline


def test_timeline_html_has_one_row_per_group_and_one_bar_per_session():
    facts = _facts()
    facts.groups.append(
        GroupFacts(
            id="g2",
            name="Gadget",
            state="failed",
            sessions=[
                SessionFacts(role="coder", generation=1, started_at="2026-01-01T00:20:00+00:00")
            ],
        )
    )
    output = timeline_html(facts)
    assert output.startswith('<table class="timeline">')
    assert output.count("<tr>") == 1 + 2  # header row + one per group
    assert output.count('class="bar coder') == 2
    assert output.count('class="bar reviewer') == 1
    assert 'href="#group-g1"' in output and 'href="#group-g2"' in output
    # g2's coder never ended: its bar is marked open, never zero-width.
    assert 'class="bar coder open"' in output


def test_timeline_html_escapes_group_names_and_titles():
    facts = _facts()
    facts.groups[0].name = "Widget <b>bold</b> & co"
    facts.groups[0].sessions[0].retirement_reason = 'context <limit> "hit"'
    output = timeline_html(facts)
    assert "<b>bold</b>" not in output
    assert "Widget &lt;b&gt;bold&lt;/b&gt; &amp; co" in output
    assert 'class="mark retired"' in output
    assert "<limit>" not in output
    assert "&lt;limit&gt;" in output


def test_timeline_html_marks_escalations_from_facts_timeline():
    facts = _facts(
        timeline=[
            TimelineEvent(
                at="2026-01-01T00:05:00+00:00", kind="escalation", group_id="g1", label="stuck"
            )
        ]
    )
    output = timeline_html(facts)
    assert output.count('class="mark escalation"') == 1
    assert "escalation: stuck" in output


def test_timeline_html_without_timestamps_returns_a_note_not_a_table():
    facts = _facts(groups=[GroupFacts(id="g1", name="Widget", state="completed")])
    output = timeline_html(facts)
    assert "<table" not in output
    assert "no timestamped sessions" in output


# ---------------------------------------------------------------- flowchart


def test_plan_outcome_flowchart_structural_minimum():
    facts = _facts()
    output = plan_outcome_flowchart(facts)
    assert output.startswith("flowchart LR")
    assert ":::ok" in output


def test_render_all_bundles_every_diagram_with_correct_first_lines():
    facts = _facts()
    diagrams = render_all(facts, Path("."))
    assert diagrams.timeline.startswith('<table class="timeline">')
    assert diagrams.plan_outcome.startswith("flowchart LR")
    assert diagrams.architecture_delta.splitlines()[0].startswith(("flowchart", "%%"))
    assert "sequenceDiagram" not in diagrams.model_dump_json()
