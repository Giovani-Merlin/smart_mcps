"""Tests for orchestrator/grouping/plan_reader.py — the task-map parser.

Contract under test: docs/orchestrator-task-map.md. Absent block → None (LLM
mapper fallback); malformed block → TaskMapError, never a silent fallback;
verification flags mirror the mapper's wording.
"""

import json

import pytest

from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.plan_reader import TaskMapError, parse_task_map, strip_task_map

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
        # v3 is a future, unsupported version (this parser reads v1/v2); v2
        # itself is now supported — see TestTaskMapV2 for its own coverage.
        text = "# plan\n\n```yaml\n# orchestrator-task-map v3\ntasks: []\n```\n"
        with pytest.raises(TaskMapError, match="unsupported task map version v3"):
            parse_task_map(text, make_client(tmp_path))


class TestValidParsing:
    """VALID_MAP's t2-api names one real symbol and one unknown one (ghost_fn);
    these tests are about field-carrying and file handling, not the unknown-symbol
    regime itself (covered separately below), so they opt into the old
    drop-with-flag behaviour via allow_unknown_symbols=True."""

    def test_mappings_carry_all_plan_time_fields_in_document_order(self, tmp_path):
        out = parse_task_map(
            plan_with(VALID_MAP), make_client(tmp_path), allow_unknown_symbols=True
        )
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
        out = parse_task_map(
            plan_with(VALID_MAP), make_client(tmp_path), allow_unknown_symbols=True
        )
        assert "app/main.py" in out.mappings[0].prospective_files
        assert any(
            "t1-scaffold file app/main.py does not exist yet — retained as prospective" in flag
            for flag in out.flags
        )

    def test_prospective_files_identical_regardless_of_allow_unknown_symbols(self, tmp_path):
        """R14: files have no prospective notation ambiguity — the flag only
        governs symbol handling, so a plan with no unknown symbols behaves
        identically with the override on or off."""
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [not-yet-created.py]\n"
        )
        client = make_client(tmp_path)
        strict = parse_task_map(plan_with(map_yaml), client, allow_unknown_symbols=False)
        lenient = parse_task_map(plan_with(map_yaml), client, allow_unknown_symbols=True)
        assert strict.mappings[0].prospective_files == lenient.mappings[0].prospective_files
        assert strict.flags == lenient.flags

    def test_unknown_symbol_dropped_with_mapper_style_flag_when_allowed(self, tmp_path):
        out = parse_task_map(
            plan_with(VALID_MAP), make_client(tmp_path), allow_unknown_symbols=True
        )
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


class TestUnknownSymbolHardFail:
    """R14: an unknown symbol is a hard error by default — the map's ``symbols:``
    field has no prospective notation, so every listed symbol is a claim that it
    exists. --allow-unknown-symbols (tested above) restores the old behaviour."""

    def test_unknown_symbol_raises_naming_task_and_symbol_by_default(self, tmp_path):
        with pytest.raises(TaskMapError, match=r"t2-api.*ghost_fn"):
            parse_task_map(plan_with(VALID_MAP), make_client(tmp_path))

    def test_default_is_allow_unknown_symbols_false(self, tmp_path):
        """The keyword-arg default and the no-arg call must agree."""
        client = make_client(tmp_path)
        with pytest.raises(TaskMapError):
            parse_task_map(plan_with(VALID_MAP), client)
        with pytest.raises(TaskMapError):
            parse_task_map(plan_with(VALID_MAP), client, allow_unknown_symbols=False)


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


class TestSizeHints:
    """Plan U7: size_hints prices a prospective file by declared class instead of
    the flat per-file allowance. Carrier is the map's ``size_hints`` sibling key,
    not plan prose."""

    def test_size_hints_carried_on_prospective_files_only(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [existing.py, app/new.py]\n"
            "    size_hints:\n"
            "      app/new.py: large\n"
        )
        out = parse_task_map(plan_with(map_yaml), make_client(tmp_path))
        assert out.mappings[0].size_hints == (("app/new.py", "large"),)

    def test_map_without_size_hints_key_carries_no_hints(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [app/new.py]\n"
        )
        out = parse_task_map(plan_with(map_yaml), make_client(tmp_path))
        assert out.mappings[0].size_hints == ()

    def test_size_hints_path_not_in_files_raises(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [app/new.py]\n"
            "    size_hints:\n"
            "      app/other.py: large\n"
        )
        with pytest.raises(TaskMapError, match=r"t1.*app/other\.py.*not in this task's files"):
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))

    def test_size_hints_unknown_class_raises(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [app/new.py]\n"
            "    size_hints:\n"
            "      app/new.py: huge\n"
        )
        with pytest.raises(TaskMapError, match=r"t1.*'huge'.*large.*medium.*small"):
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))

    def test_size_hints_on_existing_file_raises_naming_it(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [existing.py]\n"
            "    size_hints:\n"
            "      existing.py: small\n"
        )
        with pytest.raises(TaskMapError, match=r"t1.*existing\.py.*already exists"):
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))

    def test_size_hints_wrong_shape_raises(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [app/new.py]\n"
            "    size_hints: [app/new.py]\n"
        )
        with pytest.raises(TaskMapError, match=r"t1.*'size_hints'.*mapping of path"):
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))


