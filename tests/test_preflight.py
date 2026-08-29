"""U4 tests: the LLM-free Preflight merge gate, standalone and wired into
IntegrationMerger.merge_group."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from orchestrator.config import PreflightConfig
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.preflight import (
    BaselineStep,
    CheckStep,
    PreflightBaseline,
    PreflightFailure,
    configured_check_step,
    detect_check_command,
    detect_check_steps,
    run_preflight,
)
from orchestrator.execution.review import MergeConflict
from orchestrator.execution.worktrees import create_worktree, group_branch
from orchestrator.model import Group, ReviewIntensity


def make_group(gid: str, files: list[str] | None = None) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=files or [],
    )


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "shared.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def coder_commit(worktree: Path, filename: str, content: str, message: str) -> None:
    (worktree / filename).write_text(content)
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", message)


def group_worktree(repo: Path, merger: IntegrationMerger, group: Group) -> Path:
    return create_worktree(
        repo,
        run_id=merger.run_id,
        group_id=group.id,
        name=group.name,
        branch=group_branch(merger.run_id, group.id),
        start_point=merger.tip(),
    )


# ---------------------------------------------------------- standalone unit


class RaisingRunner:
    """A session runner that fails the test the instant it is called at all —
    proof Preflight makes zero LLM calls (plan R6)."""

    def __getattr__(self, name):
        raise AssertionError(f"Preflight must never touch a session runner ({name})")


def test_dirty_worktree_fails_and_names_the_dirty_paths(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    (worktree / "litter.txt").write_text("uncommitted\n")
    with pytest.raises(PreflightFailure, match="litter.txt") as excinfo:
        run_preflight(worktree, config=PreflightConfig(), output_dir=tmp_path / "out", log=None)
    assert excinfo.value.kind == "env"


def test_clean_worktree_with_no_check_command_passes(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    logged = []
    run_preflight(
        worktree, config=PreflightConfig(), output_dir=tmp_path / "out", log=logged.append
    )
    assert any("no check" in line for line in logged)


def test_scratch_only_dirt_passes_when_archived_first(tmp_path):
    """R6a: cleanliness is evaluated by the caller's ordering — a worktree
    whose only dirt was archived away (simulated here by simply never having
    written it) passes."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    # Simulates archival having already run: nothing left to be dirty about.
    run_preflight(worktree, config=PreflightConfig(), output_dir=tmp_path / "out")


def test_check_command_failure_writes_output_and_fails(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    config = PreflightConfig(check_command=["python3", "-c", "import sys; sys.exit(1)"])
    out_dir = tmp_path / "out"
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=config, output_dir=out_dir)
    assert excinfo.value.output_path is not None
    assert excinfo.value.output_path.is_file()


