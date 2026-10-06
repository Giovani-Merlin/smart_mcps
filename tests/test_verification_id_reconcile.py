"""A spec rewrite keeps the plan's verification item ids (r20261006-050234).

The rewrite speccer re-ids items (`g9-blockers-tests` for the plan's `g9-1`);
groups.json, the merge log, the report facts and the one-pager pointer legend
all key on the plan's ids, so a rewritten group's eight passes were reported
as "0/12 pass, unit not landed". Only ids change — order, text and flags are
the speccer's.
"""

from __future__ import annotations

from orchestrator.model import VerificationItem, reconcile_verification_ids


def item(id_: str, description: str) -> VerificationItem:
    return VerificationItem(id=id_, description=description)


PLAN = [
    item("g9-1", "Run: `uv run pytest tests/test_analysis_blockers.py -q` Pass: green"),
    item("g9-2", "Run: `uv run ruff check src/infinity_skills/analysis` Pass: no findings."),
    item("g9-3", "Run (driver): `grep -E researcher run.log` Pass: role is researcher"),
]


def test_items_that_keep_a_plan_id_are_untouched_and_claim_it():
    rewritten = [item("g9-2", "Run: `uv run ruff check src` Pass: clean"), item("g9-1", "x")]
    out = reconcile_verification_ids(PLAN, rewritten)
    assert [i.id for i in out] == ["g9-2", "g9-1"]
    assert out[0].description == "Run: `uv run ruff check src` Pass: clean"


def test_a_reworded_item_takes_the_closest_unclaimed_plan_id():
    rewritten = [
        item(
            "g9-blockers-tests",
            "Run: `uv run pytest tests/test_analysis_blockers.py -q` Pass: 8 passed",
        ),
        item("g9-ruff", "Run: `uv run ruff check src/infinity_skills/analysis` Pass: no findings"),
    ]
    out = reconcile_verification_ids(PLAN, rewritten)
    assert [i.id for i in out] == ["g9-1", "g9-2"]
    # Descriptions and order are the speccer's.
    assert out[0].description.endswith("8 passed")


def test_a_genuinely_new_item_keeps_the_speccers_id():
    rewritten = [item("g9-no-lint-command-list", "Pass: no command-head list anywhere")]
    out = reconcile_verification_ids(PLAN, rewritten)
    assert [i.id for i in out] == ["g9-no-lint-command-list"]


def test_each_plan_id_is_claimed_at_most_once_and_the_best_match_wins():
    rewritten = [
        item("a", "Run: `uv run ruff check src/infinity_skills/analysis` Pass: no findings (lint)"),
        item("b", "Run: `uv run ruff check src/infinity_skills/analysis` Pass: no findings."),
    ]
    out = reconcile_verification_ids(PLAN, rewritten)
    ids = [i.id for i in out]
    assert ids.count("g9-2") == 1
    assert ids[1] == "g9-2"  # the verbatim match, not the earlier looser one
    assert ids[0] == "a"


def test_driver_run_flags_survive_an_id_swap():
    rewritten = [
        item("g9-role", "Run (driver): `grep -E researcher run.log` Pass: role is researcher")
    ]
    out = reconcile_verification_ids(PLAN, rewritten)
    assert out[0].id == "g9-3" and out[0].driver_run is True
