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
    UnitFacts,
    VerificationFacts,
)
from orchestrator.report.onepager import _BANNED_PHRASES, _MODAL_VERBS, scaffold, validate

RUN_ID = "r20260101-000000"


def make_facts() -> RunFacts:
    return RunFacts(
        run_id=RUN_ID,
        plan_title="Test Plan",
        groups=[GroupFacts(id="g1", name="widget", state="completed")],
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
#: 1 Next steps bullet, every bullet ending in a valid pointer, no banned
#: phrases, no modal verbs, well under the word cap.
_CLEAN_TEXT = """# Test Plan — {run_id}

## TL;DR

- The widget group landed cleanly (g1)
- Requirement one is satisfied by unit one (R1)
- The widget file changed as planned (foo.py)

## Problems found

- No problems were recorded for this run (g1)

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
        "## Next steps\n\n- Watch the widget file for regressions next run (foo.py)\n",
        "## Next steps\n\n- Watch the widget file for regressions next run (foo.py)\n\n"
        "## Problems found\n\n- No problems were recorded for this run (g1)\n",
    )
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "headings must be exactly one H1" in violations[0]


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
    extra = "".join(f"- extra problem number {n} (g1)\n" for n in range(2, 7))
    text = _replace_once(
        _CLEAN_TEXT,
        "- No problems were recorded for this run (g1)\n",
        "- No problems were recorded for this run (g1)\n" + extra,
    )
    violations = validate(text, make_facts())
    assert violations == ["Problems found must have 1-5 bullets, found 6"]


def test_section_zero_bullets_trips_only_that_rule() -> None:
    text = _replace_once(_CLEAN_TEXT, "- No problems were recorded for this run (g1)\n", "")
    violations = validate(text, make_facts())
    assert violations == ["Problems found must have 1-5 bullets, found 0"]


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
    long_body = " ".join(["word"] * 310)
    text = _replace_once(
        _CLEAN_TEXT,
        "- The widget group landed cleanly (g1)\n",
        f"- {long_body} (g1)\n",
    )
    violations = validate(text, make_facts())
    assert len(violations) == 1
    assert "exceeds 300 words" in violations[0]


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
