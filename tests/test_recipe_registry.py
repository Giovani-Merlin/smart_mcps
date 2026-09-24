"""Tests for orchestrator/recipes/ — the Unit Recipe registry (plan U1)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from orchestrator.config import EstimatorConfig
from orchestrator.grouping import plan_reader
from orchestrator.grouping.estimator import node_work
from orchestrator.grouping.graphing import source_bytes_of
from orchestrator.recipes import get_recipe, registered_names
from orchestrator.recipes.code import price_code
from orchestrator.recipes.run import RunArgs

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_PLAN = REPO_ROOT / "docs/plans/2026-09-23-001-refactor-review-loop-split-plan.md"


class TestRegistry:
    def test_registered_names(self):
        assert registered_names() == ("code", "run")

    def test_unknown_recipe_names_it_and_lists_known(self):
        with pytest.raises(KeyError) as excinfo:
            get_recipe("research")
        message = str(excinfo.value)
        assert "research" in message
        assert "code" in message
        assert "run" in message

    def test_get_recipe_returns_entries(self):
        code = get_recipe("code")
        run = get_recipe("run")
        assert code.name == "code"
        assert run.name == "run"
        assert run.args_model is RunArgs


class TestRunArgsValidation:
    def _base(self, **overrides):
        payload = {"commands": [{"cmd": "echo hi", "wall_clock_min": 1.0}]}
        payload.update(overrides)
        return payload

    def test_zero_commands_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            RunArgs(commands=[])
        assert "commands" in str(excinfo.value)

    def test_nonpositive_wall_clock_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            RunArgs(commands=[{"cmd": "x", "wall_clock_min": 0}])
        assert "wall_clock_min" in str(excinfo.value)

    def test_unknown_key_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            RunArgs(**self._base(bogus="x"))
        assert "bogus" in str(excinfo.value) or "extra" in str(excinfo.value).lower()

    @pytest.mark.parametrize(
        "entry",
        ["~/.claude", "~/.claude/projects", "~/"],
    )
    def test_allow_write_protecting_claude_home_rejected(self, entry):
        with pytest.raises(ValidationError) as excinfo:
            RunArgs(**self._base(allow_write=[entry]))
        assert "allow_write" in str(excinfo.value)

    def test_allow_write_relative_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            RunArgs(**self._base(allow_write=["data/x"]))
        assert "allow_write" in str(excinfo.value)

    def test_allow_write_var_rejected(self):
        with pytest.raises(ValidationError) as excinfo:
            RunArgs(**self._base(allow_write=["$HOME/x"]))
        assert "allow_write" in str(excinfo.value)

    def test_allow_write_ok_path_accepted(self):
        RunArgs(**self._base(allow_write=["/tmp/x", "~/scratch"]))

    def test_outputs_absolute_rejected(self):
        with pytest.raises(ValidationError):
            RunArgs(**self._base(outputs=["/abs/x"]))

    def test_outputs_dotdot_rejected(self):
        with pytest.raises(ValidationError):
            RunArgs(**self._base(outputs=["a/../b"]))

    def test_outputs_relative_ok(self):
        RunArgs(**self._base(outputs=["data/out.json"]))


class TestCodePriceMatchesNodeWork:
    def test_matches_estimator_for_real_plan(self):
        """The real plan is the oracle. Its task map has since drifted (a file
        it names as prospective now exists — a pre-existing repo-state quirk,
        not this recipe's concern), so metadata is built the same way
        ``parse_task_map_for_pricing`` builds it, minus that one guard, which
        has nothing to do with pricing arithmetic."""
        plan_text = REAL_PLAN.read_text()
        match = plan_reader._BLOCK.search(plan_text)
        assert match, "expected plan to carry a v1 task map"
        payload = yaml.safe_load(match.group("body"))
        config = EstimatorConfig()
        assert payload["tasks"], "expected at least one task"
        for entry in payload["tasks"]:
            raw_size_hints = entry.get("size_hints") or {}
            files: list[str] = []
            prospective: list[str] = []
            for file in entry.get("files") or []:
                if (REPO_ROOT / file).is_file():
                    files.append(file)
                else:
                    prospective.append(file)
            size_hints = {f: raw_size_hints[f] for f in prospective if f in raw_size_hints}
            metadata = {
                "source_bytes": source_bytes_of(REPO_ROOT, files),
                "files": tuple(files),
                "prospective_files": tuple(prospective),
                "size_hints": size_hints,
            }
            expected = node_work(metadata, config)
            actual = price_code(None, metadata, config).tokens
            assert actual == expected, entry["task_id"]


class TestImportHazard:
    def test_recipes_package_does_not_import_grouping_or_execution(self):
        code = (
            "import sys\n"
            "import orchestrator.recipes\n"
            "bad = [m for m in sys.modules "
            "if m.startswith('orchestrator.grouping') or m.startswith('orchestrator.execution')]\n"
            "assert not bad, bad\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
