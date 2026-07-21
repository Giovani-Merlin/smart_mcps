"""U9 tests: flag > config-file > default precedence, actionable errors, status.

The full run/resume flow is exercised end-to-end in test_e2e_stub.py; this file
covers the config resolution seam and every early-exit path that must fail with
an actionable message before any session is launched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from orchestrator.cli import apply_overrides, main
from orchestrator.config import load_config
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.grouping.pipeline import serialize_grouping
from orchestrator.model import (
    EscalationRequest,
    EscalationResponse,
    Group,
    GroupingResult,
    GroupManifestEntry,
    HumanAction,
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


class TestEscalationOverrides:
    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            sequential=False,
            concurrency=None,
            permission_mode=None,
            token_budget=None,
            hitl=False,
            intensity=None,
            escalation_source=None,
            escalation_timeout=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_default_is_disabled_autonomous(self):
        merged = apply_overrides(load_config(None), self._args())
        assert merged.escalation.enabled is False
        assert merged.escalation.intensity == "on_stuck"

    def test_hitl_flag_enables_with_default_tier(self):
        merged = apply_overrides(load_config(None), self._args(hitl=True))
        assert merged.escalation.enabled is True
        assert merged.escalation.intensity == "on_stuck"

    def test_intensity_flag_implies_enabled(self):
        merged = apply_overrides(load_config(None), self._args(intensity="interactive"))
        assert merged.escalation.enabled is True
        assert merged.escalation.intensity == "interactive"

    def test_autonomous_intensity_forces_off_even_over_config_file(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[escalation]\nenabled = true\nintensity = "on_stuck"\n')
        merged = apply_overrides(load_config(config_file), self._args(intensity="autonomous"))
        assert merged.escalation.enabled is False
        assert merged.escalation.intensity == "autonomous"

    def test_source_and_timeout_flags_layer_in(self):
        merged = apply_overrides(
            load_config(None),
            self._args(hitl=True, escalation_source="orchestrator_only", escalation_timeout=45.0),
        )
        assert merged.escalation.source == "orchestrator_only"
        assert merged.escalation.timeout_s == 45.0

    def test_absent_flags_keep_config_file_escalation(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[escalation]\nenabled = true\nintensity = "on_failure"\n')
        merged = apply_overrides(load_config(config_file), self._args())
        assert merged.escalation.enabled is True
        assert merged.escalation.intensity == "on_failure"


class TestAnswerCommand:
    def _write_request(self, tmp_path, esc_id="e1", kind="coder_question"):
        paths = RunPaths(tmp_path, "r1")
        request = EscalationRequest(
            id=esc_id, run_id="r1", group_id="g1", generation=1, kind=kind, prompt="decide"
        )
        atomic_write_text(
            paths.escalations_dir / f"request-{esc_id}.json", request.model_dump_json()
        )
        return paths

    def test_answer_writes_a_response_file(self, tmp_path, capsys):
        paths = self._write_request(tmp_path)
        exit_code = main(
            [
                "answer",
                "r1",
                "e1",
                "--action",
                "answer",
                "--text",
                "use JWT",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 0
        response_path = paths.escalations_dir / "response-e1.json"
        assert response_path.is_file()
        response = EscalationResponse.model_validate_json(response_path.read_text())
        assert response.action == HumanAction.ANSWER and response.answer == "use JWT"
        assert "answered e1" in capsys.readouterr().out

    def test_answer_unknown_escalation_is_actionable(self, tmp_path, capsys):
        exit_code = main(["answer", "r1", "nope", "--repo", str(tmp_path)])
        assert exit_code == 1
        assert "no escalation nope" in capsys.readouterr().err

    def test_skip_and_abort_actions_round_trip(self, tmp_path):
        paths = self._write_request(tmp_path, esc_id="e2", kind="reviewer_too_hard")
        assert main(["answer", "r1", "e2", "--action", "skip", "--repo", str(tmp_path)]) == 0
        response = EscalationResponse.model_validate_json(
            (paths.escalations_dir / "response-e2.json").read_text()
        )
        assert response.action == HumanAction.SKIP

    def test_answer_rejects_an_already_answered_escalation(self, tmp_path, capsys):
        """The stale check `_cmd_answer` gained by delegating to answer_escalation:
        a second answer must not race the waiting group against a new decision."""
        paths = self._write_request(tmp_path, esc_id="e3")
        assert main(["answer", "r1", "e3", "--text", "first", "--repo", str(tmp_path)]) == 0
        response_path = paths.escalations_dir / "response-e3.json"
        first = response_path.read_bytes()
        capsys.readouterr()

        exit_code = main(
            ["answer", "r1", "e3", "--action", "skip", "--text", "second", "--repo", str(tmp_path)]
        )
        assert exit_code == 1
        assert "already answered" in capsys.readouterr().err
        assert response_path.read_bytes() == first

    def test_status_lists_pending_escalations(self, tmp_path, capsys):
        paths = self._write_request(tmp_path, esc_id="e9", kind="merge_conflict")
        atomic_write_text(paths.state_path, RunState(run_id="r1", groups={}).model_dump_json())
        exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "pending escalations" in out
        assert "e9" in out and "merge_conflict" in out


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


class TestUiCommand:
    """`ui` only has to parse here — serving is exercised through create_app in
    tests/test_observatory_api.py, which needs no running uvicorn."""

    def test_ui_help_lists_the_operator_flags(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            main(["ui", "--help"])
        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert "--registry" in out
        assert "--port" in out
        assert "--repo" in out

    def test_ui_defaults_match_the_documented_local_tool(self):
        from orchestrator.cli import DEFAULT_REGISTRY_PATH, DEFAULT_UI_PORT, OBSERVATORY_HOST

        assert DEFAULT_REGISTRY_PATH == "~/.orchestrator-ui.yaml"
        assert DEFAULT_UI_PORT == 8765
        assert OBSERVATORY_HOST == "127.0.0.1"
