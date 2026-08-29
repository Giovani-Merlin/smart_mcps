"""Tests for `orchestrate split` — seam-addressable, overridable plan
splitting via verbatim plan surgery (plan U2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.grouping.advisory import AdvisoryReport, finding_task_groups
from orchestrator.grouping.plan_edit import (
    PlanEditError,
    add_frontmatter_field,
    add_predecessor_note,
    assign_tasks,
    extract_task_map_entries,
    split_plan,
    split_units,
)

PLAN_TEXT = """---
title: sample plan
type: feat
date: 2026-08-29
---

# feat: sample plan

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
  - task_id: u3-orders-api
    description: Orders CRUD routes
    slice: orders
    files:
      - app/routes/orders.py
    symbols: []
    depends_on: []
    implements: ["/api/orders"]
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

### U3. Orders API

- **Summary**: Orders CRUD routes on the scaffold.
- **Verification**:
  - `/api/orders` returns 200.
"""

PLAN_NO_FRONTMATTER = PLAN_TEXT.split("\n\n", 1)[1]


def _combined(documents):
    task_ids = [t for doc in documents for t in doc.task_ids]
    unit_ids = [u for doc in documents for u in doc.unit_ids]
    return task_ids, unit_ids


class TestAssignTasks:
    def test_every_task_assigned_once_succeeds(self):
        indices = assign_tasks(
            ["u1-scaffold", "u2-users-api", "u3-orders-api"],
            [["u1-scaffold", "u2-users-api"], ["u3-orders-api"]],
        )
        assert indices == [0, 0, 1]

    def test_unassigned_task_named_in_error(self):
        with pytest.raises(PlanEditError, match="u3-orders-api"):
            assign_tasks(
                ["u1-scaffold", "u2-users-api", "u3-orders-api"],
                [["u1-scaffold"], ["u2-users-api"]],
            )

    def test_doubly_assigned_task_named_in_error(self):
        with pytest.raises(PlanEditError, match="u1-scaffold"):
            assign_tasks(
                ["u1-scaffold", "u2-users-api"],
                [["u1-scaffold"], ["u1-scaffold", "u2-users-api"]],
            )

    def test_both_problems_named_in_one_error(self):
        with pytest.raises(PlanEditError) as excinfo:
            assign_tasks(
                ["t1", "t2", "t3"],
                [["t1"], ["t1"]],
            )
        message = str(excinfo.value)
        assert "t1" in message and "t2" in message and "t3" in message

    def test_unknown_task_id_in_groups_is_rejected(self):
        with pytest.raises(PlanEditError, match="does-not-exist"):
            assign_tasks(["t1"], [["t1", "does-not-exist"]])


class TestSplitPlan:
    def test_two_way_split_is_byte_identical_combined(self):
        documents = split_plan(PLAN_TEXT, [["u1-scaffold", "u2-users-api"], ["u3-orders-api"]])
        assert len(documents) == 2

        task_ids, unit_ids = _combined(documents)
        original_doc = extract_task_map_entries(PLAN_TEXT)
        assert sorted(task_ids) == sorted(original_doc.order)
        _, _, original_unit_order, _ = split_units(PLAN_TEXT)
        assert sorted(unit_ids) == sorted(original_unit_order)

        for document in documents:
            for task_id in document.task_ids:
                assert original_doc.entries[task_id] in document.text
            for unit_id in document.unit_ids:
                heading = {"u1": "### U1.", "u2": "### U2.", "u3": "### U3."}[unit_id]
                assert heading in document.text

    def test_no_unit_lost_or_duplicated(self):
        documents = split_plan(PLAN_TEXT, [["u1-scaffold"], ["u2-users-api", "u3-orders-api"]])
        _, unit_ids = _combined(documents)
        assert sorted(unit_ids) == ["u1", "u2", "u3"]
        assert len(unit_ids) == len(set(unit_ids))

    def test_three_way_split(self):
        documents = split_plan(PLAN_TEXT, [["u1-scaffold"], ["u2-users-api"], ["u3-orders-api"]])
        assert [d.unit_ids for d in documents] == [("u1",), ("u2",), ("u3",)]

    def test_plan_without_task_map_raises(self):
        with pytest.raises(PlanEditError):
            split_plan("# no map here\n\n## Units\n\n### U1. Foo\n", [["t1"]])

    def test_unassigned_task_is_rejected(self):
        with pytest.raises(PlanEditError, match="u3-orders-api"):
            split_plan(PLAN_TEXT, [["u1-scaffold", "u2-users-api"]])

    def test_doubly_assigned_task_is_rejected(self):
        with pytest.raises(PlanEditError, match="u1-scaffold"):
            split_plan(
                PLAN_TEXT,
                [["u1-scaffold", "u2-users-api"], ["u1-scaffold", "u3-orders-api"]],
            )

    def test_original_plan_text_is_never_mutated(self):
        before = PLAN_TEXT
        split_plan(PLAN_TEXT, [["u1-scaffold"], ["u2-users-api", "u3-orders-api"]])
        assert PLAN_TEXT == before


class TestFrontmatterHelpers:
    def test_add_field_to_existing_frontmatter(self):
        text = "---\ntitle: x\n---\n\nbody\n"
        updated = add_frontmatter_field(text, "split_from", "orig-plan.md")
        assert updated.startswith("---\ntitle: x\nsplit_from: orig-plan.md\n---\n\nbody\n")

    def test_add_field_with_no_existing_frontmatter(self):
        text = "# a plan\n\nbody\n"
        updated = add_frontmatter_field(text, "split_from", "orig-plan.md")
        assert updated == "---\nsplit_from: orig-plan.md\n---\n\n# a plan\n\nbody\n"

    def test_add_predecessor_note_after_frontmatter(self):
        text = "---\ntitle: x\n---\n\nbody\n"
        updated = add_predecessor_note(text, "> assumes part1 is merged.")
        assert updated == "---\ntitle: x\n---\n\n> assumes part1 is merged.\n\nbody\n"

    def test_add_predecessor_note_with_no_frontmatter(self):
        text = "# a plan\n\nbody\n"
        updated = add_predecessor_note(text, "> note")
        assert updated == "\n> note\n# a plan\n\nbody\n"


class TestFindingTaskGroups:
    def test_disconnected_finding_yields_task_sets(self):
        finding = AdvisoryReport.model_validate(
            {
                "plan_path": "x",
                "granularities": [],
                "cohesion": [
                    {
                        "kind": "disconnected",
                        "message": "m",
                        "task_sets": [["t1", "t2"], ["t3"]],
                    }
                ],
            }
        ).cohesion[0]
        assert finding_task_groups(finding) == [["t1", "t2"], ["t3"]]

    def test_serial_finding_with_direct_cut_yields_two_groups(self):
        finding = AdvisoryReport.model_validate(
            {
                "plan_path": "x",
                "granularities": [],
                "cohesion": [
                    {
                        "kind": "serial",
                        "message": "m",
                        "boundary": {"tasks_before": ["t1"], "tasks_after": ["t2", "t3"]},
                    }
                ],
            }
        ).cohesion[0]
        assert finding_task_groups(finding) == [["t1"], ["t2", "t3"]]

    def test_serial_finding_with_only_valleys_is_not_addressable(self):
        finding = AdvisoryReport.model_validate(
            {
                "plan_path": "x",
                "granularities": [],
                "cohesion": [
                    {
                        "kind": "serial",
                        "message": "m",
                        "boundary": {"valleys": [{"tasks_before": ["t1"], "tasks_after": ["t2"]}]},
                    }
                ],
            }
        ).cohesion[0]
        assert finding_task_groups(finding) is None

    def test_monolithic_finding_is_not_addressable(self):
        finding = AdvisoryReport.model_validate(
            {
                "plan_path": "x",
                "granularities": [],
                "cohesion": [
                    {"kind": "monolithic", "message": "m", "boundary": {"modularity": 0.1}}
                ],
            }
        ).cohesion[0]
        assert finding_task_groups(finding) is None


class TestSplitCli:
    def _write_advisory(self, repo: Path, name: str, cohesion: list[dict]) -> None:
        out_dir = repo / ".orchestrator" / "groupings" / name / "preview"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "version": 1,
            "plan_path": "sample-plan.md",
            "granularities": [],
            "cohesion": cohesion,
        }
        (out_dir / "advisory.json").write_text(json.dumps(report))

    def test_tasks_override_writes_expected_documents(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        before = plan.read_text()

        exit_code = main(
            [
                "split",
                str(plan),
                "--tasks",
                "u1-scaffold,u2-users-api",
                "--tasks",
                "u3-orders-api",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 0
        capsys.readouterr()

        part1 = tmp_path / "sample-part1-plan.md"
        part2 = tmp_path / "sample-part2-plan.md"
        assert part1.is_file() and part2.is_file()
        assert "split_from: sample-plan.md" in part1.read_text()
        assert "split_from: sample-plan.md" in part2.read_text()
        # No predecessor note for a --tasks override (no seam ordering implied).
        assert "assumes" not in part1.read_text()
        assert "assumes" not in part2.read_text()

        # The source plan is untouched.
        assert plan.read_text() == before

    def test_tasks_overrides_seam_when_both_given(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        self._write_advisory(
            tmp_path,
            "sample-plan",
            [
                {
                    "kind": "disconnected",
                    "message": "m",
                    "task_sets": [["u1-scaffold"], ["u2-users-api", "u3-orders-api"]],
                }
            ],
        )
        exit_code = main(
            [
                "split",
                str(plan),
                "--seam",
                "1",
                "--tasks",
                "u1-scaffold,u2-users-api",
                "--tasks",
                "u3-orders-api",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 0
        capsys.readouterr()
        part1 = (tmp_path / "sample-part1-plan.md").read_text()
        assert "u2-users-api" in part1
        assert "u3-orders-api" not in part1

    def test_missing_or_double_assigned_tasks_named_and_rejected(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        exit_code = main(
            [
                "split",
                str(plan),
                "--tasks",
                "u1-scaffold,u2-users-api",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "u3-orders-api" in err

    def test_serial_seam_writes_predecessor_note_only_on_downstream(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        self._write_advisory(
            tmp_path,
            "sample-plan",
            [
                {
                    "kind": "serial",
                    "message": "m",
                    "boundary": {
                        "tasks_before": ["u1-scaffold"],
                        "tasks_after": ["u2-users-api", "u3-orders-api"],
                    },
                }
            ],
        )
        exit_code = main(["split", str(plan), "--seam", "1", "--repo", str(tmp_path)])
        assert exit_code == 0
        capsys.readouterr()
        part1 = (tmp_path / "sample-part1-plan.md").read_text()
        part2 = (tmp_path / "sample-part2-plan.md").read_text()
        assert "assumes" not in part1
        assert "assumes" in part2
        assert "sample-part1-plan.md" in part2

    def test_seam_out_of_range_is_rejected(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        self._write_advisory(tmp_path, "sample-plan", [])
        exit_code = main(["split", str(plan), "--seam", "1", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "out of range" in capsys.readouterr().err

    def test_monolithic_seam_is_rejected_with_actionable_message(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        self._write_advisory(
            tmp_path,
            "sample-plan",
            [{"kind": "monolithic", "message": "m", "boundary": {"modularity": 0.1}}],
        )
        exit_code = main(["split", str(plan), "--seam", "1", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "--tasks" in capsys.readouterr().err

    def test_missing_advisory_report_is_actionable(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        exit_code = main(["split", str(plan), "--seam", "1", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "group --advise" in capsys.readouterr().err

    def test_neither_seam_nor_tasks_is_rejected(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        exit_code = main(["split", str(plan), "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "--seam" in capsys.readouterr().err

    def test_produced_documents_pass_plan_check(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        exit_code = main(
            [
                "split",
                str(plan),
                "--tasks",
                "u1-scaffold,u2-users-api",
                "--tasks",
                "u3-orders-api",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 0
        capsys.readouterr()

        for part in ("sample-part1-plan.md", "sample-part2-plan.md"):
            check_exit = main(["plan-check", str(tmp_path / part)])
            out = capsys.readouterr().out
            assert check_exit == 0, out

    def test_produced_documents_price_under_cap(self, tmp_path, capsys):
        plan = tmp_path / "sample-plan.md"
        plan.write_text(PLAN_TEXT)
        exit_code = main(
            [
                "split",
                str(plan),
                "--tasks",
                "u1-scaffold,u2-users-api",
                "--tasks",
                "u3-orders-api",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 0
        capsys.readouterr()

        for part in ("sample-part1-plan.md", "sample-part2-plan.md"):
            price_exit = main(["group", str(tmp_path / part), "--price", "--repo", str(tmp_path)])
            out = capsys.readouterr().out
            assert price_exit == 0, out
