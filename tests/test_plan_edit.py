"""Tests for `orchestrator/grouping/plan_edit.py` — verbatim plan surgery and
the `plan-check` CLI guard (plan U1).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.grouping.plan_edit import (
    PlanEditError,
    extract_task_map_entries,
    render_task_map_block,
    split_units,
    validate_plan,
    verify_map_unchanged,
)
from orchestrator.grouping.plan_reader import task_map_block_span

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PLAN = (
    REPO_ROOT / "docs" / "plans" / "2026-08-26-001-fix-observatory-and-run-resilience-plan.md"
)


PLAN_TEXT = """# feat: sample plan

Some preamble prose.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-scaffold
    description: Create the scaffold
    slice: null
    files:
      - app/main.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-users-api
    description: Users CRUD routes
    slice: users
    files:
      - app/routes/users.py
    symbols: []
    depends_on: [u1-scaffold]
    implements: ["/api/users"]
    consumes: []
```

## Units

### U1. Scaffold

- **Summary**: Create the FastAPI scaffold.
- **Verification**:
  - The app boots.

### U2. Users API

- **Summary**: Users CRUD routes on the scaffold.
- **Verification**:
  - `/api/users` returns 200.
"""


def _raising_codegraph(*args, **kwargs):
    raise AssertionError(f"plan-check must never touch codegraph or an LLM (args={args})")


class TestExtractionAndReassembly:
    def test_all_units_reassembled_yield_byte_identical_map_and_sections(self):
        doc = extract_task_map_entries(PLAN_TEXT)
        assert doc is not None
        rebuilt_block = render_task_map_block(doc)
        span = task_map_block_span(PLAN_TEXT)
        assert rebuilt_block == PLAN_TEXT[span[0] : span[1]]

        head, unit_texts, order, tail = split_units(PLAN_TEXT)
        assert order == ("u1", "u2")
        rebuilt_units = "".join(unit_texts[u] for u in order)
        assert head + rebuilt_units + tail == PLAN_TEXT
        for unit_id in order:
            assert unit_texts[unit_id] in PLAN_TEXT

    def test_real_plan_round_trips_byte_identical(self):
        text = REAL_PLAN.read_text()
        doc = extract_task_map_entries(text)
        assert doc is not None
        span = task_map_block_span(text)
        assert render_task_map_block(doc) == text[span[0] : span[1]]

        head, unit_texts, order, tail = split_units(text)
        assert head + "".join(unit_texts[u] for u in order) + tail == text
        assert len(order) == 36

    def test_render_subset_keeps_bytes_of_selected_entries(self):
        doc = extract_task_map_entries(PLAN_TEXT)
        block = render_task_map_block(doc, ["u2-users-api"])
        assert "task_id: u2-users-api" in block
        assert "task_id: u1-scaffold" not in block
        # The surviving entry's bytes are untouched.
        assert doc.entries["u2-users-api"] in block

    def test_render_unknown_task_id_raises(self):
        doc = extract_task_map_entries(PLAN_TEXT)
        with pytest.raises(PlanEditError):
            render_task_map_block(doc, ["does-not-exist"])

    def test_no_task_map_returns_none(self):
        assert extract_task_map_entries("# just a plan\n\nno map here\n") is None

    def test_no_units_section_raises(self):
        with pytest.raises(PlanEditError):
            split_units("# just a plan\n\n## Task Map\n\n```yaml\n# x\n```\n")


