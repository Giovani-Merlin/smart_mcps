"""plan-check's Goal-symbol lint: a Goal that changes a symbol defined in a file
outside the unit's Files is flagged before grouping."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.grouping.verification_lint import lint_goal_symbols


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("class Runner:\n    pass\n\n\ndef helper():\n    pass\n")
    (tmp_path / "pkg" / "b.py").write_text("def other():\n    pass\n")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def _plan(goal: str, files: tuple[str, ...] = ("pkg/b.py",), with_map: bool = True) -> str:
    text = f"# Plan\n\n## Units\n\n### U1. thing — a unit\n\n- **Goal**: {goal}\n- **Files**: x\n\n"
    if not with_map:
        return text
    files_yaml = "".join(f"      - {f}\n" for f in files)
    return (
        text + "## Task Map\n\n```yaml\n# orchestrator-task-map v2\ntasks:\n"
        "  - task_id: u1-thing\n    description: a unit\n    slice: null\n    files:\n"
        f"{files_yaml}```\n"
    )


def test_change_verb_with_defining_file_outside_files_warns(repo: Path) -> None:
    warnings = lint_goal_symbols(_plan("`Runner.start` gains a flag."), repo)
    assert len(warnings) == 1
    assert "pkg/a.py" in warnings[0] and "gains" in warnings[0]


def test_defining_file_in_files_is_silent(repo: Path) -> None:
    assert lint_goal_symbols(_plan("`Runner.start` gains a flag.", ("pkg/a.py",)), repo) == []


def test_mention_without_change_verb_is_silent(repo: Path) -> None:
    assert lint_goal_symbol_text(repo, "calls `Runner.start` before `helper`.") == []


def test_prospective_symbol_is_silent(repo: Path) -> None:
    assert lint_goal_symbol_text(repo, "`nowhere_fn` gains a flag.") == []


def test_short_name_is_silent(repo: Path) -> None:
    assert lint_goal_symbol_text(repo, "`run` gains a flag.") == []


def test_plan_without_task_map_does_not_crash(repo: Path) -> None:
    assert lint_goal_symbols(_plan("`Runner` gains a flag.", with_map=False), repo) == []


def lint_goal_symbol_text(repo: Path, goal: str) -> list[str]:
    return lint_goal_symbols(_plan(goal), repo)
