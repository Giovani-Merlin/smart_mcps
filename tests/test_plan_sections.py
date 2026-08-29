"""Tests for the deterministic plan-section parser (plan U1)."""

from orchestrator.grouping.assembler import AssemblyInputs, assemble_group_specs
from orchestrator.grouping.graphing import TaskGraph
from orchestrator.grouping.plan_sections import (
    PlanSectionsError,
    parse_plan_sections,
    section_for_task,
    unit_key_for_task,
)
import pytest

PLAN_WITH_TASK_MAP = """# feat: toy plan

## Objective

Ship the toy feature.

## Units

### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser that splits things.
- **Summary**: Parses the plan into sections.
- **Files**: `orchestrator/grouping/plan_sections.py` *(new)*
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `plan-sections`
- **Verification**:
  - Parsing this plan document itself yields one section per task-map entry.
  - A plan whose unit heading is missing raises one error.

### U2. spec-assembly — assemble specs

- **Goal**: Build specs without an LLM.
- **Files**: `orchestrator/grouping/assembler.py` *(new)*
- **Depends-on**: u1-plan-sections
- **Slice**: —
- **Implements / Consumes**: consumes `plan-sections`; implements `assembled-specs`
- **Verification**: Every unit's Verification bullet appears exactly once.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-plan-sections
    description: parser
    files: [orchestrator/grouping/plan_sections.py]
  - task_id: u2-spec-assembly
    description: assembler
    depends_on: [u1-plan-sections]
    files: [orchestrator/grouping/assembler.py]
```
"""

PLAN_MISSING_U3 = """# feat: toy plan

## Units

### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser.
- **Verification**: it parses.

### U2. spec-assembly — assemble specs

- **Goal**: Build specs.
- **Verification**: it assembles.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-plan-sections
    description: parser
  - task_id: u2-spec-assembly
    description: assembler
  - task_id: u3-layered-context
    description: layered context
    depends_on: [u1-plan-sections, u2-spec-assembly]
```
"""

PLAN_MISSING_TWO = """# feat: toy plan

## Units

### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser.
- **Verification**: it parses.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-plan-sections
    description: parser
  - task_id: u3-layered-context
    description: layered context
    depends_on: [u1-plan-sections]
  - task_id: u5-planning-contract-docs
    description: docs
```
"""

PLAN_NO_SUMMARY = """# feat: toy plan

## Units

### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser.
- **Verification**: it parses.
"""

PLAN_NO_UNITS_HEADING = """# feat: legacy plan

## Tasks

- T1: extend the proxy server tool list
"""

_U1_BASE = """### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser that splits things.
- **Summary**: Parses the plan into sections.
- **Files**: `orchestrator/grouping/plan_sections.py` *(new)*
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `plan-sections`
- **Verification**:
  - Parsing this plan document itself yields one section per task-map entry.
  - A plan whose unit heading is missing raises one error.
"""

_U1_ENRICHED = """### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser that splits things.
- **Summary**: Parses the plan into sections.
- **Files**: `orchestrator/grouping/plan_sections.py` *(new)*
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `plan-sections`
- **Verification**:
  - Parsing this plan document itself yields one section per task-map entry.
  - A plan whose unit heading is missing raises one error.
- **Edge cases**: an empty plan yields zero unit sections, not an error.
- **Non-goals / must-not**: must not regenerate any unit's prose.
"""

PLAN_WITH_TASK_MAP_ENRICHED = PLAN_WITH_TASK_MAP.replace(_U1_BASE, _U1_ENRICHED)
assert PLAN_WITH_TASK_MAP_ENRICHED != PLAN_WITH_TASK_MAP

PLAN_RUN_PASS = """# feat: toy plan

## Units

### U1. plan-sections — deterministic plan parsing

- **Goal**: A parser that splits things.
- **Summary**: Parses the plan into sections.
- **Files**: `orchestrator/grouping/plan_sections.py` *(new)*
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `plan-sections`
- **Verification**:
  - Run: `uv run pytest tests/test_plan_sections.py`
    Pass: exits 0 with no failures.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-plan-sections
    description: parser
    files: [orchestrator/grouping/plan_sections.py]
```
"""


