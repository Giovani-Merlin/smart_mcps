"""U2 tests: the launch-branch preflight baseline and its comparison helper."""

from __future__ import annotations

import subprocess
from pathlib import Path


from orchestrator.config import PreflightConfig
from orchestrator.execution.preflight import (
    PreflightBaseline,
    capture_preflight_baseline,
    compare_to_baseline,
    load_baseline,
    save_baseline,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


def _init_repo(worktree: Path) -> None:
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")


def _pytest_project(worktree: Path, test_body: str) -> None:
    (worktree / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\nversion = '0'\nrequires-python = '>=3.11'\n"
    )
    (worktree / "test_sample.py").write_text(test_body)
    (worktree / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["uv", "lock"], cwd=worktree, capture_output=True, text=True, check=True)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "add sample project")


# ------------------------------------------------------------- capture (U2)


def test_capture_records_command_sha_and_one_entry_per_test(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _pytest_project(
        repo,
        "def test_a():\n    assert True\n\ndef test_b():\n    assert False\n",
    )
    sha = git(repo, "rev-parse", "HEAD").strip()

    baseline = capture_preflight_baseline(
        repo, config=PreflightConfig(), output_dir=tmp_path / "out", commit_sha=sha
    )

    assert baseline.captured is True
    assert baseline.commit_sha == sha
    assert baseline.command
    assert set(baseline.tests) == {"test_sample::test_a", "test_sample::test_b"}
    assert baseline.failing_tests == frozenset({"test_sample::test_b"})


def test_baseline_round_trips_through_save_and_load(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _pytest_project(repo, "def test_a():\n    assert False\n")
    sha = git(repo, "rev-parse", "HEAD").strip()
    out_dir = tmp_path / "out"

    baseline = capture_preflight_baseline(
        repo, config=PreflightConfig(), output_dir=out_dir, commit_sha=sha
    )
    baseline_path = tmp_path / "run" / "preflight-baseline.json"
    save_baseline(baseline_path, baseline)

    assert baseline_path.is_file()
    reloaded = load_baseline(baseline_path)
    assert reloaded is not None
    assert reloaded.commit_sha == sha
    assert reloaded.command == baseline.command
    assert reloaded.failing_tests == baseline.failing_tests


def test_missing_baseline_file_loads_as_none(tmp_path):
    assert load_baseline(tmp_path / "nope" / "preflight-baseline.json") is None


def test_clean_check_command_records_empty_failing_set_not_absence(tmp_path):
    """[g1-baseline-clean-not-absent] A passing check command still produces a
    *captured* baseline — a clean run and an uncapturable one are distinct."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _pytest_project(repo, "def test_a():\n    assert True\n")
    sha = git(repo, "rev-parse", "HEAD").strip()

    baseline = capture_preflight_baseline(
        repo, config=PreflightConfig(), output_dir=tmp_path / "out", commit_sha=sha
    )

    assert baseline.captured is True
    assert baseline.exit_code == 0
    assert baseline.failing_tests == frozenset()
    assert "test_sample::test_a" in baseline.tests


def test_uncapturable_check_command_records_absent(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = git(repo, "rev-parse", "HEAD").strip()
    config = PreflightConfig(check_command=["python3", "-c", "import time; time.sleep(5)"])
    config = config.model_copy(update={"check_timeout_s": 0.2})

    baseline = capture_preflight_baseline(
        repo, config=config, output_dir=tmp_path / "out", commit_sha=sha
    )

    assert baseline.captured is False
    assert baseline.tests == {}


def test_no_check_command_detected_is_captured_and_empty(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sha = git(repo, "rev-parse", "HEAD").strip()

    baseline = capture_preflight_baseline(
        repo, config=PreflightConfig(), output_dir=tmp_path / "out", commit_sha=sha
    )

    assert baseline.captured is True
    assert baseline.tests == {}
    assert baseline.failing_tests == frozenset()


# ------------------------------------------------------------ comparison (U2)


def test_new_failures_are_exactly_the_ones_not_in_the_baseline():
    baseline = PreflightBaseline(
        command=["uv", "run", "pytest"],
        commit_sha="abc",
        exit_code=1,
        tests={"a": "failed", "b": "failed", "c": "passed"},
    )
    comparison = compare_to_baseline(baseline, {"a", "b", "c_new"})
    assert comparison.verdict == "new_failures"
    assert comparison.new_failures == frozenset({"c_new"})


def test_identical_failing_sets_are_pre_existing():
    baseline = PreflightBaseline(
        command=["uv", "run", "pytest"],
        commit_sha="abc",
        exit_code=1,
        tests={"a": "failed", "b": "failed"},
    )
    comparison = compare_to_baseline(baseline, {"a", "b"})
    assert comparison.verdict == "pre_existing"
    assert comparison.new_failures == frozenset()


def test_absent_baseline_never_reports_a_failure_as_new():
    comparison = compare_to_baseline(None, {"a", "b"})
    assert comparison.verdict == "no_baseline"
    assert comparison.new_failures == frozenset()


def test_uncaptured_baseline_object_also_reports_no_baseline():
    baseline = PreflightBaseline(
        command=["uv", "run", "pytest"], commit_sha="abc", exit_code=-1, captured=False
    )
    comparison = compare_to_baseline(baseline, {"a"})
    assert comparison.verdict == "no_baseline"
    assert comparison.new_failures == frozenset()
