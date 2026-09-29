"""plan-check's repo-aware lint of ``Run:`` verification items.

Regression for run r20260927-100604: coder item g2-7 (``bash -c`` writing
``/tmp``) halted the run on a sandbox denial, and g3-2 named a bundle path no
code writes. Both are visible in the plan text before launch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.grouping.verification_lint import lint_verification


def _plan(verification: str, files: tuple[str, ...] = ("src/app.py",)) -> str:
    files_yaml = "".join(f"      - {f}\n" for f in files)
    return (
        "# Plan\n\n## Units\n\n"
        "### U1. thing — a unit\n\n"
        "- **Verification**:\n"
        f"  - {verification}\n\n"
        "## Task Map\n\n```yaml\n# orchestrator-task-map v2\ntasks:\n"
        "  - task_id: u1-thing\n"
        "    description: a unit\n"
        "    slice: null\n"
        "    files:\n"
        f"{files_yaml}"
        "    symbols: []\n"
        "    depends_on: []\n"
        "    implements: []\n"
        "    consumes: []\n"
        "```\n"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("")
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    return tmp_path


def test_g2_7_shape_is_a_tmp_problem_and_an_allowlist_warning(repo):
    plan = _plan("""Run: `bash -c 'echo 5 > /tmp/v && cat /tmp/v'` Pass: prints 5.""")
    problems, warnings = lint_verification(plan, repo)
    assert len(problems) == 1 and "/tmp/v" in problems[0] and "never /tmp" in problems[0]
    assert any("`bash`" in w for w in warnings)


def test_driver_items_skip_the_allowlist_but_never_the_tmp_rule(repo):
    assert lint_verification(_plan("Run (driver): `bash -c true` Pass: ok."), repo) == ([], [])
    problems, warnings = lint_verification(
        _plan("Run (driver): `uv run tool --out /tmp/rr` Pass: ok."), repo
    )
    assert warnings == [] and problems and "/tmp/rr" in problems[0]


def test_scratch_dir_and_tmp_lookalikes_are_fine(repo):
    plan = _plan(
        "Run: `uv run pytest tests/test_app.py -q --basetemp=.coder-scratch/pt` Pass: green."
    )
    assert lint_verification(plan, repo) == ([], [])


def test_open_of_a_missing_path_is_a_problem(repo):
    plan = _plan(
        """Run: `python -c "import json; json.load(open('runs/r1/ingest.json'))"` Pass: loads."""
    )
    problems, _ = lint_verification(plan, repo)
    assert problems and "runs/r1/ingest.json" in problems[0]


def test_existing_declared_and_created_paths_are_fine(repo):
    for verification in (
        "Run: `uv run pytest tests/test_app.py -q` Pass: green.",
        "Run: `uv run pytest tests/test_new.py -q` Pass: green.",
        "Run: `uv run tool --out out/r1 && cat out/r1/body.md` Pass: prints.",
        """Run: `python -c "f(paths=['scripts/eval.py'])"` Pass: ok.""",
    ):
        plan = _plan(verification, files=("src/app.py", "tests/test_new.py"))
        problems, _ = lint_verification(plan, repo)
        assert problems == [], (verification, problems)


def test_operators_inside_quotes_do_not_split_and_path_prefixes_resolve(repo):
    plan = _plan(
        """Run: `.venv/bin/python -c "import a; print(1 | 2)" && grep -n "x\\|y" src/app.py` Pass: ok."""
    )
    assert lint_verification(plan, repo) == ([], [])


def test_plan_check_fails_on_a_missing_path_and_prints_warnings(repo, capsys):
    plan_path = repo / "plan.md"
    plan_path.write_text(
        _plan("""Run: `cat runs/r1/ingest.json && bash -c true` Pass: prints the bundle.""")
    )
    assert main(["plan-check", "--repo", str(repo), str(plan_path)]) == 1
    out = capsys.readouterr().out
    assert "warning:" in out and "`bash`" in out
    assert "runs/r1/ingest.json" in out
