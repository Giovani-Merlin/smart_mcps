"""Provisioning failures are loud and fatal by default (orchestrator-notes
#8/#10), the merge gate tests the provisioned environment, `resume` keeps the
launch execution config (#6), and `status` defaults to the run that matters (#9)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from orchestrator.cli import _load_config, main
from orchestrator.config import ExecutionConfig, SessionConfig
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.preflight import detect_check_steps
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.execution.worktrees import ProvisioningError, provision_env

LONG_STDERR = (
    "Using CPython 3.12.13\nCreating virtual environment\n"
    + "Building pandas\n" * 200
    + ("x Failed to build `tts==0.22.0`\nRuntimeError: TTS requires python >= 3.9 and < 3.12\n")
)


def _uv_worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "uv.lock").write_text("")
    return wt


def failing_run(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 2, stdout="", stderr=LONG_STDERR)


def test_failure_log_carries_the_tail_not_the_head(tmp_path, capsys):
    events: list[str] = []
    details: list[str] = []
    assert (
        provision_env(
            _uv_worktree(tmp_path), runner=failing_run, log=events.append, on_detail=details.append
        )
        is False
    )
    assert "Failed to build `tts==0.22.0`" in events[0]
    assert "exit 2" in events[0]
    assert "Creating virtual environment" not in events[0]  # the head is gone
    assert details == [events[0].split(": ", 1)[1]]


def test_strict_mode_raises_after_recording(tmp_path):
    events: list[str] = []
    states: list[tuple[str, list[str]]] = []
    with pytest.raises(ProvisioningError) as excinfo:
        provision_env(
            _uv_worktree(tmp_path),
            runner=failing_run,
            log=events.append,
            on_state=lambda s, a: states.append((s, a)),
            extra_args=["--all-extras"],
            strict=True,
        )
    message = str(excinfo.value)
    assert "Failed to build `tts==0.22.0`" in message
    assert "resume" in message and "provision_on_failure" in message
    assert states == [("failed", ["uv", "sync", "--all-extras"])]
    assert events and "provisioning failed" in events[0]


def test_strict_mode_is_the_default_policy():
    assert SessionConfig().provision_on_failure == "fail"


def test_strict_mode_covers_a_missing_uv(tmp_path):
    def missing_uv(argv, **kwargs):
        raise OSError("No such file or directory: 'uv'")

    with pytest.raises(ProvisioningError):
        provision_env(_uv_worktree(tmp_path), runner=missing_uv, strict=True)


def test_gate_runs_pytest_with_the_provision_args(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    [step] = detect_check_steps(tmp_path, output_dir=tmp_path / "out", uv_run_args=["--all-extras"])
    assert step.argv[:4] == ["uv", "run", "--all-extras", "pytest"]
    [plain] = detect_check_steps(tmp_path, output_dir=tmp_path / "out")
    assert plain.argv[:3] == ["uv", "run", "pytest"]


def test_workspace_seam_reports_the_failure_in_warn_mode(tmp_path, monkeypatch):
    from orchestrator.cli import _workspace_seams
    from orchestrator.execution.merge import IntegrationMerger
    from tests.test_cli import make_group

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["commit", "--allow-empty", "-m", "i"]):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    merger = IntegrationMerger(repo, "r1")
    merger.ensure()

    def fake_provision(path, *, on_detail=None, on_state=None, strict=False, **_):
        assert strict is False
        on_detail("exit 2\nx Failed to build tts")
        on_state("failed", ["uv", "sync"])
        return False

    monkeypatch.setattr("orchestrator.cli.provision_env", fake_provision)
    paths = RunPaths(repo, "r1")
    workspace_for, _, failure_for = _workspace_seams(
        repo, "r1", merger, paths, SessionConfig(provision_on_failure="warn")
    )
    group = make_group("g1")
    workspace_for(group)
    assert failure_for(group) == "exit 2\nx Failed to build tts"
    assert failure_for(make_group("g2")) is None
    record = (paths.group_dir("g1") / "provisioning.json").read_text()
    assert "Failed to build tts" in record


# ------------------------------------------------------------ resume config


def test_load_config_restores_the_persisted_execution_config(tmp_path):
    args = argparse.Namespace(config=None, concurrency=None)
    config = _load_config(args, tmp_path, persisted_execution=ExecutionConfig(concurrency=4))
    assert config.execution.concurrency == 4
    # An explicit flag on resume still wins.
    args = argparse.Namespace(config=None, concurrency=2)
    config = _load_config(args, tmp_path, persisted_execution=ExecutionConfig(concurrency=4))
    assert config.execution.concurrency == 2


# --------------------------------------------------------------- status default


def test_status_without_run_id_shows_the_unfinished_run(tmp_path, capsys):
    for run_id, state in (
        ("r1", GroupState.COMPLETED),
        ("r2", GroupState.RUNNING),
        ("r3", GroupState.COMPLETED),
    ):
        paths = RunPaths(tmp_path, run_id)
        atomic_write_text(
            paths.state_path,
            RunState(run_id=run_id, groups={"g1": GroupRunState(state=state)}).model_dump_json(),
        )
    assert main(["status", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "(showing r2" in out and "run r2" in out
    assert "known runs: r1, r2, r3" in out


# ---------------------------------------------------------- HEAD precondition


def test_run_on_a_repo_with_no_commits_names_the_fix(tmp_path, capsys):
    from tests.test_cli import write_run_artifacts

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    write_run_artifacts(tmp_path)
    assert main(["run", "--repo", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "has no commits" in err and "initial commit" in err
    assert "invalid reference" not in err
