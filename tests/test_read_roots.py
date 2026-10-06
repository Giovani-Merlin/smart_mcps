"""A worker's own run directory is passed as a read root (`--add-dir`).

The stub tests run against tests/fake_claude.py and spend no tokens. The live
test (opt in with `-m llm`) asks the real CLI whether a compound command reading
the run directory is still refused without `--add-dir` and runs with it.
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


def test_runner_level_extra_add_dirs_join_every_call_once(fake_home, tmp_path):
    """`extra_add_dirs` (the data layer's real directories, wired by the CLI)
    ride on every worker call after the per-call run directory, deduplicated.
    Run r20261006-162245 g10: `cp data/corpus.db …` — a plan verification item —
    was refused because `data/` resolves through a symlink to a directory the
    CLI did not count as a working directory."""
    data_root = tmp_path / "repo" / "data"
    runner = SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env={"FAKE_CLAUDE_HOME": str(fake_home)},
        transcript_root=fake_home / "projects",
        extra_add_dirs=[data_root],
    )
    run_dir = tmp_path / "run"
    runner.start_worker(base_context="", prompt="go", name="n", cwd=tmp_path, add_dirs=[run_dir])
    assert _add_dir_values(_last_argv(fake_home)) == [str(run_dir), str(data_root)]
    runner.start_worker(base_context="", prompt="go", name="n", cwd=tmp_path)
    assert _add_dir_values(_last_argv(fake_home)) == [str(data_root)]
    runner.start_worker(
        base_context="", prompt="go", name="n", cwd=tmp_path, add_dirs=[data_root, run_dir]
    )
    assert _add_dir_values(_last_argv(fake_home)) == [str(data_root), str(run_dir)]


def test_context_line_names_add_dir_without_breaking_the_session_id():
    context = _argv_context(["--resume", "abc-123"], [Path("/x/run")])
    assert "--add-dir /x/run" in context
    assert _session_id_from_context(context) == "abc-123"
    assert _session_id_from_context(_argv_context(["--session-id", "s1"])) == "s1"


@pytest.mark.llm
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_a_compound_read_of_the_run_directory_runs_only_with_add_dir(tmp_path):
    """The control is the same command without `--add-dir`, not a read of `/tmp`:
    the CLI lists `/tmp` freely, so that never measured the read root (run
    r20261006-115802, item g1-2). Without the flag the CLI refuses the run
    directory as outside the session's working directory; with it the compound
    command runs."""
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.log").write_text("x\n")
    work = tmp_path / "work"
    work.mkdir()

    def ran(*, add_dir: bool) -> bool:
        # The oracle is a file the compound command creates, not the model's
        # answer text: a refused command's explanation can still quote a marker.
        witness = work / f"ran-{'with' if add_dir else 'without'}-add-dir"
        argv = [
            "claude", "--print", "--output-format", "json",
            "--permission-mode", "acceptEdits", "--setting-sources", "",
            "--allowedTools", "Bash(ls *)", "Bash(touch *)",
        ]  # fmt: skip
        if add_dir:
            argv += ["--add-dir", str(run_dir)]
        subprocess.run(
            argv,
            input=f"Run the shell command `ls {run_dir} && touch {witness}` with the Bash tool.",
            capture_output=True, text=True, cwd=work, timeout=120, env=dict(os.environ),
        )  # fmt: skip
        return witness.exists()

    assert not ran(add_dir=False)
    assert ran(add_dir=True)


@pytest.mark.llm
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
def test_a_copy_through_a_data_symlink_runs_only_with_the_target_as_add_dir(tmp_path):
    """The data layer's exact shape: `<work>/data` is a symlink to a directory
    outside the working directory. The CLI resolves the link and refuses
    `cp data/corpus.db …` as "outside the allowed working directories" (run
    r20261006-162245 g10, a plan verification item); with the link's *target*
    as `--add-dir` the same command runs. Oracle: the copied file exists."""
    real_data = tmp_path / "repo" / "data"
    real_data.mkdir(parents=True)
    (real_data / "corpus.db").write_bytes(b"sqlite-ish")
    work = tmp_path / "work"
    work.mkdir()
    (work / "data").symlink_to(real_data, target_is_directory=True)

    def ran(*, add_dir: bool) -> bool:
        witness = work / f"copy-{'with' if add_dir else 'without'}-add-dir.db"
        argv = [
            "claude", "--print", "--output-format", "json",
            "--permission-mode", "acceptEdits", "--setting-sources", "",
            "--allowedTools", "Bash(cp *)",
        ]  # fmt: skip
        if add_dir:
            argv += ["--add-dir", str(real_data)]
        subprocess.run(
            argv,
            input=f"Run the shell command `cp data/corpus.db {witness.name}` with the Bash tool.",
            capture_output=True, text=True, cwd=work, timeout=120, env=dict(os.environ),
        )  # fmt: skip
        return witness.exists()

    assert not ran(add_dir=False)
    assert ran(add_dir=True)