def test_check_command_success_passes(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    config = PreflightConfig(check_command=["python3", "-c", "print('ok')"])
    run_preflight(worktree, config=config, output_dir=tmp_path / "out")


def test_zero_llm_calls_on_pass_and_fail(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    _runner = RaisingRunner()  # never invoked anywhere below — proof by construction
    run_preflight(
        worktree, config=PreflightConfig(check_command=["true"]), output_dir=tmp_path / "out"
    )
    with pytest.raises(PreflightFailure):
        run_preflight(
            worktree, config=PreflightConfig(check_command=["false"]), output_dir=tmp_path / "out"
        )


def test_detect_check_command_precedence(tmp_path):
    uv_dir = tmp_path / "uv"
    uv_dir.mkdir()
    (uv_dir / "pyproject.toml").write_text("[project]\n")
    assert detect_check_command(uv_dir) == ["uv", "run", "pytest"]

    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()
    (lock_dir / "uv.lock").write_text("")
    assert detect_check_command(lock_dir) == ["uv", "run", "pytest"]

    node_dir = tmp_path / "node"
    node_dir.mkdir()
    (node_dir / "package.json").write_text("{}")
    assert detect_check_command(node_dir) == ["npm", "test"]

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert detect_check_command(empty_dir) is None


def test_check_command_resolution_logged_once(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    (worktree / "pyproject.toml").write_text("[project]\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "add pyproject")
    logged = []
    with pytest.raises(PreflightFailure):
        # No real uv/pytest project here, so the resolved command will fail —
        # what matters is that resolution is logged exactly once.
        run_preflight(
            worktree, config=PreflightConfig(), output_dir=tmp_path / "out", log=logged.append
        )
    resolution_lines = [line for line in logged if "uv run pytest" in line]
    assert len(resolution_lines) == 1


def test_no_check_detectable_logs_and_passes(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    logged = []
    run_preflight(
        worktree, config=PreflightConfig(), output_dir=tmp_path / "out", log=logged.append
    )
    assert any("no check command" in line for line in logged)


def test_check_timeout_is_killed_and_named_as_the_failure(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")
    config = PreflightConfig(
        check_command=["python3", "-c", "import time; time.sleep(5)"], check_timeout_s=0.2
    )
    with pytest.raises(PreflightFailure, match="timed out") as excinfo:
        run_preflight(worktree, config=config, output_dir=tmp_path / "out")
    assert excinfo.value.kind == "timeout"


# --------------------------------------------------------- failure kind (U1)


def _init_repo(worktree: Path) -> None:
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    git(worktree, "commit", "--allow-empty", "-m", "init")


def test_exit_code_2_classifies_as_env(tmp_path):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    config = PreflightConfig(check_command=["python3", "-c", "import sys; sys.exit(2)"])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=config, output_dir=tmp_path / "out")
    assert excinfo.value.kind == "env"


@pytest.mark.parametrize("exit_code", [3, 4, 5])
def test_other_nonzero_exit_codes_classify_as_env(tmp_path, exit_code):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    config = PreflightConfig(check_command=["python3", "-c", f"import sys; sys.exit({exit_code})"])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=config, output_dir=tmp_path / "out")
    assert excinfo.value.kind == "env"


def test_exit_1_with_collection_error_classifies_as_env(tmp_path):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    script = "import sys\nprint('ImportError while loading conftest.py')\nsys.exit(1)\n"
    config = PreflightConfig(check_command=["python3", "-c", script])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=config, output_dir=tmp_path / "out")
    assert excinfo.value.kind == "env"


def test_exit_1_with_ordinary_failure_classifies_as_regression(tmp_path):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    script = "import sys\nprint('AssertionError: 1 != 2')\nsys.exit(1)\n"
    config = PreflightConfig(check_command=["python3", "-c", script])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=config, output_dir=tmp_path / "out")
    assert excinfo.value.kind == "regression"


def test_check_run_leaves_worktree_clean_and_writes_junit_outside(tmp_path):
    """A real, auto-detected pytest check leaves no .pytest_cache or JUnit XML
    inside the worktree (plan U1) — everything lands in output_dir instead."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    (worktree / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\nversion = '0'\nrequires-python = '>=3.11'\n"
        "[tool.pytest.ini_options]\n"
    )
    (worktree / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    (worktree / ".gitignore").write_text("__pycache__/\n")
    subprocess.run(["uv", "lock"], cwd=worktree, capture_output=True, text=True, check=True)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "add sample project")

    out_dir = tmp_path / "out"
    run_preflight(worktree, config=PreflightConfig(), output_dir=out_dir)

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
    )
    assert status.stdout.strip() == ""
    assert not (worktree / ".pytest_cache").exists()
    junit_path = out_dir / "preflight-junit.xml"
    assert junit_path.is_file()


# --------------------------------------------------- wired into merge_group


def test_merge_group_refreshes_the_worktree_before_checking(repo):
    """A check command reads the integration branch's newer content in the
    worktree it runs in — proof merge_group's refresh happens before Preflight
    runs, not after."""
    merger = IntegrationMerger(
        repo, "r1", preflight_config=PreflightConfig(check_command=["cat", "tip.txt"])
    )
    g1 = make_group("g1", files=["tip.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "own.txt", "g1 own work\n", "feat: g1")

    g2 = make_group("g2", files=["tip.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "tip.txt", "from g2\n", "feat: g2 adds tip.txt")
    merger.merge_group(g2, wt2)

    # g1's own worktree has never seen tip.txt — `cat tip.txt` fails unless
    # merge_group's refresh brought it in first, which would raise here.
    merger.merge_group(g1, wt1)
    integration_wt = merger.ensure()
    tree = git(integration_wt, "ls-tree", "--name-only", merger.branch)
    assert "own.txt" in tree and "tip.txt" in tree


def test_single_lock_hold_across_refresh_check_and_merge(repo):
    """A slow check command must not let a second merge_group call interleave
    between refresh and merge."""
    sentinel = repo / "release.flag"
    started_flag = repo / "started.flag"
    check_script = repo / "wait_and_pass.sh"
    check_script.write_text(
        f"#!/bin/sh\ntouch {started_flag}\nwhile [ ! -f {sentinel} ]; do sleep 0.02; done\nexit 0\n"
    )
    check_script.chmod(0o755)

    merger = IntegrationMerger(
        repo, "r1", preflight_config=PreflightConfig(check_command=[str(check_script)])
    )
    g1 = make_group("g1", files=["a.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "a.txt", "a\n", "feat: g1")
    g2 = make_group("g2", files=["b.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "b.txt", "b\n", "feat: g2")

    results = {}

    def run_g1():
        merger.merge_group(g1, wt1)
        results["g1_done_at"] = time.monotonic()

    t = threading.Thread(target=run_g1)
    t.start()
    # Wait for g1's check to actually be running (holding the lock).
    deadline = time.monotonic() + 5
    while not started_flag.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started_flag.exists(), "g1's check command never started"

    # g2's merge_group must block until g1 releases the lock — verified by
    # checking g2 cannot complete before the sentinel is written.
    sentinel.write_text("go\n")
    merger.merge_group(g2, wt2)
    t.join(timeout=5)
    assert "g1_done_at" in results
    assert results["g1_done_at"] <= time.monotonic()


def test_semantic_conflict_blocked_check_passes_alone_fails_after_merge(repo):
    """The check passes for each group in isolation, but fails once the
    refresh brings both groups' independent, disjoint-file changes together —
    the semantic-merge-conflict case Preflight exists to catch. Neither group
    touches the other's declared file, so this is not a textual conflict."""
    (repo / "a.txt").write_text("OLD\n")
    (repo / "b.txt").write_text("OLD\n")
    (repo / "check.py").write_text(
        "import pathlib, sys\n"
        "a = pathlib.Path('a.txt').read_text()\n"
        "b = pathlib.Path('b.txt').read_text()\n"
        "sys.exit(1 if (a == 'NEW\\n' and b == 'NEW2\\n') else 0)\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed a.txt/b.txt/check.py")

    merger = IntegrationMerger(
        repo, "r1", preflight_config=PreflightConfig(check_command=["python3", "check.py"])
    )
    g1 = make_group("g1", files=["a.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "a.txt", "NEW\n", "feat: g1 changes a.txt")

    g2 = make_group("g2", files=["b.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "b.txt", "NEW2\n", "feat: g2 changes b.txt")
    merger.merge_group(g2, wt2)  # passes alone: a stays OLD, b becomes NEW2
    tip_before = merger.tip()

    # g1's refresh onto the new tip brings in b.txt=NEW2 alongside its own
    # a.txt=NEW — the combination the check rejects — with no textual conflict.
    with pytest.raises(PreflightFailure):
        merger.merge_group(g1, wt1)
    assert merger.tip() == tip_before


def test_textual_refresh_conflict_routes_to_merge_conflict_not_preflight(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1", files=["shared.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "shared.txt", "g1 version\n", "feat: g1 edits shared")

    g2 = make_group("g2", files=["shared.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "shared.txt", "g2 version\n", "feat: g2 edits shared")
    merger.merge_group(g2, wt2)
    tip_before = merger.tip()

    with pytest.raises(MergeConflict, match="shared.txt"):
        merger.merge_group(g1, wt1)
    assert merger.tip() == tip_before


def test_self_verify_group_is_still_gated_by_preflight(repo):
    merger = IntegrationMerger(
        repo,
        "r1",
        preflight_config=PreflightConfig(
            check_command=["python3", "-c", "import sys; sys.exit(1)"]
        ),
    )
    g1 = make_group("g1")
    assert g1.intensity == ReviewIntensity.SELF_VERIFY
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "one.txt", "g1 work\n", "feat: g1")
    with pytest.raises(PreflightFailure):
        merger.merge_group(g1, wt1)


# ------------------------------------------------- declared-file drift (F11)


def test_missing_declared_file_is_reported_and_does_not_block(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.email", "t@t")
    git(worktree, "config", "user.name", "t")
    (worktree / "made_it.py").write_text("x = 1\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "init")
    logged = []
    run_preflight(
        worktree,
        config=PreflightConfig(),
        output_dir=tmp_path / "out",
        log=logged.append,
        declared_files=["made_it.py", "tests/never_written.py"],
    )
    reports = [line for line in logged if "declared file(s) not present" in line]
    assert len(reports) == 1
    assert "tests/never_written.py" in reports[0]
    # Reported, never gated: the file it did create is not named as missing.
    assert "made_it.py" not in reports[0].split(":", 1)[1]


def test_merge_reports_the_group_s_undelivered_declared_files_and_still_merges(repo):
    logged = []
    merger = IntegrationMerger(repo, "r1", log=logged.append)
    # The group declares two files and delivers only one — g1's real shape on
    # run r20260819-crashrec, where the declared test file was never created.
    g1 = make_group("g1", files=["one.txt", "tests/test_worktrees.py"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "one.txt", "g1 work\n", "feat: g1")

    merger.merge_group(g1, wt1)

    assert merger.merged == [g1]
    reports = [line for line in logged if "declared file(s) not present" in line]
    assert len(reports) == 1
    assert "tests/test_worktrees.py" in reports[0]


# ------------------------------------------------- baseline-aware merge gate


def _junit_writer(results: dict[str, str]) -> str:
    """A check command that writes a JUnit report with `results` and exits 1."""
    cases = "".join(
        f'<testcase classname="tests/test_x.py" name="{name}">'
        + ("<failure/>" if outcome == "failed" else "")
        + "</testcase>"
        for name, outcome in results.items()
    )
    return (
        "import sys, pathlib\n"
        f"xml = {'''<testsuite>''' + cases + '''</testsuite>'''!r}\n"
        "path = [a for a in sys.argv if a.startswith('--junitxml=')]\n"
        "pathlib.Path(path[0].split('=', 1)[1]).write_text(xml)\n"
        "print('AssertionError: short test summary info')\n"
        "sys.exit(1)\n"
    )


def _run_with_junit(worktree: Path, out_dir: Path, results: dict[str, str], baseline):
    script = _junit_writer(results)
    config = PreflightConfig(
        check_command=[
            "python3",
            "-c",
            script,
            f"--junitxml={out_dir / 'preflight-junit.xml'}",
        ]
    )
    return run_preflight(worktree, config=config, output_dir=out_dir, baseline=baseline)


def test_pre_existing_failures_allow_the_merge(tmp_path):
    """A check exiting nonzero whose every failing test was already red on the
    launch branch is not a regression — the tree merges rather than failing."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    baseline = PreflightBaseline(
        command=["pytest"],
        commit_sha="deadbeef",
        exit_code=1,
        tests={"tests/test_x.py::test_old": "failed"},
    )
    logged: list[str] = []
    script = _junit_writer({"test_old": "failed", "test_fine": "passed"})
    out_dir = tmp_path / "out"
    config = PreflightConfig(
        check_command=[
            "python3",
            "-c",
            script,
            f"--junitxml={out_dir / 'preflight-junit.xml'}",
        ]
    )
    run_preflight(worktree, config=config, output_dir=out_dir, log=logged.append, baseline=baseline)
    assert any("already red on the launch branch" in line for line in logged)


def test_one_new_failure_among_pre_existing_ones_still_blocks(tmp_path):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    baseline = PreflightBaseline(
        command=["pytest"],
        commit_sha="deadbeef",
        exit_code=1,
        tests={"tests/test_x.py::test_old": "failed"},
    )
    out_dir = tmp_path / "out"
    with pytest.raises(PreflightFailure) as excinfo:
        _run_with_junit(worktree, out_dir, {"test_old": "failed", "test_new": "failed"}, baseline)
    assert excinfo.value.kind == "regression"


def test_without_a_baseline_the_strict_exit_code_gate_is_unchanged(tmp_path):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    out_dir = tmp_path / "out"
    with pytest.raises(PreflightFailure):
        _run_with_junit(worktree, out_dir, {"test_old": "failed"}, None)


def test_a_collection_error_is_never_excused_by_the_baseline(tmp_path):
    """Only `regression` failures are eligible: an env failure produced no
    comparable result set, so a matching baseline must not let it through."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    baseline = PreflightBaseline(command=["pytest"], commit_sha="deadbeef", exit_code=1, tests={})
    script = "import sys\nprint('ModuleNotFoundError: no module named x')\nsys.exit(1)\n"
    config = PreflightConfig(check_command=["python3", "-c", script])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=config, output_dir=tmp_path / "out", baseline=baseline)
    assert excinfo.value.kind == "env"


# --------------------------------------------------- UI steps in the gate (A)


def _ui_project(root: Path, *, node_modules: bool = True, dev: dict | None = None) -> None:
    """The marker set `detect_check_steps` reads for the dashboard's suites."""
    ui = root / "ui"
    ui.mkdir(parents=True, exist_ok=True)
    (ui / "package.json").write_text(
        json.dumps({"devDependencies": dev if dev is not None else {"vitest": "^4", "typescript": "^5"}})
    )
    (ui / "tsconfig.json").write_text("{}")
    if node_modules:
        (ui / "node_modules").mkdir(exist_ok=True)


def test_uv_and_ui_markers_detect_pytest_then_vitest_then_tsc(tmp_path):
    """The bug this closes: marker *precedence* meant `pyproject.toml` won and
    the UI was never checked at all. Both suites are detected now, in order."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    _ui_project(root)
    out_dir = tmp_path / "out"

    steps = detect_check_steps(root, output_dir=out_dir)

    assert [step.name for step in steps] == ["pytest", "vitest", "tsc"]
    pytest_step, vitest_step, tsc_step = steps
    assert pytest_step.junit_path == out_dir / "preflight-junit.xml"
    assert pytest_step.subdir == "."
    assert vitest_step.subdir == "ui"
    assert vitest_step.id_prefix == "ui::"
    assert vitest_step.junit_path == out_dir / "preflight-junit-ui.xml"
    assert f"--outputFile={out_dir / 'preflight-junit-ui.xml'}" in vitest_step.argv
    assert tsc_step.argv == ["npx", "tsc", "--noEmit"]
    assert tsc_step.junit_path is None


def test_ui_steps_are_skipped_not_failed_without_node_modules(tmp_path):
    """A missing `ui/node_modules` weakens the gate; it must never fail it — an
    `env` preflight failure raises GroupFailure, which halts the whole run."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    _ui_project(worktree, node_modules=False)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "add ui")

    assert detect_check_steps(worktree, output_dir=tmp_path / "out") == []

    baseline = PreflightBaseline(
        command=["npx", "vitest"],
        commit_sha="deadbeef",
        exit_code=0,
        steps=[BaselineStep(name="vitest", command=["npx", "vitest"], exit_code=0)],
    )
    logged: list[str] = []
    run_preflight(
        worktree,
        config=PreflightConfig(),
        output_dir=tmp_path / "out",
        log=logged.append,
        baseline=baseline,
    )
    assert any(
        "step 'vitest' was in the baseline but is skipped here (no ui/node_modules)" in line
        for line in logged
    )


def _inject_steps(monkeypatch, steps: list[CheckStep]) -> None:
    monkeypatch.setattr(
        "orchestrator.execution.preflight.detect_check_steps",
        lambda root, *, output_dir, junit_stem="preflight-junit": steps,
    )


def _vitest_step(out_dir: Path, results: dict[str, str]) -> CheckStep:
    """A vitest-shaped step: writes a JUnit report with vitest's own
    `classname="src/x.test.ts"` shape and exits 1."""
    junit = out_dir / "preflight-junit-ui.xml"
    cases = "".join(
        f'<testcase classname="src/attempts.test.ts" name="{name}">'
        + ("<failure/>" if outcome == "failed" else "")
        + "</testcase>"
        for name, outcome in results.items()
    )
    script = (
        "import sys, pathlib\n"
        f"xml = {'<testsuite>' + cases + '</testsuite>'!r}\n"
        f"pathlib.Path({str(junit)!r}).parent.mkdir(parents=True, exist_ok=True)\n"
        f"pathlib.Path({str(junit)!r}).write_text(xml)\n"
        "print('FAIL src/attempts.test.ts')\n"
        "sys.exit(1)\n"
    )
    return CheckStep(
        name="vitest",
        argv=["python3", "-c", script],
        junit_path=junit,
        id_prefix="ui::",
    )


def test_a_vitest_failure_blocks_the_merge_with_ui_prefixed_ids(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    out_dir = tmp_path / "out"
    _inject_steps(monkeypatch, [_vitest_step(out_dir, {"renders a row": "failed"})])
    baseline = PreflightBaseline(
        command=["npx", "vitest"],
        commit_sha="deadbeef",
        exit_code=0,
        steps=[BaselineStep(name="vitest", command=["npx", "vitest"], exit_code=0)],
    )

    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=PreflightConfig(), output_dir=out_dir, baseline=baseline)

    assert excinfo.value.kind == "regression"
    assert excinfo.value.step_name == "vitest"
    assert excinfo.value.comparison is not None
    assert excinfo.value.comparison.new_failures == frozenset(
        {"ui::src/attempts.test.ts::renders a row"}
    )


def _tsc_step() -> CheckStep:
    return CheckStep(
        name="tsc",
        subdir=".",
        argv=[
            "python3",
            "-c",
            "import sys\nprint(\"src/types.ts(3,7): error TS2322\")\nsys.exit(1)\n",
        ],
    )


def test_a_tsc_failure_blocks_when_the_baseline_step_was_clean(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    _inject_steps(monkeypatch, [_tsc_step()])
    baseline = PreflightBaseline(
        command=["npx", "tsc"],
        commit_sha="deadbeef",
        exit_code=0,
        steps=[BaselineStep(name="tsc", command=["npx", "tsc", "--noEmit"], exit_code=0)],
    )

    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(
            worktree, config=PreflightConfig(), output_dir=tmp_path / "out", baseline=baseline
        )
    assert excinfo.value.kind == "regression"
    assert excinfo.value.step_name == "tsc"


def test_the_same_tsc_failure_is_excused_when_the_baseline_step_also_failed(tmp_path, monkeypatch):
    """A report-less step is comparable by exit code: red at launch and red
    here introduced nothing, so the tree merges."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    _inject_steps(monkeypatch, [_tsc_step()])
    baseline = PreflightBaseline(
        command=["npx", "tsc"],
        commit_sha="deadbeef",
        exit_code=1,
        steps=[BaselineStep(name="tsc", command=["npx", "tsc", "--noEmit"], exit_code=1)],
    )
    logged: list[str] = []

    run_preflight(
        worktree,
        config=PreflightConfig(),
        output_dir=tmp_path / "out",
        log=logged.append,
        baseline=baseline,
    )
    assert any("exited nonzero on the launch branch too" in line for line in logged)


def test_a_regression_with_no_junit_and_no_baseline_step_is_not_excused(tmp_path, monkeypatch):
    """[A1] `∅ - baseline` is `∅`, which reads as "nothing new" against *every*
    baseline — the hole that let a report-less regression merge silently. No
    comparable evidence is no excuse."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    out_dir = tmp_path / "out"
    # A JUnit step whose report is never written, plus a baseline that knows
    # nothing about this step: both halves of the evidence test come up empty.
    step = CheckStep(
        name="vitest",
        argv=["python3", "-c", "import sys\nprint('AssertionError: 1 != 2')\nsys.exit(1)\n"],
        junit_path=out_dir / "preflight-junit-ui.xml",
        id_prefix="ui::",
    )
    _inject_steps(monkeypatch, [step])
    baseline = PreflightBaseline(
        command=["uv", "run", "pytest"],
        commit_sha="deadbeef",
        exit_code=0,
        steps=[BaselineStep(name="pytest", command=["uv", "run", "pytest"], exit_code=0)],
    )

    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=PreflightConfig(), output_dir=out_dir, baseline=baseline)
    assert excinfo.value.kind == "regression"
    assert excinfo.value.comparison is not None
    assert excinfo.value.comparison.verdict == "new_failures"


def test_a_report_less_step_is_not_excused_by_an_unrelated_baseline_step(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    _inject_steps(monkeypatch, [_tsc_step()])
    baseline = PreflightBaseline(
        command=["uv", "run", "pytest"],
        commit_sha="deadbeef",
        exit_code=1,
        steps=[BaselineStep(name="pytest", command=["uv", "run", "pytest"], exit_code=1)],
    )
    with pytest.raises(PreflightFailure):
        run_preflight(
            worktree, config=PreflightConfig(), output_dir=tmp_path / "out", baseline=baseline
        )


def test_a_configured_pytest_check_command_gets_a_junitxml(tmp_path):
    """[A1] `config.check_command` short-circuited the `or` in front of the
    detector, so an operator-configured pytest run produced no report — and a
    report-less regression compared an empty set against the baseline."""
    out_dir = tmp_path / "out"
    step = configured_check_step(
        ["uv", "run", "pytest"], output_dir=out_dir, junit_stem="preflight-junit"
    )
    assert step.argv == [
        "uv",
        "run",
        "pytest",
        "-p",
        "no:cacheprovider",
        f"--junitxml={out_dir / 'preflight-junit.xml'}",
    ]
    assert step.junit_path == out_dir / "preflight-junit.xml"

    # A non-pytest command is left exactly as configured, but still gets the
    # canonical report path — it may write one itself.
    other = configured_check_step(["make", "check"], output_dir=out_dir, junit_stem="preflight-junit")
    assert other.argv == ["make", "check"]
    assert other.junit_path == out_dir / "preflight-junit.xml"


def test_a_step_failure_stops_the_run_before_later_steps(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    marker = tmp_path / "second-ran"
    first = CheckStep(name="pytest", argv=["python3", "-c", "import sys; sys.exit(1)"])
    second = CheckStep(
        name="tsc",
        argv=["python3", "-c", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')"],
    )
    _inject_steps(monkeypatch, [first, second])
    with pytest.raises(PreflightFailure):
        run_preflight(worktree, config=PreflightConfig(), output_dir=tmp_path / "out")
    assert not marker.exists()


def test_a_non_pytest_step_exit_2_is_a_regression_not_an_env_failure(tmp_path, monkeypatch):
    """`tsc` exits 2 on an ordinary type error. Read through pytest's exit-code
    table that is `env` — not attributable, so no rewrite, and a halted run.
    The table applies to the pytest step only."""
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    step = CheckStep(name="tsc", argv=["python3", "-c", "import sys; sys.exit(2)"])
    _inject_steps(monkeypatch, [step])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=PreflightConfig(), output_dir=tmp_path / "out")
    assert excinfo.value.kind == "regression"
    assert excinfo.value.step_name == "tsc"


def test_the_pytest_step_keeps_the_pytest_exit_code_table(tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    _init_repo(worktree)
    step = CheckStep(name="pytest", argv=["python3", "-c", "import sys; sys.exit(2)"])
    _inject_steps(monkeypatch, [step])
    with pytest.raises(PreflightFailure) as excinfo:
        run_preflight(worktree, config=PreflightConfig(), output_dir=tmp_path / "out")
    assert excinfo.value.kind == "env"
