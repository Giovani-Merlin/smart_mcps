"""``orchestrator/report/markdown.py`` — trial A (changelog entry + RUNLOG)
and trial C (PR body), the synthetic-facts cases the two real fixture runs
don't exercise directly: a stale-only failure, RUNLOG idempotency across two
runs, and the postmortem gate. See
``docs/plans/2026-09-02-001-feat-run-report-plan.md`` U3.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.report.diagrams import Diagrams
from orchestrator.report.facts import (
    GitRangeFacts,
    GroupFacts,
    RunFacts,
    SessionFacts,
    UnitFacts,
    VerificationFacts,
)
from orchestrator.report.markdown import (
    render_changelog_entry,
    render_fragments,
    render_pr_body,
    update_runlog,
)

RUN_ID = "r20260101-000000"


def _diagrams() -> Diagrams:
    return Diagrams(
        timeline="gantt\n    section g1\n",
        plan_outcome="flowchart LR\n    a --> b\n",
        architecture_delta="%% no python files changed",
        howto_sequences=[],
        howto_note="no entry points",
    )


def _clean_facts() -> RunFacts:
    return RunFacts(
        run_id=RUN_ID,
        plan_title="Synthetic report-markdown fixture",
        plan_objective="Ship the widget.\n\nSecond paragraph.",
        git_range=GitRangeFacts(available=True, base_sha="a" * 40, tip_sha="b" * 40),
        groups=[
            GroupFacts(
                id="g1",
                name="Widget",
                summary="build the widget",
                report_summary="Implemented the widget end to end with tests.",
                state="completed",
                sessions=[
                    SessionFacts(
                        role="coder",
                        model="model-a",
                        started_at="2026-01-01T00:00:00+00:00",
                        ended_at="2026-01-01T01:00:00+00:00",
                        tokens={"input": 100, "output": 50},
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
                        status="pass",
                        evidence="ran it",
                    )
                ],
                landed=True,
            )
        ],
        trouble=False,
    )


def _troubled_facts() -> RunFacts:
    facts = _clean_facts()
    facts.groups[0].surprises = [
        {"kind": "other", "description": "found a surprise", "path": "groups/g1/report-g1-r1.json"}
    ]
    facts.groups[0].failure = "boom, it broke"
    facts.units[0].verification[0].status = "fail"
    facts.units[0].landed = False
    facts.trouble = True
    return facts


def _stale_only_facts() -> RunFacts:
    """A group that failed once then succeeded: ``failure`` is nulled and
    ``trouble`` stays False (report U1's stale-failure rule) — the markdown
    renderers must gate purely on ``facts.trouble``, not on group state."""
    facts = _clean_facts()
    facts.groups[0].stale_failure = True
    facts.trouble = False
    return facts


# --------------------------------------------------------------- fragments


def test_fragment_reports_state_summary_verification_tokens_and_elapsed():
    fragments = render_fragments(_clean_facts())
    fragment = fragments["g1"]
    assert "state: completed" in fragment
    assert "Implemented the widget end to end with tests." in fragment
    assert "1/1 pass" in fragment
    assert "150 total across 1 session(s)" in fragment
    assert "1h0m" in fragment
    assert "g1-1" in fragment and "pass" in fragment


def test_fragment_trims_summary_to_twenty_words():
    facts = _clean_facts()
    facts.groups[0].report_summary = " ".join(f"word{i}" for i in range(40))
    fragment = render_fragments(facts)["g1"]
    summary_line = next(line for line in fragment.splitlines() if "**Summary**" in line)
    # 20 kept words plus the ellipsis marker.
    assert summary_line.count("word") == 20


def test_fragment_lists_surprises_verbatim_with_their_artifact_path():
    fragments = render_fragments(_troubled_facts())
    assert "found a surprise" in fragments["g1"]
    assert "groups/g1/report-g1-r1.json" in fragments["g1"]


# ------------------------------------------------------------- changelog


def test_every_bullet_line_ends_with_a_closing_paren_clean_run():
    entry = render_changelog_entry(_clean_facts(), _diagrams())
    bullets = [line for line in entry.splitlines() if line.startswith("- ")]
    assert bullets
    assert all(line.endswith(")") for line in bullets)


def test_every_bullet_line_ends_with_a_closing_paren_troubled_run():
    entry = render_changelog_entry(_troubled_facts(), _diagrams())
    bullets = [line for line in entry.splitlines() if line.startswith("- ")]
    assert bullets
    assert all(line.endswith(")") for line in bullets)


def test_clean_run_has_no_postmortem_section():
    entry = render_changelog_entry(_clean_facts(), _diagrams())
    assert "## Postmortem" not in entry


def test_troubled_run_has_postmortem_naming_failure_and_surprise_verbatim():
    entry = render_changelog_entry(_troubled_facts(), _diagrams())
    assert "## Postmortem" in entry
    postmortem = entry.split("## Postmortem", 1)[1]
    assert "boom, it broke" in postmortem
    assert "found a surprise" in postmortem
    assert "groups/g1/report-g1-r1.json" in postmortem


def test_stale_only_failure_does_not_trigger_postmortem():
    entry = render_changelog_entry(_stale_only_facts(), _diagrams())
    assert "## Postmortem" not in entry


def test_changelog_embeds_the_gantt_and_flowchart_diagrams_as_mermaid_fences():
    entry = render_changelog_entry(_clean_facts(), _diagrams())
    assert "```mermaid\ngantt" in entry
    assert "```mermaid\nflowchart LR" in entry


# --------------------------------------------------------------- RUNLOG.md


def test_runlog_is_idempotent_across_two_runs_of_the_same_id(tmp_path: Path):
    runlog = tmp_path / "RUNLOG.md"
    entry = render_changelog_entry(_clean_facts(), _diagrams())

    first = update_runlog(runlog, RUN_ID, entry)
    runlog.write_text(first)
    second = update_runlog(runlog, RUN_ID, entry)

    assert second.count(f"<!-- run:{RUN_ID} -->") == 1
    assert second.count(f"<!-- /run:{RUN_ID} -->") == 1


def test_runlog_never_touches_another_runs_marked_block(tmp_path: Path):
    runlog = tmp_path / "RUNLOG.md"
    other_run_id = "r20260102-000000"
    other_entry = "## other run entry\n\n- **Outcome**: 1/1 groups completed (`state.json`)\n"
    seeded = f"<!-- run:{other_run_id} -->\n{other_entry}<!-- /run:{other_run_id} -->\n"
    runlog.write_text(seeded)

    entry = render_changelog_entry(_clean_facts(), _diagrams())
    updated = update_runlog(runlog, RUN_ID, entry)

    assert other_entry in updated
    assert f"<!-- run:{RUN_ID} -->" in updated


# ---------------------------------------------------------------- PR body


def test_pr_body_has_the_five_headings_in_order():
    body = render_pr_body(_clean_facts())
    headings = ["## Motivation", "## Changes", "## Risks", "## Testing", "## Handoff"]
    positions = [body.index(h) for h in headings]
    assert positions == sorted(positions)


def test_pr_body_omits_postmortem_when_no_trouble():
    body = render_pr_body(_clean_facts())
    assert "## Postmortem" not in body


def test_pr_body_has_postmortem_when_troubled():
    body = render_pr_body(_troubled_facts())
    assert "## Postmortem" in body
    assert "boom, it broke" in body


def test_pr_body_stale_only_failure_omits_postmortem():
    body = render_pr_body(_stale_only_facts())
    assert "## Postmortem" not in body


def test_pr_body_every_bullet_ends_with_a_closing_paren():
    body = render_pr_body(_troubled_facts())
    bullets = [line for line in body.splitlines() if line.startswith("- ")]
    assert bullets
    assert all(line.endswith(")") for line in bullets)
