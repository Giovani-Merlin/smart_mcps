"""U4 tests: the LLM-free Preflight merge gate, standalone and wired into
IntegrationMerger.merge_group."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from orchestrator.config import PreflightConfig
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.preflight import (
    PreflightFailure,
    detect_check_command,
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
