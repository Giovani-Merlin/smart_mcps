"""A worker's own run directory is passed as a read root (`--add-dir`).

The stub tests run against tests/fake_claude.py and spend no tokens. The live
test (opt in with `-m llm`) asks the real CLI whether a compound command reading
the run directory is still refused.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.execution.sessions import SessionRunner, _argv_context, _session_id_from_context

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def _runner(fake_home: Path) -> SessionRunner:
    return SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env={"FAKE_CLAUDE_HOME": str(fake_home)},
        transcript_root=fake_home / "projects",
    )


def _last_argv(fake_home: Path) -> list[str]:
    lines = (fake_home / "calls.jsonl").read_text().splitlines()
    return json.loads(lines[-1])["argv"]


def _add_dir_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]


def test_start_worker_emits_one_add_dir_for_the_run_directory(fake_home, tmp_path):
    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    _runner(fake_home).start_worker(
        base_context="", prompt="go", name="r1-g1", cwd=tmp_path, add_dirs=[run_dir]
    )
    assert _add_dir_values(_last_argv(fake_home)) == [str(run_dir)]


def test_no_add_dirs_means_no_flag(fake_home, tmp_path):
    runner = _runner(fake_home)
    runner.start_worker(base_context="", prompt="go", name="r1-g1", cwd=tmp_path, add_dirs=())
    assert "--add-dir" not in _last_argv(fake_home)
    runner.start_worker(base_context="", prompt="go", name="r1-g1", cwd=tmp_path)
    assert "--add-dir" not in _last_argv(fake_home)


def test_resume_and_fork_pass_add_dirs_through(fake_home, tmp_path):
    runner = _runner(fake_home)
    run_dir = tmp_path / "run"
    first = runner.start_worker(base_context="", prompt="go", name="n", cwd=tmp_path)
    runner.resume(session_id=first.session_id, prompt="again", cwd=tmp_path, add_dirs=[run_dir])
    assert _add_dir_values(_last_argv(fake_home)) == [str(run_dir)]
    runner.start_fork(
        base_id=first.session_id, prompt="fork", name="f", cwd=tmp_path, add_dirs=[run_dir]
    )
    assert _add_dir_values(_last_argv(fake_home)) == [str(run_dir)]


def test_context_line_names_add_dir_without_breaking_the_session_id():
    context = _argv_context(["--resume", "abc-123"], [Path("/x/run")])
    assert "--add-dir /x/run" in context
    assert _session_id_from_context(context) == "abc-123"
    assert _session_id_from_context(_argv_context(["--session-id", "s1"])) == "s1"


@pytest.mark.llm
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_a_compound_read_of_the_run_directory_runs_but_tmp_is_still_refused(tmp_path):
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.log").write_text("x\n")
    work = tmp_path / "work"
    work.mkdir()

    def ran(path: Path) -> bool:
        proc = subprocess.run(
            [
                "claude", "--print", "--output-format", "json",
                "--permission-mode", "acceptEdits", "--setting-sources", "",
                "--allowedTools", "Bash(ls *)",
                "--add-dir", str(run_dir),
            ],
            input=f"Run the shell command `ls {path} && echo PROBE_OK_42` with the Bash tool.",
            capture_output=True, text=True, cwd=work, timeout=120, env=dict(os.environ),
        )  # fmt: skip
        return "PROBE_OK_42" in proc.stdout

    assert ran(run_dir)
    assert not ran(Path("/tmp"))
