"""Tests for deterministic spec assembly (plan U2) — the write_specs replacement."""

import re

import pytest

from orchestrator.grouping.assembler import (
    ASSEMBLED_FLAG,
    AssemblyError,
    AssemblyInputs,
    assemble_group_specs,
)
from orchestrator.grouping.graphing import TaskGraph
from orchestrator.grouping.plan_sections import parse_plan_sections

PLAN = """# feat: toy plan

## Units

### U1. scaffold — create the app skeleton

- **Goal**: Create the app skeleton.
- **Summary**: Creates the app skeleton.
- **Files**: `app/main.py` *(new)*
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `scaffold-ready`
- **Verification**: `app/main.py` exists.

### U2. items-api — items API routes

- **Goal**: Items API routes.
- **Summary**: Items API routes on the scaffold.
- **Files**: `app/items.py` *(new)*
- **Depends-on**: u1-scaffold
- **Slice**: items
- **Implements / Consumes**: consumes `scaffold-ready`; implements `/api/items`
- **Verification**:
  - `GET /api/items` returns 200.
  - `POST /api/items` creates an item.

### U3. items-ui — items admin page

- **Goal**: Items admin page.
- **Summary**: Items admin page calling the items API.
- **Files**: `web/items.tsx` *(new)*
- **Depends-on**: u1-scaffold
- **Slice**: items
- **Implements / Consumes**: consumes `/api/items`
- **Verification**: the items page renders a table.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-scaffold
    description: create the app skeleton
  - task_id: u2-items-api
    description: items API routes
    depends_on: [u1-scaffold]
  - task_id: u3-items-ui
    description: items admin page
    depends_on: [u1-scaffold]
```
"""

PLAN_NO_VERIFICATION_U3 = PLAN.replace(
    "- **Verification**: the items page renders a table.", "- **Verification**: —"
)


def make_graph(dependencies, metadata=None):
    nodes = frozenset({"u1-scaffold", "u2-items-api", "u3-items-ui"})
    return TaskGraph(
        nodes=nodes,
        dependencies=dependencies,
        metadata=metadata or {},
    )


def make_inputs(plan_text=PLAN):
    plan_sections = parse_plan_sections(plan_text)
    graph = make_graph(
        dependencies={("u1-scaffold", "u2-items-api"): 1.0, ("u1-scaffold", "u3-items-ui"): 1.0},
        metadata={
            "u1-scaffold": {"slice": None},
            "u2-items-api": {"slice": "items"},
            "u3-items-ui": {"slice": "items"},
        },
    )
    partition = {"u1-scaffold": 0, "u2-items-api": 1, "u3-items-ui": 1}
    members_by_gid = {0: ["u1-scaffold"], 1: ["u2-items-api", "u3-items-ui"]}
    dag = {0: {1}}
    descriptions = {
        "u1-scaffold": "create the app skeleton",
        "u2-items-api": "items API routes",
        "u3-items-ui": "items admin page",
    }
    return AssemblyInputs(
        plan_sections=plan_sections,
        graph=graph,
        partition=partition,
        dag=dag,
        members_by_gid=members_by_gid,
        descriptions=descriptions,
        group_label=lambda gid: f"g{gid + 1}",
    )


class TestAssembleGroupSpecs:
    def test_every_group_gets_non_empty_contract_fields(self):
        specs = assemble_group_specs(make_inputs())
        assert set(specs) == {"g1", "g2"}
        for spec in specs.values():
            assert spec.name
            assert spec.summary
            assert spec.spec
            assert spec.verification

    def test_spec_contains_member_unit_sections_verbatim(self):
        specs = assemble_group_specs(make_inputs())
        sections = parse_plan_sections(PLAN)
        assert sections.units["u1"].text in specs["g1"].spec
        assert sections.units["u2"].text in specs["g2"].spec
        assert sections.units["u3"].text in specs["g2"].spec
        # g1 must not carry g2's member sections and vice versa
        assert sections.units["u2"].text not in specs["g1"].spec

    def test_relational_header_names_upstream_and_downstream_groups(self):
        specs = assemble_group_specs(make_inputs())
        assert "g2" in specs["g1"].spec.split("Downstream groups:")[1].split("Slice:")[0]
        assert "g1" in specs["g2"].spec.split("Upstream groups:")[1].split("Downstream groups:")[0]

    def test_regenerating_after_changed_partition_changes_the_header(self):
        """R2: the header is graph/DAG facts, so a different partition must
        produce a different header (here: no dependency edge crosses groups)."""
        inputs = make_inputs()
        no_dag_inputs = AssemblyInputs(
            plan_sections=inputs.plan_sections,
            graph=inputs.graph,
            partition=inputs.partition,
            dag={},
            members_by_gid=inputs.members_by_gid,
            descriptions=inputs.descriptions,
            group_label=inputs.group_label,
        )
        specs_with_dag = assemble_group_specs(inputs)
        specs_without_dag = assemble_group_specs(no_dag_inputs)
        assert specs_with_dag["g1"].spec != specs_without_dag["g1"].spec

    def test_verification_ids_match_pattern_and_cover_every_bullet(self):
        specs = assemble_group_specs(make_inputs())
        all_items = [item for spec in specs.values() for item in spec.verification]
        assert len(all_items) == 4  # 1 (u1) + 2 (u2) + 1 (u3)
        for item in all_items:
            assert re.match(r"^g\d+-\d+$", item.id)
            assert item.required is True
        descriptions = {item.description for item in all_items}
        assert "`app/main.py` exists." in descriptions
        assert "`GET /api/items` returns 200." in descriptions
        assert "`POST /api/items` creates an item." in descriptions
        assert "the items page renders a table." in descriptions

    def test_missing_unit_verification_fails_naming_the_unit(self):
        with pytest.raises(AssemblyError, match="u3"):
            assemble_group_specs(make_inputs(PLAN_NO_VERIFICATION_U3))

    def test_byte_deterministic_across_two_calls(self):
        a = assemble_group_specs(make_inputs())
        b = assemble_group_specs(make_inputs())
        assert {gid: spec.model_dump() for gid, spec in a.items()} == {
            gid: spec.model_dump() for gid, spec in b.items()
        }


class TestAssembledFlag:
    def test_flag_text_is_stable(self):
        assert ASSEMBLED_FLAG == "specs: assembled from plan — speccer LLM skipped"
