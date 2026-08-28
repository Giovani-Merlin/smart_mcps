"""U32 tests: the integration worktree is provisioned like a group worktree,
every worktree's provisioning is logged and recorded, and the record survives
the worktree it describes being torn down (plan U32)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.worktrees import (
    create_worktree,
    group_branch,
    provision_env,
    read_provisioning_record,
    remove_worktree,
    write_provisioning_record,
)
from orchestrator.model import Group, ReviewIntensity
from orchestrator.observatory.app import create_app


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


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


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


class TestIntegrationWorktreeProvisioned:
    """[g21-integration-provisioned] After a run establishes the integration
    worktree, that worktree contains a provisioned environment."""

    def test_ensure_provisions_the_integration_worktree(self, repo, monkeypatch):
        calls: list[tuple[Path, list[str]]] = []

        def fake_provision_env(path, *, log=None, env=None, extra_args=None, on_state=None, **_):
            (path / ".venv-marker").write_text("provisioned\n")
            calls.append((path, list(extra_args or [])))
            if on_state is not None:
                on_state("provisioned", ["uv", "sync", *(extra_args or [])])
            return True

        monkeypatch.setattr("orchestrator.execution.merge.provision_env", fake_provision_env)
        merger = IntegrationMerger(repo, "r1", provision_args=["--all-extras"])
        path = merger.ensure()

        assert (path / ".venv-marker").is_file()
        assert calls == [(path, ["--all-extras"])]

    def test_provisioning_is_attempted_exactly_once_per_process(self, repo, monkeypatch):
        """`tip()` calls `ensure()` on every group's workspace_for — re-syncing
        the integration worktree's venv on every one of those calls would be
        needless, repeated work."""
        calls: list[Path] = []

        def fake_provision_env(path, **kwargs):
            calls.append(path)
            on_state = kwargs.get("on_state")
            if on_state is not None:
                on_state("provisioned", ["uv", "sync"])
            return True

        monkeypatch.setattr("orchestrator.execution.merge.provision_env", fake_provision_env)
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        merger.tip()
        merger.tip()
        assert len(calls) == 1


class TestProvisioningLogLine:
    """[g21-log-per-worktree] The run log contains one line per provisioned
    worktree naming the worktree path and the exact provisioning command."""

    def test_provision_env_logs_the_worktree_path_and_exact_command(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname='x'\n")
        events: list[str] = []

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        assert (
            provision_env(worktree, runner=fake_run, log=events.append, extra_args=["--all-extras"])
            is True
        )
        assert len(events) == 1
        assert str(worktree) in events[0]
        assert "uv sync --all-extras" in events[0]

    def test_the_integration_worktrees_provisioning_reaches_the_run_log(self, repo, monkeypatch):
        logged: list[str] = []

        def fake_provision_env(path, *, log=None, extra_args=None, on_state=None, **_):
            if log is not None:
                log(f"worktree {path} was provisioned with `uv sync {' '.join(extra_args or [])}`")
            if on_state is not None:
                on_state("provisioned", ["uv", "sync", *(extra_args or [])])
            return True

        monkeypatch.setattr("orchestrator.execution.merge.provision_env", fake_provision_env)
        merger = IntegrationMerger(repo, "r1", provision_args=["--all-extras"], log=logged.append)
        path = merger.ensure()
        assert any(str(path) in line and "uv sync --all-extras" in line for line in logged)


class TestProvisioningFailureReported:
    """[g21-failure-reported] A provisioning failure is logged and reported
    rather than leaving the worktree silently unprovisioned."""

    def test_a_failing_sync_is_logged_and_recorded_as_failed(self, tmp_path):
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "pyproject.toml").write_text("[project]\nname='x'\n")
        events: list[str] = []
        states: list[tuple[str, list[str]]] = []

        def failing_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="resolution failed")

        result = provision_env(
            worktree,
            runner=failing_run,
            log=events.append,
            on_state=lambda state, argv: states.append((state, argv)),
        )
        assert result is False
        assert len(events) == 1 and "uv sync failed" in events[0]
        assert states == [("failed", ["uv", "sync"])]

    def test_a_failed_integration_provision_is_recorded_with_state_failed(self, repo, monkeypatch):
        def fake_provision_env(path, *, on_state=None, **_):
            if on_state is not None:
                on_state("failed", ["uv", "sync"])
            return False

        monkeypatch.setattr("orchestrator.execution.merge.provision_env", fake_provision_env)
        group_dir = repo / ".orchestrator" / "runs" / "r1" / "groups" / "integration"
        merger = IntegrationMerger(repo, "r1")
        path = merger.ensure()
        record = read_provisioning_record(group_dir)
        assert record is not None
        assert record["state"] == "failed"
        assert record["worktree"] == str(path)


class TestProvisioningSurvivesTeardown:
    """[g21-torn-down-still-shown] A group whose worktree was torn down still
    shows its recorded provisioning line rather than a blank."""

    def test_read_provisioning_record_survives_remove_worktree(self, repo, tmp_path):
        group_dir = tmp_path / "group-dir"
        branch = group_branch("r1", "g1")
        worktree = create_worktree(
            repo, run_id="r1", group_id="g1", name="fix auth", branch=branch, start_point="main"
        )
        write_provisioning_record(
            group_dir,
            worktree=worktree,
            command=["uv", "sync", "--all-extras"],
            state="provisioned",
        )
        remove_worktree(repo, worktree)
        assert not worktree.exists()

        record = read_provisioning_record(group_dir)
        assert record is not None
        assert record["state"] == "provisioned"
        assert record["worktree"] == str(worktree)
        assert record["command"] == ["uv", "sync", "--all-extras"]

    def test_read_provisioning_record_is_none_when_nothing_was_ever_written(self, tmp_path):
        assert read_provisioning_record(tmp_path / "never-provisioned") is None


class TestDrillInProvisioningState:
    """[g21-drillin-state] The group drill-in shows the group's worktree path
    and its provisioning state and time."""

    def _client(self, tmp_path: Path, repo: Path) -> TestClient:
        registry = tmp_path / "registry.yaml"
        registry.write_text(yaml.safe_dump({"projects": [{"name": "proj", "repo": str(repo)}]}))
        return TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

    def test_snapshot_serves_the_recorded_provisioning_state(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        run_dir = repo / ".orchestrator" / "runs" / "r1"
        group_dir = run_dir / "groups" / "g1"
        group_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps({"run_id": "r1", "groups": {"g1": {"state": "completed"}}})
        )
        write_provisioning_record(
            group_dir,
            worktree=repo / ".worktrees" / "r1" / "g1-fix-auth",
            command=["uv", "sync", "--all-extras"],
            state="provisioned",
        )
        client = self._client(tmp_path, repo)
        body = client.get("/api/projects/proj/runs/r1/snapshot").json()
        group = next(g for g in body["groups"] if g["group_id"] == "g1")
        assert group["provisioning"]["state"] == "provisioned"
        assert group["provisioning"]["command"] == ["uv", "sync", "--all-extras"]
        assert group["provisioning"]["worktree"].endswith("g1-fix-auth")
        assert group["provisioning"]["at"] is not None

    def test_a_group_with_no_provisioning_record_serves_null_not_an_error(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        run_dir = repo / ".orchestrator" / "runs" / "r1"
        (run_dir / "groups" / "g1").mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps({"run_id": "r1", "groups": {"g1": {"state": "pending"}}})
        )
        client = self._client(tmp_path, repo)
        body = client.get("/api/projects/proj/runs/r1/snapshot").json()
        group = next(g for g in body["groups"] if g["group_id"] == "g1")
        assert group["provisioning"] is None

    def test_the_provisioning_record_survives_worktree_teardown_in_the_snapshot(self, tmp_path):
        """The precise scenario U32 names: a group merges cleanly, its worktree
        is removed, and the drill-in must still show how it was provisioned."""
        repo = tmp_path / "proj"
        repo.mkdir()
        run_dir = repo / ".orchestrator" / "runs" / "r1"
        group_dir = run_dir / "groups" / "g1"
        group_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps({"run_id": "r1", "groups": {"g1": {"state": "completed"}}})
        )
        gone_worktree = repo / ".worktrees" / "r1" / "g1-fix-auth"
        write_provisioning_record(
            group_dir, worktree=gone_worktree, command=["uv", "sync"], state="provisioned"
        )
        # the worktree itself never existed on disk here — same observable state
        # as one that existed and was later torn down by remove_worktree.
        assert not gone_worktree.exists()

        client = self._client(tmp_path, repo)
        body = client.get("/api/projects/proj/runs/r1/snapshot").json()
        group = next(g for g in body["groups"] if g["group_id"] == "g1")
        assert group["provisioning"]["worktree"] == str(gone_worktree)
        assert group["provisioning"]["state"] == "provisioned"
