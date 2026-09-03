"""``render_html`` (plan U4, report v2 U1/U2/U4) — the synthetic-facts edge
cases the two real fixture runs don't exercise: a failed verification item,
an absent or present one-pager (rendered to HTML with pointer anchors), an
escalation with its operator answer, the pan/zoom diagram shell, and the
``StrictUndefined`` contract.
"""

from __future__ import annotations

from pathlib import Path

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
from orchestrator.report.gitview import group_diff, split_diff
from orchestrator.report.html import (
    _env,
    _link_pointers,
    markdown_html,
    render_html,
    render_one_pager_html,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CLEAN_FIXTURE_ID = "r20260829-162627"
_CLEAN_FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "runs" / _CLEAN_FIXTURE_ID

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


# ------------------------------------------------ v2.1: open evidence items


def test_evidence_items_start_expanded():
    html = render_html(_facts(failing=False), _diagrams())
    assert '<details id="item-g1-1" class="cell-pass" open>' in html


# --------------------------------------------- v2.1: diffs, spec, context


_DIFF = (
    "diff --git a/x.py b/x.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,4 +1,5 @@\n"
    " keep\n"
    "-old one\n"
    "-old two\n"
    "+new one\n"
    " tail\n"
    "+added\n"
    "+<script>alert(1)</script>\n"
    "\\ No newline at end of file\n"
    "diff --git a/img.png b/img.png\n"
    "Binary files a/img.png and b/img.png differ\n"
)


def test_split_diff_pairs_removed_and_added_runs_side_by_side():
    files = split_diff(_DIFF)
    assert [f.path for f in files] == ["x.py", "img.png"]
    assert files[1].binary and files[1].rows == []
    rows = files[0].rows
    assert [r.kind for r in rows] == ["hunk", "ctx", "change", "del", "ctx", "add", "add"]
    change = rows[2]
    assert (change.old_no, change.old, change.new_no, change.new) == (2, "old one", 2, "new one")
    only_del = rows[3]
    assert (only_del.old_no, only_del.old, only_del.new) == (3, "old two", None)
    assert (rows[4].old_no, rows[4].new_no) == (4, 3)  # line numbers advance per side
    assert (rows[5].old, rows[5].new_no, rows[5].new) == (None, 4, "added")


def test_split_diff_cells_are_escaped_in_the_rendered_table():
    facts = _facts(failing=False)
    facts.groups[0].merge_sha = "0" * 40
    import orchestrator.report.html as html_module
    from orchestrator.report.gitview import GroupDiff

    original = html_module.group_diff
    html_module.group_diff = lambda *_a, **_k: GroupDiff(_DIFF, False, 2, 4, 2)
    try:
        html = render_html(facts, _diagrams(), repo_root=Path("."))
    finally:
        html_module.group_diff = original
    assert '<table class="split-diff">' in html
    assert "<script>alert(1)</script>" not in html
    assert '<td class="new txt add">&lt;script&gt;alert(1)&lt;/script&gt;</td>' in html
    assert '<td class="old txt del">old two</td><td class="ln empty"></td>' in html
    assert '<div class="diff-path">img.png <span class="muted">(binary)</span></div>' in html


def test_markdown_html_renders_pipe_tables():
    rendered = str(markdown_html("| Path | Purpose |\n|---|---|\n| `a` | b |\n"))
    assert "<table>" in rendered and "<th>Path</th>" in rendered and "<td>b</td>" in rendered


def test_group_diff_truncates_on_a_line_boundary():
    from orchestrator.report.facts import build_facts

    facts = build_facts(_REPO_ROOT, _CLEAN_FIXTURE_ID, run_dir=_CLEAN_FIXTURE_DIR)
    sha = facts.groups[0].merge_sha
    assert sha
    full = group_diff(_REPO_ROOT, sha)
    assert not full.truncated and full.files > 0 and full.added > 0
    small = group_diff(_REPO_ROOT, sha, max_bytes=200)
    assert small.truncated
    assert small.text.endswith("\n") and 0 < len(small.text.encode()) <= 200
    assert group_diff(_REPO_ROOT, sha, max_bytes=5).text == ""  # no line fits: nothing half-cut
    assert small.files == full.files  # counts come from numstat, not the cut text


def test_real_fixture_renders_a_diff_spec_section_and_shared_context():
    from orchestrator.report.diagrams import render_all
    from orchestrator.report.facts import build_facts

    facts = build_facts(_REPO_ROOT, _CLEAN_FIXTURE_ID, run_dir=_CLEAN_FIXTURE_DIR)
    diagrams = render_all(facts, _REPO_ROOT)
    html = render_html(facts, diagrams, repo_root=_REPO_ROOT, run_dir=_CLEAN_FIXTURE_DIR)
    assert html.count('<details class="diff"') == len(facts.groups) == 3
    assert '<table class="split-diff">' in html
    assert '<td class="new txt add">' in html
    assert "diff truncated" not in html
    assert "no merge commit found" not in html
    assert 'id="shared-context"' in html
    assert "Worker ground rules" in html
    assert 'id="spec-g1"' in html
    assert "Spec given to this group" in html
    assert 'id="section-u1"' in html and "Plan section U1" in html
    assert '<a href="#shared-context">Shared context</a>' in html

    # Without repo_root/run_dir those parts are absent, not broken.
    bare = render_html(facts, diagrams)
    assert '<details class="diff"' not in bare
    assert "no merge commit found" not in bare
    assert 'id="shared-context"' not in bare
    assert 'id="spec-g1"' in bare  # the spec comes from facts, not the paths


def test_shared_context_states_digest_or_full_plan_from_the_file_itself():
    from orchestrator.report.diagrams import render_all
    from orchestrator.report.facts import build_facts
    from orchestrator.report.html import base_context_plan_mode

    fixtures = _REPO_ROOT / "tests" / "fixtures" / "runs"
    assert (
        base_context_plan_mode((fixtures / "r20260829-162627" / "base-context.md").read_text())
        == "digest"
    )
    assert (
        base_context_plan_mode((fixtures / "r20260828-220035" / "base-context.md").read_text())
        == "document"
    )
    assert base_context_plan_mode("# Base context\n\n## Worker ground rules\n") is None

    # The older run's workers saw every unit's section; the report must say so.
    old_id = "r20260828-220035"
    old = build_facts(_REPO_ROOT, old_id, run_dir=fixtures / old_id)
    html = render_html(old, render_all(old, _REPO_ROOT), run_dir=fixtures / old_id)
    assert "full plan document" in html
    assert "no worker saw another unit's full section" not in html
    assert (
        "<code>Plan document (2026-08-28-001-feat-deterministic-grouper-advisory-plan.md)</code>"
        in html
    )

    new_id = "r20260829-162627"
    new = build_facts(_REPO_ROOT, new_id, run_dir=fixtures / new_id)
    html = render_html(new, render_all(new, _REPO_ROOT), run_dir=fixtures / new_id)
    assert "no worker saw another unit's full section" in html
    assert "full plan document" not in html


def test_group_without_merge_sha_says_so_only_when_diffs_are_enabled():
    facts = _facts(failing=False)
    with_root = render_html(facts, _diagrams(), repo_root=_REPO_ROOT)
    assert "no merge commit found for this group" in with_root
    assert "no merge commit found" not in render_html(facts, _diagrams())


def test_spec_markdown_is_escaped_not_injected():
    facts = _facts(failing=False)
    facts.groups[0].spec = "Do the thing.\n\n<script>alert(1)</script>\n"
    html = render_html(facts, _diagrams())
    assert 'id="spec-g1"' in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_link_pointers_leaves_sub_bullets_and_paragraphs_untouched():
    text = (
        "A paragraph mentioning (g1) stays as it is.\n"
        "- Fix the widget: it matters (g1)\n"
        "  - how: open the file (g1)\n"
        "  plain continuation (g1)\n"
    )
    linked = _link_pointers(text, {"g1": "#group-g1"})
    assert linked.splitlines() == [
        "A paragraph mentioning (g1) stays as it is.",
        "- Fix the widget: it matters ([g1](#group-g1))",
        "  - how: open the file (g1)",
        "  plain continuation (g1)",
    ]
