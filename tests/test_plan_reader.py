"""Tests for orchestrator/grouping/plan_reader.py — the task-map parser.

Contract under test: docs/orchestrator-task-map.md. Absent block → None (LLM
mapper fallback); malformed block → TaskMapError, never a silent fallback;
verification flags mirror the mapper's wording.
"""

import json

import pytest

from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.plan_reader import TaskMapError, parse_task_map

VALID_MAP = """\
# orchestrator-task-map v1
tasks:
  - task_id: t1-scaffold
    description: create the app skeleton
    slice: null
    files:
      - app/main.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: t2-api
    description: items API routes
    slice: items
    files:
      - existing.py
      - app/items.py
    symbols: [real_fn, ghost_fn]
    depends_on: [t1-scaffold]
    implements: ["/api/items"]
    consumes: []
  - task_id: t3-ui
    description: items admin page
    slice: items
    files: [web/items.tsx]
    depends_on: [t1-scaffold]
    consumes: ["/api/items"]
"""


def plan_with(map_yaml: str) -> str:
    return f"# feat: toy\n\nSome prose.\n\n## Task Map\n\n```yaml\n{map_yaml}```\n"


def make_client(tmp_path, known_symbols=("real_fn",)):
    (tmp_path / "existing.py").write_text("def real_fn():\n    pass\n")

    def runner(args):
        if args[0] == "query":
            name = args[1]
            if name in known_symbols:
                return json.dumps([{"node": {"name": name, "filePath": "existing.py"}}])
            return "[]"
        return "{}"

    return CodegraphClient(repo_root=tmp_path, runner=runner)


class TestBlockDetection:
    def test_plan_without_map_returns_none(self, tmp_path):
        assert parse_task_map("# plan\n\njust prose\n", make_client(tmp_path)) is None

    def test_unmarked_yaml_fence_is_not_a_task_map(self, tmp_path):
        text = "# plan\n\n```yaml\ntasks:\n  - task_id: t\n```\n"
        assert parse_task_map(text, make_client(tmp_path)) is None

    def test_multiple_marked_blocks_rejected(self, tmp_path):
        text = plan_with(VALID_MAP) + plan_with(VALID_MAP)
        with pytest.raises(TaskMapError, match="exactly one"):
            parse_task_map(text, make_client(tmp_path))

    def test_unsupported_version_fails_instead_of_falling_back(self, tmp_path):
        text = "# plan\n\n```yaml\n# orchestrator-task-map v2\ntasks: []\n```\n"
        with pytest.raises(TaskMapError, match="unsupported task map version v2"):
            parse_task_map(text, make_client(tmp_path))


class TestValidParsing:
    def test_mappings_carry_all_plan_time_fields_in_document_order(self, tmp_path):
        out = parse_task_map(plan_with(VALID_MAP), make_client(tmp_path))
        assert [m.task_id for m in out.mappings] == ["t1-scaffold", "t2-api", "t3-ui"]
        api = out.mappings[1]
        assert api.files == ("existing.py",)
        assert api.prospective_files == ("app/items.py",)
        assert api.symbols == ("real_fn",)
        assert api.depends_on == ("t1-scaffold",)
        assert api.slice == "items"
        assert api.implements == ("/api/items",)
        assert out.mappings[2].consumes == ("/api/items",)
        assert out.mappings[0].slice is None
        assert out.descriptions["t2-api"] == "items API routes"

    def test_nonexistent_file_retained_as_prospective_with_info_flag(self, tmp_path):
        out = parse_task_map(plan_with(VALID_MAP), make_client(tmp_path))
        assert "app/main.py" in out.mappings[0].prospective_files
        assert any(
            "t1-scaffold file app/main.py does not exist yet — retained as prospective" in flag
            for flag in out.flags
        )

    def test_unknown_symbol_dropped_with_mapper_style_flag(self, tmp_path):
        out = parse_task_map(plan_with(VALID_MAP), make_client(tmp_path))
        assert "ghost_fn" not in out.mappings[1].symbols
        assert any(
            "task map: task t2-api mapped unknown symbol ghost_fn — dropped" in flag
            for flag in out.flags
        )

    def test_duplicate_files_counted_once(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [existing.py, existing.py]\n"
        )
        out = parse_task_map(plan_with(map_yaml), make_client(tmp_path))
        assert out.mappings[0].files == ("existing.py",)


class TestHardErrors:
    def cases(self):
        prefix = "# orchestrator-task-map v1\n"
        return {
            "not valid YAML": prefix + "tasks: [unclosed\n",
            "top level must be a mapping": prefix + "- just\n- a list\n",
            "'tasks' must be a non-empty list": prefix + "tasks: []\n",
            "unknown top-level keys": prefix
            + "tasks:\n  - task_id: t\n    description: d\nextra: 1\n",
            "unknown keys": prefix
            + "tasks:\n  - task_id: t\n    description: d\n    depend_on: [x]\n",
            "non-empty string 'task_id'": prefix + "tasks:\n  - description: d\n",
            "non-empty string 'description'": prefix + "tasks:\n  - task_id: t\n",
            "'slice' must be a string or null": prefix
            + "tasks:\n  - task_id: t\n    description: d\n    slice: 3\n",
            "must be a list of non-empty strings": prefix
            + "tasks:\n  - task_id: t\n    description: d\n    files: one.py\n",
            "duplicate task_id": prefix
            + "tasks:\n  - task_id: t\n    description: d\n  - task_id: t\n    description: d\n",
            "depends_on itself": prefix
            + "tasks:\n  - task_id: t\n    description: d\n    depends_on: [t]\n",
            "depends_on unknown task": prefix
            + "tasks:\n  - task_id: t\n    description: d\n    depends_on: [ghost]\n",
        }

    def test_every_malformed_shape_raises_naming_the_problem(self, tmp_path):
        client = make_client(tmp_path)
        for expected, map_yaml in self.cases().items():
            with pytest.raises(TaskMapError, match=expected):
                parse_task_map(plan_with(map_yaml), client)

    def test_depends_on_cycle_rejected_naming_members(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: a\n    description: d\n    depends_on: [b]\n"
            "  - task_id: b\n    description: d\n    depends_on: [a]\n"
        )
        with pytest.raises(TaskMapError, match=r"cycle among tasks \['a', 'b'\]"):
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))

    def test_slice_over_cap_rejected(self, tmp_path):
        entries = "".join(
            f"  - task_id: t{i}\n    description: d\n    slice: everything\n" for i in range(6)
        )
        map_yaml = f"# orchestrator-task-map v1\ntasks:\n{entries}"
        with pytest.raises(TaskMapError, match="slice 'everything' has 6 tasks"):
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))
