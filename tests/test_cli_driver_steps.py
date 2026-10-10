"""U10: the `driver-item` and `data-step` subcommands, the status lines they feed,
and the launch-time data-layer warning."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from orchestrator.cli import main
from orchestrator.execution.manifest import RunPaths
from orchestrator.model import ReviewIntensity
from test_cli import make_group, write_run_artifacts
from test_e2e_stub import (  # noqa: F401 — fixtures
    StubLlm,
    coder_entry,
    fake_home,
    name_of,
    repo,
    script_session,
    write_config,
)

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "r20260828-220035"
RUN = FIXTURE.name


def _cli(*args: str, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-m", "orchestrator.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _fixture_run(tmp_path: Path) -> RunPaths:
    shutil.copytree(FIXTURE, tmp_path / ".orchestrator" / "runs" / RUN)
    return RunPaths(tmp_path, RUN)


def test_driver_item_records_and_status_prints_it(tmp_path):
    paths = _fixture_run(tmp_path)
    done = _cli(
        "driver-item", RUN, "g1", "g1-2", "--status", "pass", "--notes", "ok",
        "--repo", str(tmp_path),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert "recorded g1-2 pass for g1" in done.stdout
    record = json.loads(paths.driver_items_path("g1").read_text())
    assert record["g1-2"]["status"] == "pass" and record["g1-2"]["notes"] == "ok"
    status = _cli("status", RUN, "--repo", str(tmp_path))
    assert "driver items: g1-2 pass" in status.stdout


def test_driver_item_unknown_item_exits_1_naming_known_ids(tmp_path):
    _fixture_run(tmp_path)
    done = _cli("driver-item", RUN, "g1", "nope", "--status", "pass", "--repo", str(tmp_path))
    assert done.returncode == 1
    assert "error:" in done.stderr and "g1-1" in done.stderr


def test_driver_item_notes_file_reads_stdin(tmp_path):
    paths = _fixture_run(tmp_path)
    done = subprocess.run(
        [
            sys.executable, "-m", "orchestrator.cli", "driver-item", RUN, "g1", "g1-1",
            "--status", "skipped", "--notes-file", "-", "--repo", str(tmp_path),
        ],
        input="from `stdin`",
        capture_output=True,
        text=True,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert json.loads(paths.driver_items_path("g1").read_text())["g1-1"]["notes"] == "from `stdin`"


def test_driver_item_and_data_step_help():
    for command, needles in (
        ("driver-item", ("--status", "--notes", "--notes-file")),
        ("data-step", ("run_id", "cmd")),
    ):
        done = _cli(command, "--help")
        assert done.returncode == 0
        assert all(needle in done.stdout for needle in needles)


def test_data_step_exit_status_and_log(tmp_path):
    paths = _fixture_run(tmp_path)
    done = _cli("data-step", RUN, "--repo", str(tmp_path), "--", "sh", "-c", "exit 3")
    assert done.returncode == 3
    log = paths.event_log_path.read_text()
    assert "data step started" in log and "data step finished (exit 3)" in log


def test_data_step_without_command_is_a_usage_error(tmp_path):
    paths = _fixture_run(tmp_path)
    done = _cli("data-step", RUN, "--repo", str(tmp_path), "--")
    assert done.returncode == 2
    assert not paths.data_step_lock_path.exists()


def test_data_step_quoting_and_cwd_survive_the_wrapper(tmp_path):
    paths = _fixture_run(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "orchestrator.cli", "data-step", RUN, "--repo", str(tmp_path),
            "--", "sh", "-c", "sleep 1; exit 3",
        ],
        cwd=sub,
    )  # fmt: skip
    record = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and record is None:
        if paths.data_step_record_path.is_file():
            try:
                record = json.loads(paths.data_step_record_path.read_text())
            except ValueError:
                pass
        time.sleep(0.05)
    assert proc.wait(timeout=30) == 3
    assert record is not None
    assert record["cmd"] == "sh -c 'sleep 1; exit 3'"
    assert Path(record["cwd"]).resolve() == sub.resolve()
    assert (
        "data step finished (exit 3): sh -c 'sleep 1; exit 3'" in paths.event_log_path.read_text()
    )


def _run_stub(repo, fake_home, run_id: str, extra: str) -> str:
    write_run_artifacts(
        repo, [make_group("g1", intensity=ReviewIntensity.SELF_VERIFY)], name="alpha"
    )
    (repo / "data").mkdir(exist_ok=True)
    write_config(repo, fake_home, extra)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "x\n"}, commit="g1: work"),
    )
    code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--grouping", "alpha"],
        llm_runner=StubLlm(),
    )
    assert code == 0
    return (repo / ".orchestrator" / "runs" / run_id / "logs" / "run.log").read_text()


def test_warning_logged_once_when_data_dirs_configured(repo, fake_home):
    log = _run_stub(repo, fake_home, "r-warn", '[workspace]\ndata_dirs = ["data"]\n')
    assert log.count("are read live by the merge gate") == 1
    assert "data-step r-warn -- <cmd>" in log


def test_warning_never_logged_without_data_dirs(repo, fake_home):
    log = _run_stub(repo, fake_home, "r-nowarn", "")
    assert "are read live by the merge gate" not in log