class TestParsePlanSections:
    def test_one_section_per_task_map_entry_verbatim(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert set(sections.units) == {"u1", "u2"}
        for unit in sections.units.values():
            assert unit.text in PLAN_WITH_TASK_MAP

    def test_digest_has_every_summary_line_and_no_unit_bodies(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert "Parses the plan into sections." in sections.digest
        assert "spec-assembly" in sections.digest  # u2's fallback summary (heading title)
        for unit in sections.units.values():
            assert unit.text not in sections.digest
            for item in unit.verification:
                assert item not in sections.digest

    def test_summary_bullet_parsed_verbatim(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert sections.units["u1"].summary == "Parses the plan into sections."
        assert sections.units["u1"].summary_is_fallback is False

    def test_verification_bullets_split_into_items(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        u1 = sections.units["u1"]
        assert len(u1.verification) == 2
        assert u1.verification[0].startswith("Parsing this plan document itself")

    def test_single_line_verification_is_one_item(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        u2 = sections.units["u2"]
        assert u2.verification == ("Every unit's Verification bullet appears exactly once.",)

    def test_implements_consumes_tags_parsed(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert sections.units["u1"].implements == ("plan-sections",)
        assert sections.units["u2"].consumes == ("plan-sections",)
        assert sections.units["u2"].implements == ("assembled-specs",)

    def test_missing_single_unit_heading_names_it(self):
        with pytest.raises(PlanSectionsError, match="u3-layered-context"):
            parse_plan_sections(PLAN_MISSING_U3)

    def test_two_missing_unit_headings_reported_together(self):
        with pytest.raises(PlanSectionsError) as excinfo:
            parse_plan_sections(PLAN_MISSING_TWO)
        message = str(excinfo.value)
        assert "u3-layered-context" in message
        assert "u5-planning-contract-docs" in message

    def test_missing_summary_falls_back_to_heading_title_with_flag(self):
        sections = parse_plan_sections(PLAN_NO_SUMMARY)
        unit = sections.units["u1"]
        assert unit.summary_is_fallback is True
        assert "plan-sections" in unit.summary
        assert any("u1" in flag and "Summary" in flag for flag in sections.flags)

    def test_plan_without_units_heading_yields_no_sections(self):
        sections = parse_plan_sections(PLAN_NO_UNITS_HEADING)
        assert sections.units == {}
        assert "extend the proxy server tool list" in sections.preamble

    def test_byte_stable_across_two_parses(self):
        a = parse_plan_sections(PLAN_WITH_TASK_MAP)
        b = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert a.digest == b.digest
        assert {k: v.text for k, v in a.units.items()} == {k: v.text for k, v in b.units.items()}

    def test_task_map_absent_from_digest_and_sections(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert "orchestrator-task-map" not in sections.digest
        assert "orchestrator-task-map" not in sections.preamble
        for unit in sections.units.values():
            assert "orchestrator-task-map" not in unit.text


class TestUnitKeyForTask:
    def test_matches_u_prefixed_task_ids(self):
        assert unit_key_for_task("u3-layered-context") == "u3"
        assert unit_key_for_task("u12-foo") == "u12"

    def test_non_matching_task_ids_return_none(self):
        assert unit_key_for_task("t1-scaffold") is None


class TestSectionForTask:
    def test_looks_up_by_numeric_prefix(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        unit = section_for_task(sections.units, "u1-plan-sections")
        assert unit is not None
        assert unit.unit_id == "u1"

    def test_non_unit_task_id_returns_none(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP)
        assert section_for_task(sections.units, "t1-scaffold") is None


class TestDeepenEnrichmentBullets:
    """Plan g3-1/g3-2: `Edge cases` / `Non-goals` / `Run:`+`Pass:` bullets are
    unknown labels to `_split_bullets` — they must parse harmlessly, leave
    every known field untouched, and never leak into the shared digest."""

    def test_known_fields_unchanged_by_new_bullets(self):
        base = parse_plan_sections(PLAN_WITH_TASK_MAP).units["u1"]
        enriched = parse_plan_sections(PLAN_WITH_TASK_MAP_ENRICHED).units["u1"]
        assert enriched.summary == base.summary
        assert enriched.summary_is_fallback == base.summary_is_fallback
        assert enriched.verification == base.verification
        assert enriched.implements == base.implements
        assert enriched.consumes == base.consumes
        assert enriched.text != base.text

    def test_digest_carries_only_tagged_summary_no_edge_case_text(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP_ENRICHED)
        assert "an empty plan yields zero unit sections" not in sections.digest
        assert "must not regenerate any unit's prose" not in sections.digest

    def test_new_bullets_appear_verbatim_in_assembled_group_spec(self):
        sections = parse_plan_sections(PLAN_WITH_TASK_MAP_ENRICHED)
        graph = TaskGraph(
            nodes=frozenset({"u1-plan-sections", "u2-spec-assembly"}),
            dependencies={("u1-plan-sections", "u2-spec-assembly"): 1.0},
            metadata={},
        )
        inputs = AssemblyInputs(
            plan_sections=sections,
            graph=graph,
            partition={"u1-plan-sections": 0, "u2-spec-assembly": 0},
            dag={},
            members_by_gid={0: ["u1-plan-sections", "u2-spec-assembly"]},
            descriptions={},
            group_label=lambda gid: f"g{gid + 1}",
        )
        specs = assemble_group_specs(inputs)
        assert "an empty plan yields zero unit sections, not an error." in specs["g1"].spec
        assert "must not regenerate any unit's prose." in specs["g1"].spec


class TestRunPassVerificationBullet:
    """Plan g3-3: a `Run:`/`Pass:` verification bullet becomes exactly one
    `VerificationItem` whose description carries both lines, and the coverage
    lint still maps it to exactly one group."""

    def test_run_pass_bullet_is_one_verification_item_with_both_lines(self):
        sections = parse_plan_sections(PLAN_RUN_PASS)
        u1 = sections.units["u1"]
        assert len(u1.verification) == 1
        assert "Run:" in u1.verification[0]
        assert "Pass:" in u1.verification[0]
        assert "uv run pytest tests/test_plan_sections.py" in u1.verification[0]
        assert "exits 0 with no failures." in u1.verification[0]

    def test_coverage_lint_maps_it_to_exactly_one_group(self):
        sections = parse_plan_sections(PLAN_RUN_PASS)
        graph = TaskGraph(
            nodes=frozenset({"u1-plan-sections"}),
            dependencies={},
            metadata={},
        )
        inputs = AssemblyInputs(
            plan_sections=sections,
            graph=graph,
            partition={"u1-plan-sections": 0},
            dag={},
            members_by_gid={0: ["u1-plan-sections"]},
            descriptions={},
            group_label=lambda gid: f"g{gid + 1}",
        )
        specs = assemble_group_specs(inputs)
        all_items = [item for spec in specs.values() for item in spec.verification]
        assert len(all_items) == 1
        assert "Run:" in all_items[0].description
        assert "Pass:" in all_items[0].description
