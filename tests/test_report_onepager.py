"""``orchestrator.report.onepager`` — the one-pager scaffold and its
validator (plan U5). Each test below trips exactly one validator rule so a
regression names which rule broke, not just that "validate" changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.report.facts import (
    ChangedFileFacts,
    GitRangeFacts,
    GroupFacts,
    RidFacts,
    RunFacts,
    SessionFacts,
    UnitFacts,
    VerificationFacts,
)
from orchestrator.report.onepager import (
    _BANNED_PHRASES,
    _MODAL_VERBS,
    body_without_title,
    scaffold,
    strip_pointer_comment,
    validate,
)

RUN_ID = "r20260101-000000"


def make_facts() -> RunFacts:
    return RunFacts(
        run_id=RUN_ID,
        plan_title="Test Plan",
        groups=[
            GroupFacts(
                id="g1",
                name="widget",
                state="completed",
                sessions=[SessionFacts(role="coder", generation=2)],
                escalations=[{"id": "0123456789abcdef", "kind": "stuck"}],
            )
        ],
        units=[
            UnitFacts(
                unit_id="u1",
                task_id="u1-widget",
                group_id="g1",
                title="Widget",
                verification=[
                    VerificationFacts(item_id="g1-1", description="widget works", status="pass")
                ],
                landed=True,
            )
        ],
        rids=[RidFacts(rid="R1", units=["u1"], landed=True)],
        changed_files=[ChangedFileFacts(path="foo.py", added=3, deleted=0, group_id="g1")],
        git_range=GitRangeFacts(base_sha="deadbeef01234", tip_sha="cafebabe56789", available=True),
    )


#: A clean, fully-valid document: 3 TL;DR bullets, 1 Problems found bullet,
#: 1 Run notes bullet, 1 Next steps bullet, every bullet ending in a valid
#: pointer, no banned phrases, no modal verbs, well under the word cap.
_CLEAN_TEXT = """# Test Plan — {run_id}

## TL;DR

- The widget group landed cleanly (g1)
- Requirement one is satisfied by unit one (R1)
- The widget file changed as planned (foo.py)

## Problems found

- No problems were recorded for this run (g1)

## Run notes

- Nothing was fixed by hand during this run (g1)

## Next steps