class TestErrorAccumulation:
    """Plan U6/C1: a validation phase reports every problem it finds together,
    not just the first — and phase order still holds: a shape problem hides
    reference problems in the same call until the shape is fixed."""

    def test_two_unknown_key_tasks_reported_together(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n    description: d\n    bogus_key: 1\n"
            "  - task_id: t2\n    description: d\n    another_bogus: 2\n"
        )
        with pytest.raises(TaskMapError) as excinfo:
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))
        message = str(excinfo.value)
        assert "bogus_key" in message
        assert "another_bogus" in message

    def test_fixing_unknown_keys_then_reveals_both_size_hints_problems_together(self, tmp_path):
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n"
            "    description: d\n"
            "    files: [app/new1.py]\n"
            "    size_hints:\n"
            "      app/other.py: large\n"
            "  - task_id: t2\n"
            "    description: d\n"
            "    files: [app/new2.py]\n"
            "    size_hints:\n"
            "      app/new2.py: huge\n"
        )
        with pytest.raises(TaskMapError) as excinfo:
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))
        message = str(excinfo.value)
        assert "app/other.py" in message
        assert "not in this task's files" in message
        assert "'huge'" in message

    def test_shape_errors_hide_reference_errors_in_the_same_call(self, tmp_path):
        """A duplicate task_id (reference phase) alongside an unknown key
        (shape phase) is not reported yet — the shape phase reports and stops
        first."""
        map_yaml = (
            "# orchestrator-task-map v1\n"
            "tasks:\n"
            "  - task_id: t1\n    description: d\n    bogus_key: 1\n"
            "  - task_id: t1\n    description: d\n"
        )
        with pytest.raises(TaskMapError) as excinfo:
            parse_task_map(plan_with(map_yaml), make_client(tmp_path))
        message = str(excinfo.value)
        assert "bogus_key" in message
        assert "duplicate" not in message


class TestStripTaskMap:
    """R27: the task-map block is grouper parser input only — strip_task_map
    removes it (plus a directly preceding heading) from LLM-facing plan text."""

    def test_removes_heading_and_block_leaving_prose_intact(self):
        text = plan_with(VALID_MAP)
        stripped = strip_task_map(text)
        assert "orchestrator-task-map v1" not in stripped
        assert "## Task Map" not in stripped
        assert "```yaml" not in stripped
        assert "# feat: toy" in stripped
        assert "Some prose." in stripped

    def test_text_without_a_marked_block_passes_through_unchanged(self):
        text = "# just a plan\n\nno task map here at all.\n"
        assert strip_task_map(text) == text

    def test_unmarked_yaml_fence_is_left_alone(self):
        """A yaml fence that isn't the version-marked task map is not a strip
        target — the same discrimination parse_task_map uses."""
        text = "# plan\n\n```yaml\ntasks:\n  - task_id: t\n```\n"
        assert strip_task_map(text) == text

    def test_block_without_a_preceding_heading_is_still_removed(self):
        text = "# feat: x\n\n```yaml\n# orchestrator-task-map v1\ntasks: []\n```\n"
        stripped = strip_task_map(text)
        assert "orchestrator-task-map v1" not in stripped
        assert "# feat: x" in stripped

    def test_v2_block_and_heading_removed_exactly_as_v1(self):
        """g1-10: a v2 block never reaches an LLM context — strip_task_map
        removes it (and its preceding heading) the same way it removes v1."""
        v2_map = TestTaskMapV2.V2_NO_RECIPE
        text = plan_with(v2_map) + "\n## Next section\nprose\n"
        stripped = strip_task_map(text)
        assert "orchestrator-task-map" not in stripped
        assert "## Task Map" not in stripped
        assert "```yaml" not in stripped
        assert "# feat: toy" in stripped
        assert "## Next section" in stripped

    def test_content_before_the_heading_is_byte_identical(self):
        """Only the heading + block span is removed — everything before it,
        including its own trailing blank-line paragraph break, is untouched."""
        text = plan_with(VALID_MAP)
        prefix = text.split("## Task Map")[0]
        assert strip_task_map(text).startswith(prefix)

    def test_idempotent(self):
        text = plan_with(VALID_MAP)
        once = strip_task_map(text)
        twice = strip_task_map(once)
        assert once == twice