class TestVerifyMapUnchanged:
    def test_accepts_added_bullets_inside_a_unit_body(self):
        after = PLAN_TEXT.replace(
            "- **Summary**: Create the FastAPI scaffold.",
            "- **Summary**: Create the FastAPI scaffold.\n"
            "- **Edge cases**: empty request body returns 400.",
        )
        assert verify_map_unchanged(PLAN_TEXT, after) == []

    def test_rejects_and_names_altered_depends_on_size_hints_and_task_id(self):
        after = PLAN_TEXT
        # depends_on altered on u2-users-api.
        after = after.replace(
            "depends_on: [u1-scaffold]", "depends_on: [u1-scaffold, u2-users-api]"
        )
        # size_hints added on u1-scaffold (a prospective-file class it didn't
        # carry before — a real semantic drift, not just prose).
        after = after.replace(
            "  - task_id: u1-scaffold\n"
            "    description: Create the scaffold\n"
            "    slice: null\n"
            "    files:\n"
            "      - app/main.py\n",
            "  - task_id: u1-scaffold\n"
            "    description: Create the scaffold\n"
            "    slice: null\n"
            "    files:\n"
            "      - app/main.py\n"
            "      - app/settings.py\n"
            "    size_hints:\n"
            "      app/settings.py: small\n",
        )
        # task_id itself renamed on the (already-modified) users task.
        after = after.replace("task_id: u2-users-api", "task_id: u2-users-api-renamed")

        diffs = verify_map_unchanged(PLAN_TEXT, after)
        joined = "\n".join(diffs)
        assert "u1-scaffold" in joined and "size_hints" in joined
        assert "u2-users-api" in joined
        assert "u2-users-api-renamed" in joined

    def test_reports_all_three_altered_entries_in_one_call(self):
        before = PLAN_TEXT
        after = before.replace("depends_on: [u1-scaffold]", "depends_on: []")
        after = after.replace('implements: ["/api/users"]', "implements: []")
        after = after.replace("slice: users", "slice: admin")

        diffs = verify_map_unchanged(before, after)
        # All three field changes land on the same task_id (u2-users-api), so
        # assert on the count of distinct diff messages directly.
        assert len(diffs) == 3
        assert all("u2-users-api" in diff for diff in diffs)
        fields_changed = {diff.split("'")[3] for diff in diffs}
        assert fields_changed == {"depends_on", "implements", "slice"}

    def test_reports_three_separately_altered_entries(self):
        plan = """```yaml
# orchestrator-task-map v1
tasks:
  - task_id: t1
    description: one
    depends_on: []
  - task_id: t2
    description: two
    depends_on: []
  - task_id: t3
    description: three
    depends_on: []
```
"""
        after = plan.replace(
            "  - task_id: t1\n    description: one\n    depends_on: []\n",
            "  - task_id: t1\n    description: one\n    depends_on: [t2]\n",
        )
        after = after.replace(
            "  - task_id: t2\n    description: two\n    depends_on: []\n",
            "  - task_id: t2\n    description: two\n    depends_on: [t1]\n",
        )
        after = after.replace(
            "  - task_id: t3\n    description: three\n    depends_on: []\n",
            "  - task_id: t3\n    description: three\n    depends_on: [t1]\n",
        )
        diffs = verify_map_unchanged(plan, after)
        assert len(diffs) == 3
        assert {d.split("'")[1] for d in diffs} == {"t1", "t2", "t3"}


class TestValidatePlan:
    def test_well_formed_plan_has_no_problems(self):
        assert validate_plan(PLAN_TEXT) == []

    def test_map_task_with_no_matching_unit_section(self):
        broken = PLAN_TEXT.replace("### U2. Users API", "### U9. Users API")
        problems = validate_plan(broken)
        assert any("u2-users-api" in p and "no matching" in p for p in problems)

    def test_unit_section_with_no_matching_map_entry(self):
        broken = PLAN_TEXT.replace("task_id: u2-users-api", "task_id: u9-users-api")
        problems = validate_plan(broken)
        assert any("u2" in p and "no matching task-map entry" in p for p in problems)

    def test_plan_without_map_or_units_is_valid(self):
        assert validate_plan("# a foreign plan\n\njust prose\n") == []


class TestPlanCheckCli:
    def test_exits_zero_on_well_formed_plan(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        exit_code = main(["plan-check", str(plan)])
        assert exit_code == 0
        assert "internally consistent" in capsys.readouterr().out

    def test_exits_nonzero_naming_offending_unit_missing_section(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT.replace("### U2. Users API", "### U9. Users API"))
        exit_code = main(["plan-check", str(plan)])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "u2-users-api" in out

    def test_exits_nonzero_naming_offending_unit_missing_map_entry(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT.replace("task_id: u2-users-api", "task_id: u9-users-api"))
        exit_code = main(["plan-check", str(plan)])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "u2" in out

    def test_against_flag_diffs_two_documents(self, tmp_path, capsys):
        before = tmp_path / "before-plan.md"
        after = tmp_path / "after-plan.md"
        before.write_text(PLAN_TEXT)
        after.write_text(PLAN_TEXT.replace("depends_on: [u1-scaffold]", "depends_on: []"))
        exit_code = main(["plan-check", str(before), "--against", str(after)])
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "u2-users-api" in out and "depends_on" in out

    def test_against_flag_exits_zero_when_unchanged(self, tmp_path, capsys):
        before = tmp_path / "before-plan.md"
        after = tmp_path / "after-plan.md"
        before.write_text(PLAN_TEXT)
        after.write_text(PLAN_TEXT)
        exit_code = main(["plan-check", str(before), "--against", str(after)])
        assert exit_code == 0
        assert "agree" in capsys.readouterr().out

    def test_missing_plan_is_actionable(self, tmp_path, capsys):
        exit_code = main(["plan-check", str(tmp_path / "nope-plan.md")])
        assert exit_code == 1
        assert "not found" in capsys.readouterr().err

    def test_completes_under_a_second_on_the_repos_largest_plan(self, capsys):
        start = time.perf_counter()
        exit_code = main(["plan-check", str(REAL_PLAN)])
        elapsed = time.perf_counter() - start
        assert exit_code == 0
        assert elapsed < 1.0
        capsys.readouterr()

    def test_never_touches_codegraph_or_llm(self, monkeypatch, capsys):
        # No CodegraphClient/JsonRunner is even constructed by `plan-check` — if
        # it ever were, patching subprocess.run to raise would prove it here.
        import subprocess

        monkeypatch.setattr(subprocess, "run", _raising_codegraph)
        exit_code = main(["plan-check", str(REAL_PLAN)])
        assert exit_code == 0
        capsys.readouterr()