- Watch the widget file for regressions next run (foo.py)
""".format(run_id=RUN_ID)


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"expected exactly one occurrence of {old!r}"
    return text.replace(old, new, 1)


def test_clean_document_validates() -> None:
    assert validate(_CLEAN_TEXT, make_facts()) == []


# --------------------------------------------------------------- scaffold


def test_untouched_scaffold_fails_with_unknown_pointer_violations() -> None:
    text = scaffold(make_facts())
    violations = validate(text, make_facts())
    assert len(violations) >= 3
    assert all("unknown pointer 'POINTER'" in v for v in violations)


# --------------------------------------------------------------- headings


def test_heading_order_violation_trips_only_that_rule() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "## Problems found\n\n- No problems were recorded for this run (g1)\n\n"
        "## Run notes\n\n- Nothing was fixed by hand during this run (g1)\n",
        "## Run notes\n\n- Nothing was fixed by hand during this run (g1)\n\n"
        "## Problems found\n\n- No problems were recorded for this run (g1)\n",
    )
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "headings must be exactly one H1" in violations[0]
    assert "## Run notes" in violations[0]


def test_scaffold_has_four_sections_in_order() -> None:
    text = scaffold(make_facts())
    positions = [
        text.index(h) for h in ("## TL;DR", "## Problems found", "## Run notes", "## Next steps")
    ]
    assert positions == sorted(positions)


def test_missing_run_notes_section_fails_naming_both_rules() -> None:
    text = _replace_once(
        _CLEAN_TEXT, "## Run notes\n\n- Nothing was fixed by hand during this run (g1)\n\n", ""
    )
    violations = validate(text, make_facts())
    assert any("headings must be exactly one H1" in v for v in violations)
    assert "Run notes must have 1-8 bullets, found 0" in violations


def test_run_notes_bullet_without_pointer_is_named() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Nothing was fixed by hand during this run (g1)\n",
        "- Nothing was fixed by hand during this run\n",
    )
    violations = validate(text, make_facts())
    assert violations == [
        "bullet missing a trailing (<pointer>): - Nothing was fixed by hand during this run"
    ]


def test_escalation_id_and_session_label_are_valid_pointers() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Nothing was fixed by hand during this run (g1)\n",
        "- Answered the stuck escalation by relaunching (0123456789ab)\n"
        "- The second coder generation finished the work (g1/coder/gen2)\n",
    )
    assert validate(text, make_facts()) == []
    assert "0123456789ab" in scaffold(make_facts())
    assert "g1/coder/gen2" in scaffold(make_facts())


def test_modal_verb_in_run_notes_trips_that_rule() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Nothing was fixed by hand during this run (g1)\n",
        "- This should have been fixed by hand (g1)\n",
    )
    assert validate(text, make_facts()) == ["modal verb 'should' is not allowed in Run notes"]


def test_strip_pointer_comment_and_body_without_title() -> None:
    text = _CLEAN_TEXT + "\n<!-- valid pointers: g1, foo.py -->\n"
    stripped = strip_pointer_comment(text)
    assert "valid pointers" not in stripped
    assert stripped.startswith(f"# Test Plan — {RUN_ID}")
    body = body_without_title(text)
    assert body.startswith("## TL;DR")
    assert "valid pointers" not in body


def test_missing_h1_trips_heading_rule() -> None:
    text = _replace_once(_CLEAN_TEXT, f"# Test Plan — {RUN_ID}\n\n", "")
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "headings must be exactly one H1" in violations[0]


# ---------------------------------------------------------- bullet counts


def test_tldr_wrong_bullet_count_trips_only_that_rule() -> None:
    text = _replace_once(_CLEAN_TEXT, "- Requirement one is satisfied by unit one (R1)\n", "")
    violations = validate(text, make_facts())
    assert violations == ["TL;DR must have exactly 3 bullets, found 2"]


def test_section_too_many_bullets_trips_only_that_rule() -> None:
    extra = "".join(f"- extra problem number {n} (g1)\n" for n in range(2, 10))
    text = _replace_once(
        _CLEAN_TEXT,
        "- No problems were recorded for this run (g1)\n",
        "- No problems were recorded for this run (g1)\n" + extra,
    )
    violations = validate(text, make_facts())
    assert violations == ["Problems found must have 1-8 bullets, found 9"]


def test_eight_bullets_with_sub_bullets_pass_but_nine_top_level_fail() -> None:
    # Sub-bullets (indented "  - ") are continuations, never top-level bullets.
    eight = "".join(
        f"- extra problem number {n} (g1)\n  - detail {n}: a sub-bullet with no pointer\n"
        for n in range(2, 9)
    )
    base = "- No problems were recorded for this run (g1)\n"
    text = _replace_once(_CLEAN_TEXT, base, base + eight)
    assert validate(text, make_facts()) == []
    ninth = "- a ninth top-level problem (g1)\n"
    text = _replace_once(text, base, base + ninth)
    assert validate(text, make_facts()) == ["Problems found must have 1-8 bullets, found 9"]


def test_paragraph_before_bullets_validates() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "## Problems found\n\n",
        "## Problems found\n\nA plain paragraph of context with no pointer at all.\n\n",
    )
    assert validate(text, make_facts()) == []


def test_indented_continuation_without_pointer_validates() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Watch the widget file for regressions next run (foo.py)\n",
        "- Watch the widget file for regressions next run (foo.py)\n"
        "  it matters because the file changed without a test\n"
        "  - how: add a regression test first\n",
    )
    assert validate(text, make_facts()) == []


def test_paragraph_and_continuation_words_count_toward_the_cap() -> None:
    filler = " ".join(["word"] * 900)
    text = _replace_once(
        _CLEAN_TEXT,
        "## Problems found\n\n",
        f"## Problems found\n\n{filler}\n\n",
    )
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "exceeds 900 words" in violations[0]


def test_section_zero_bullets_trips_only_that_rule() -> None:
    text = _replace_once(_CLEAN_TEXT, "- No problems were recorded for this run (g1)\n", "")
    violations = validate(text, make_facts())
    assert violations == ["Problems found must have 1-8 bullets, found 0"]


# ----------------------------------------------------------------- pointers


def test_bullet_missing_pointer_suffix_trips_only_that_rule() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Watch the widget file for regressions next run (foo.py)\n",
        "- Watch the widget file for regressions next run\n",
    )
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "missing a trailing (<pointer>)" in violations[0]


def test_unknown_pointer_trips_only_that_rule() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Watch the widget file for regressions next run (foo.py)\n",
        "- Watch the widget file for regressions next run (bogus.py)\n",
    )
    violations = validate(text, make_facts())
    assert violations == [
        "unknown pointer 'bogus.py' in bullet: "
        "- Watch the widget file for regressions next run (bogus.py)"
    ]


# -------------------------------------------------------------- word cap


def test_word_cap_exceeded_trips_only_that_rule() -> None:
    long_body = " ".join(["word"] * 901)
    text = _replace_once(
        _CLEAN_TEXT,
        "- The widget group landed cleanly (g1)\n",
        f"- {long_body} (g1)\n",
    )
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "exceeds 900 words" in violations[0]


def test_word_cap_allows_exactly_900_words() -> None:
    # The clean text already carries a handful of words; fill up to the cap.
    already = sum(
        len(line[2:].rsplit("(", 1)[0].split())
        for line in _CLEAN_TEXT.splitlines()
        if line.startswith("- ")
    )
    filler = " ".join(["word"] * (900 - already - 4))
    text = _replace_once(
        _CLEAN_TEXT,
        "- The widget group landed cleanly (g1)\n",
        f"- The widget group landed cleanly {filler} (g1)\n",
    )
    assert validate(text, make_facts()) == []


def test_word_cap_excludes_pointer_text() -> None:
    # The pointer itself is long, but pointer text is excluded from the word
    # count, so this must still validate clean.
    facts = make_facts()
    facts.changed_files.append(
        ChangedFileFacts(path="a/very/long/nested/package/path/module.py", group_id="g1")
    )
    text = _replace_once(
        _CLEAN_TEXT,
        "- The widget group landed cleanly (g1)\n",
        "- The widget group landed cleanly (a/very/long/nested/package/path/module.py)\n",
    )
    assert validate(text, facts) == []


# ----------------------------------------------------------- banned phrases


@pytest.mark.parametrize("phrase", _BANNED_PHRASES)
def test_banned_phrase_trips_only_that_rule(phrase: str) -> None:
    # Injected into TL;DR, never Problems found: "it should be noted" itself
    # contains the modal verb "should", which would also trip that rule.
    text = _replace_once(
        _CLEAN_TEXT,
        "- The widget group landed cleanly (g1)\n",
        f"- The widget group landed cleanly, {phrase} (g1)\n",
    )
    violations = validate(text, make_facts())
    assert violations == [f"banned phrase: {phrase!r}"]


# -------------------------------------------------------------- modal verbs


@pytest.mark.parametrize("verb", _MODAL_VERBS)
def test_modal_verb_in_problems_found_trips_only_that_rule(verb: str) -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- No problems were recorded for this run (g1)\n",
        f"- This {verb} be a problem worth flagging (g1)\n",
    )
    violations = validate(text, make_facts())
    assert violations == [f"modal verb {verb!r} is not allowed in Problems found"]


def test_modal_verb_outside_problems_found_is_allowed() -> None:
    text = _replace_once(
        _CLEAN_TEXT,
        "- Watch the widget file for regressions next run (foo.py)\n",
        "- We should watch the widget file for regressions next run (foo.py)\n",
    )
    assert validate(text, make_facts()) == []


# ------------------------------------------------------------------- purity


def test_validate_never_rewrites_the_file(tmp_path: Path) -> None:
    path = tmp_path / "one-pager.md"
    path.write_text(_CLEAN_TEXT)
    before = path.read_bytes()

    violations = validate(path.read_text(), make_facts())

    assert violations == []
    assert path.read_bytes() == before