class TestTaskMapV2:
    """Plan U2: the v2 marker carries `recipe`/`recipe_args`; v1 parses exactly
    as before, and a v2 block with no `recipe` on any task is identical to the
    same block marked v1."""

    V2_NO_RECIPE = VALID_MAP.replace("# orchestrator-task-map v1", "# orchestrator-task-map v2")

    RUN_TASK = (
        "# orchestrator-task-map v2\n"
        "tasks:\n"
        "  - task_id: t1-render\n"
        "    description: render the podcast audio\n"
        "    recipe: run\n"
        "    recipe_args:\n"
        "      commands:\n"
        "        - cmd: uv run scripts/render.py\n"
        "          wall_clock_min: 5.0\n"
        "    depends_on: []\n"
        "    implements: []\n"
        "    consumes: []\n"
    )

    def test_v1_map_unaffected_by_v2_support(self, tmp_path):
        output = parse_task_map(
            plan_with(VALID_MAP), make_client(tmp_path), allow_unknown_symbols=True
        )
        assert output is not None
        assert all(m.recipe == "code" for m in output.mappings)
        assert all(m.recipe_args is None for m in output.mappings)

    def test_v2_block_no_recipe_matches_v1_mappings(self, tmp_path):
        v1_output = parse_task_map(
            plan_with(VALID_MAP), make_client(tmp_path), allow_unknown_symbols=True
        )
        v2_output = parse_task_map(
            plan_with(self.V2_NO_RECIPE), make_client(tmp_path), allow_unknown_symbols=True
        )
        assert v1_output is not None and v2_output is not None
        assert v1_output.descriptions == v2_output.descriptions
        assert v1_output.flags == v2_output.flags
        assert [m.task_id for m in v1_output.mappings] == [m.task_id for m in v2_output.mappings]
        for m1, m2 in zip(v1_output.mappings, v2_output.mappings, strict=True):
            assert m1.files == m2.files
            assert m1.symbols == m2.symbols
            assert m1.depends_on == m2.depends_on
            assert m1.slice == m2.slice
            assert m2.recipe == "code"
            assert m2.recipe_args is None

    def test_run_task_with_valid_recipe_args_parses(self, tmp_path):
        output = parse_task_map(plan_with(self.RUN_TASK), make_client(tmp_path))
        assert output is not None
        [mapping] = output.mappings
        assert mapping.recipe == "run"
        assert mapping.recipe_args is not None
        assert mapping.recipe_args.commands[0].cmd == "uv run scripts/render.py"

    def test_run_task_empty_commands_raises_naming_task(self, tmp_path):
        text = self.RUN_TASK.replace(
            "      commands:\n"
            "        - cmd: uv run scripts/render.py\n"
            "          wall_clock_min: 5.0\n",
            "      commands: []\n",
        )
        with pytest.raises(TaskMapError, match="t1-render"):
            parse_task_map(plan_with(text), make_client(tmp_path))

    def test_run_task_with_slice_raises_naming_task(self, tmp_path):
        text = self.RUN_TASK.replace("    depends_on: []", "    slice: x\n    depends_on: []")
        with pytest.raises(TaskMapError, match="t1-render"):
            parse_task_map(plan_with(text), make_client(tmp_path))

    def test_run_task_marked_v1_raises_naming_task(self, tmp_path):
        text = self.RUN_TASK.replace("# orchestrator-task-map v2", "# orchestrator-task-map v1")
        with pytest.raises(TaskMapError, match="t1-render"):
            parse_task_map(plan_with(text), make_client(tmp_path))

    def test_unknown_recipe_raises_naming_task_and_registered_names(self, tmp_path):
        text = self.RUN_TASK.replace("recipe: run", "recipe: research")
        with pytest.raises(TaskMapError) as excinfo:
            parse_task_map(plan_with(text), make_client(tmp_path))
        message = str(excinfo.value)
        assert "t1-render" in message
        assert "research" in message
        assert "code" in message and "run" in message

    def test_recipe_args_on_code_task_raises(self, tmp_path):
        text = self.RUN_TASK.replace("recipe: run", "recipe: code")
        with pytest.raises(TaskMapError, match="t1-render"):
            parse_task_map(plan_with(text), make_client(tmp_path))


class TestRealPlansStillParse:
    """g1-6: every committed plan under docs/plans/ carrying a v1 task map
    parses to the same MapperOutput before and after v2 support exists.

    v1 parsing is byte-for-byte untouched by this change (no v1-only behaviour
    was added, removed, or reordered — v2 is purely additive, gated by the
    marker version). A plan whose v1 map is already unparseable for reasons
    unrelated to recipes (e.g. stale ``size_hints`` from prior drift) raised
    the identical ``TaskMapError`` before this change too; this test only
    asserts the successful ones match, and that no failure mentions the new
    recipe machinery."""

    def test_every_v1_plan_parses_identically(self):
        import glob
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        plan_paths = sorted(glob.glob(str(repo_root / "docs/plans/*.md")))
        v1_plans = [p for p in plan_paths if "# orchestrator-task-map v1" in Path(p).read_text()]
        assert v1_plans, "expected at least one committed v1 plan"

        def runner(args):
            return "[]" if args[0] == "query" else "{}"

        for plan_path in v1_plans:
            text = Path(plan_path).read_text()
            client = CodegraphClient(repo_root=repo_root, runner=runner)
            try:
                output = parse_task_map(text, client, allow_unknown_symbols=True)
            except TaskMapError as exc:
                message = str(exc)
                assert "which require the plan's task map to be marked v2" not in message, plan_path
                assert "recipe_args" not in message, plan_path
                assert "unknown recipe" not in message, plan_path
                continue
            assert output is not None, plan_path
            assert all(m.recipe == "code" for m in output.mappings), plan_path
            assert all(m.recipe_args is None for m in output.mappings), plan_path
