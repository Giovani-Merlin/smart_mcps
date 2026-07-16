"""U9 tests: flag > config-file > default precedence, actionable errors, status.

The full run/resume flow is exercised end-to-end in test_e2e_stub.py; this file
covers the config resolution seam and every early-exit path that must fail with
an actionable message before any session is launched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from orchestrator.cli import apply_overrides, main
from orchestrator.config import load_config
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.grouping.pipeline import serialize_grouping
from orchestrator.model import (
    Group,
    GroupingResult,
    GroupManifestEntry,
    ReviewIntensity,
    RunManifest,
    SessionEntry,
    SessionRole,
)

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


def make_group(gid: str = "g1", **overrides) -> Group:
    defaults = dict(
        id=gid,
        name=f"group {gid}",
        summary=f"Summary {gid}",
        spec=f"Spec {gid}",
        difficulty=0.4,
        intensity=ReviewIntensity.PAIRED,
    )
    defaults.update(overrides)
    return Group(**defaults)


def write_run_artifacts(repo: Path, groups: list[Group] | None = None) -> None:
    """The artifacts `group` leaves behind, which `run`/`resume` consume."""
    orch = repo / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    (repo / "plan.md").write_text("# toy plan\n\n- T1: do the thing\n")
    result = GroupingResult(plan_path="plan.md", groups=groups or [make_group()])
    (orch / "groups.json").write_text(serialize_grouping(result))
    (orch / "base-context.md").write_text("shared base context\n")


class TestPrecedence:
    def test_config_file_beats_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[execution]\nconcurrency = 5\n[estimator]\ntoken_budget = 50000\n")
        loaded = load_config(config_file)
        assert loaded.execution.concurrency == 5
        assert loaded.estimator.token_budget == 50_000
        assert loaded.execution.permission_mode == "acceptEdits"  # untouched default

    def test_flags_beat_config_file(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[execution]\nconcurrency = 5\npermission_mode = "plan"\n'
            "[estimator]\ntoken_budget = 50000\n"
        )
        args = argparse.Namespace(
            sequential=True, concurrency=7, permission_mode="bypassPermissions", token_budget=25_000
        )
        merged = apply_overrides(load_config(config_file), args)
        assert merged.execution.concurrency == 7
        assert merged.execution.sequential is True
        assert merged.execution.permission_mode == "bypassPermissions"
        assert merged.estimator.token_budget == 25_000
        assert merged.execution.max_rewrites == 2  # untouched sections keep defaults

    def test_absent_flags_keep_config_file_values(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[execution]\nconcurrency = 5\nsequential = true\n")
        args = argparse.Namespace(
            sequential=False, concurrency=None, permission_mode=None, token_budget=None
        )
        merged = apply_overrides(load_config(config_file), args)
        assert merged.execution.concurrency == 5
        assert merged.execution.sequential is True  # store_true absence never un-sets the file


class TestRunEarlyExits:
    def test_run_without_group_artifacts_is_actionable(self, tmp_path, capsys):
        exit_code = main(["run", "--repo", str(tmp_path)])
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "smart-mcps-orchestrate group" in err

    def test_run_with_missing_plan_document_is_actionable(self, tmp_path, capsys):
        write_run_artifacts(tmp_path)
        (tmp_path / "plan.md").unlink()
        exit_code = main(["run", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "plan document" in capsys.readouterr().err

    def test_resume_unknown_run_id_is_actionable(self, tmp_path, capsys):
        write_run_artifacts(tmp_path)
        exit_code = main(["resume", "r-nope", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "no run state" in capsys.readouterr().err

    def test_run_refuses_to_overwrite_an_existing_run(self, tmp_path, capsys):
        write_run_artifacts(tmp_path)
        paths = RunPaths(tmp_path, "r1")
        atomic_write_text(paths.state_path, RunState(run_id="r1", groups={}).model_dump_json())
        exit_code = main(["run", "--repo", str(tmp_path), "--run-id", "r1"])
        assert exit_code == 1
        assert "already exists" in capsys.readouterr().err

    def test_preflight_failure_names_the_missing_flag(self, tmp_path, capsys, monkeypatch):
        write_run_artifacts(tmp_path)
        (tmp_path / ".orchestrator" / "config.toml").write_text(
            f'[session]\nclaude_bin = ["{sys.executable}", "{FAKE_CLAUDE}"]\n'
        )
        monkeypatch.setenv("FAKE_CLAUDE_HOME", str(tmp_path / "fake-home"))
        monkeypatch.setenv("FAKE_CLAUDE_HIDE_FLAGS", "--fork-session")
        exit_code = main(["run", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "--fork-session" in capsys.readouterr().err
        # preflight failed before any run directory was created
        assert not (tmp_path / ".orchestrator" / "runs").exists()


class TestStatus:
    def test_no_runs_yet(self, tmp_path, capsys):
        exit_code = main(["status", "--repo", str(tmp_path)])
        assert exit_code == 0
        assert "no runs" in capsys.readouterr().out

    def test_unknown_run_id_fails(self, tmp_path, capsys):
        exit_code = main(["status", "r-nope", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "no run state" in capsys.readouterr().err

    def test_pretty_prints_state_and_manifest(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        state = RunState(
            run_id="r1",
            groups={
                "g1": GroupRunState(state=GroupState.COMPLETED),
                "g2": GroupRunState(state=GroupState.FAILED, generation=2, failure="rewrite cap"),
            },
        )
        atomic_write_text(paths.state_path, state.model_dump_json())
        manifest = RunManifest(run_id="r1", plan_path="plan.md", base_session_id="base-sid")
        manifest.groups["g1"] = GroupManifestEntry(
            group_id="g1",
            group_name="group g1",
            summary="Summary g1",
            sessions=[SessionEntry(session_id="s1", role=SessionRole.CODER, name="r1-g1-coder-g1")],
        )
        ManifestStore(paths).save(manifest)

        exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "base-sid" in out
        assert "g1: completed" in out
        assert "g2: failed" in out and "rewrite cap" in out
        assert "r1-g1-coder-g1" in out

    def test_lists_known_runs_without_run_id(self, tmp_path, capsys):
        for run_id in ("r1", "r2"):
            paths = RunPaths(tmp_path, run_id)
            atomic_write_text(
                paths.state_path, RunState(run_id=run_id, groups={}).model_dump_json()
            )
        exit_code = main(["status", "--repo", str(tmp_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "r1" in out and "r2" in out
