"""Tests for the deterministic plan-section parser (plan U1)."""

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
