"""Mermaid renderers in ``orchestrator/report/diagrams.py`` (plan U2) — the
synthetic-facts edge cases the two real fixture runs don't exercise: an
unavailable git range, ``codegraph`` scrubbed from ``PATH``, and the
structural minimum every renderer's output must meet. See
``docs/plans/2026-09-02-001-feat-run-report-plan.md`` U2.
"""

from __future__ import annotations

from pathlib import Path


from orchestrator.report.diagrams import (
    architecture_delta,
    howto_sequences,
    plan_outcome_flowchart,
    render_all,
    timeline_gantt,
)
from orchestrator.report.facts import (
    GitRangeFacts,
    GroupFacts,
    RunFacts,
    SessionFacts,
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
                        role="coder", generation=1, started_at="2026-01-01T00:00:00+00:00"
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


def test_howto_sequences_returns_empty_and_note_when_codegraph_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    facts = _facts(git_range=GitRangeFacts(base_sha="a", tip_sha="b", available=True))
    diagrams, note = howto_sequences(facts, Path("."))
    assert diagrams == []
    assert note is not None
    assert "codegraph" in note


def test_timeline_gantt_structural_minimum():
    facts = _facts()
    output = timeline_gantt(facts)
    assert output.startswith("gantt")
    assert "    section g1- Widget" in output


def test_plan_outcome_flowchart_structural_minimum():
    facts = _facts()
    output = plan_outcome_flowchart(facts)
    assert output.startswith("flowchart LR")
    assert ":::ok" in output


def test_render_all_bundles_every_diagram_with_correct_first_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    facts = _facts()
    diagrams = render_all(facts, Path("."))
    assert diagrams.timeline.startswith("gantt")
    assert diagrams.plan_outcome.startswith("flowchart LR")
    assert diagrams.architecture_delta.splitlines()[0].startswith(("flowchart", "%%"))
    assert diagrams.howto_sequences == []
    assert diagrams.howto_note is not None
    for sequence in diagrams.howto_sequences:
        assert sequence.startswith("sequenceDiagram")


def test_gantt_handles_missing_ended_at_without_raising():
    facts = _facts(
        groups=[
            GroupFacts(
                id="g1",
                name="Widget",
                state="completed",
                sessions=[
                    SessionFacts(role="coder", generation=1, started_at="2026-01-01T00:00:00+00:00")
                ],
            )
        ]
    )
    output = timeline_gantt(facts)
    assert output.startswith("gantt")
    assert ":done," in output
    assert ":milestone," in output
