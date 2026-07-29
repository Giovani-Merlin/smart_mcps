"""U9 tests: flag > config-file > default precedence, actionable errors, status.

The full run/resume flow is exercised end-to-end in test_e2e_stub.py; this file
covers the config resolution seam and every early-exit path that must fail with
an actionable message before any session is launched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from orchestrator.cli import _print_outcomes, apply_overrides, main
from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.grouping.graphing import CodegraphClient
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


def write_run_artifacts(repo: Path, groups: list[Group] | None = None, name: str = "plan") -> None:
    """The named-grouping-directory artifacts `group` leaves behind (plan U10),
    which `run`/`resume` consume. ``name="plan"`` mirrors the real CLI default
    (the plan filename stem), so tests relying on auto-selection of the sole
    grouping keep working unchanged."""
    grouping_dir = repo / ".orchestrator" / "groupings" / name
    grouping_dir.mkdir(parents=True, exist_ok=True)
    (repo / "plan.md").write_text("# toy plan\n\n- T1: do the thing\n")
    result = GroupingResult(plan_path="plan.md", groups=groups or [make_group()])
    (grouping_dir / "groups.json").write_text(serialize_grouping(result))
    (grouping_dir / "base-context.md").write_text("shared base context\n")


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

    def test_deprecated_session_timeout_warns_to_stderr_but_loads(self, tmp_path, capsys):
        """R7: pydantic v2 drops unknown keys silently, so the removed per-round
        timeout is detected in the raw TOML and warned about — never an error."""
        config_file = tmp_path / "config.toml"
        config_file.write_text("[session]\ntimeout_s = 900.0\n[execution]\nconcurrency = 2\n")
        loaded = load_config(config_file)
        assert loaded.execution.concurrency == 2  # the rest of the config still applies
        err = capsys.readouterr().err
        assert "timeout_s" in err and "deprecated" in err

    def test_config_without_the_deprecated_key_warns_nothing(self, tmp_path, capsys):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[session]\nclaude_bin = "claude"\n[escalation]\ntimeout_s = 30.0\n')
        loaded = load_config(config_file)
        assert loaded.escalation.timeout_s == 30.0  # escalation-wait timeout is untouched
        assert capsys.readouterr().err == ""


class TestAllowOversizedSliceConfig:
    """Plan U6: --allow-oversized-slice and [partition] allow_oversized_slice
    must be exactly equivalent — neither is a stronger or weaker form of the
    other, they are the same override reached two ways."""

    def test_config_file_sets_it(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[partition]\nallow_oversized_slice = true\n")
        loaded = load_config(config_file)
        assert loaded.partition.allow_oversized_slice is True

    def test_default_is_false(self):
        assert OrchestratorConfig().partition.allow_oversized_slice is False

    def test_flag_sets_it_with_no_config_file(self):
        args = argparse.Namespace(allow_oversized_slice=True)
        merged = apply_overrides(OrchestratorConfig(), args)
        assert merged.partition.allow_oversized_slice is True

    def test_absent_flag_leaves_it_false(self):
        args = argparse.Namespace(allow_oversized_slice=False)
        merged = apply_overrides(OrchestratorConfig(), args)
        assert merged.partition.allow_oversized_slice is False

    def test_flag_and_config_file_reach_the_same_effective_config(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[partition]\nallow_oversized_slice = true\n")
        via_config_file = load_config(config_file)
        via_flag = apply_overrides(
            OrchestratorConfig(), argparse.Namespace(allow_oversized_slice=True)
        )
        assert via_config_file.partition.allow_oversized_slice is True
        assert via_flag.partition.allow_oversized_slice is True


def _stub_codegraph_runner(args):
    if args[0] == "sync":
        return ""
    if args[0] == "files":
        return "stub repo\n"
    raise AssertionError(f"unexpected codegraph call in a fixture test: {args}")


OVERSIZED_SLICE_PLAN = """# feat: oversized slice

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: reports-api
    description: reporting API routes
    slice: reports
    files: [app/reports.py]
  - task_id: reports-ui
    description: reporting admin page
    slice: reports
    files: [web/reports.tsx]
```
"""


class TestSliceOverflowGateCli:
    """Plan U6, CLI surface: `group --no-spec` is the zero-LLM path, so it
    exercises the gate end-to-end without needing a speccer stub."""

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(OVERSIZED_SLICE_PLAN)
        config_dir = repo / ".orchestrator"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("[estimator]\ntoken_budget = 6000\n")
        return repo, plan

    def test_no_spec_without_override_exits_nonzero_naming_everything(self, tmp_path, capsys):
        repo, plan = self._repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code != 0
        err = capsys.readouterr().err
        assert "reports" in err
        assert "reports-api" in err
        assert "reports-ui" in err
        assert "cap" in err
        # A rejected grouping must leave nothing usable behind. Originally this
        # asserted the groupings directory did not exist at all, but g7's failure
        # trace (_write_failure_trace) deliberately persists grouping-trace.json so
        # a rejected run can be debugged. The invariant that actually matters is
        # that no *grouping* results — describe_groupings skips any directory
        # without groups.json, so a trace-only directory is never selectable by
        # `run --grouping`.
        assert not list((repo / ".orchestrator" / "groupings").rglob("groups.json"))

    def test_no_spec_with_override_exits_zero_and_keeps_slice_whole(self, tmp_path, capsys):
        repo, plan = self._repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec", "--allow-oversized-slice"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "reports-api" in out
        assert "reports-ui" in out


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


class TestGroupingSelection:
    """Plan U10: `run` never guesses between ambiguous or legacy grouping state."""

    def test_ambiguous_groupings_lists_both_names_and_plans(self, tmp_path, capsys):
        write_run_artifacts(tmp_path, name="alpha")
        write_run_artifacts(tmp_path, name="beta")
        exit_code = main(["run", "--repo", str(tmp_path)])
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "alpha" in err and "beta" in err
        assert "plan.md" in err
        assert "--grouping" in err

    def test_one_of_several_groupings_selects_via_flag(self, tmp_path, capsys, monkeypatch):
        write_run_artifacts(tmp_path, [make_group("g1")], name="alpha")
        write_run_artifacts(tmp_path, [make_group("g9")], name="beta")
        (tmp_path / ".orchestrator" / "config.toml").write_text(
            f'[session]\nclaude_bin = ["{sys.executable}", "{FAKE_CLAUDE}"]\n'
        )
        monkeypatch.setenv("FAKE_CLAUDE_HOME", str(tmp_path / "fake-home"))
        monkeypatch.setenv("FAKE_CLAUDE_HIDE_FLAGS", "--fork-session")
        exit_code = main(["run", "--repo", str(tmp_path), "--grouping", "beta"])
        # picks "beta" successfully and proceeds all the way to preflight,
        # which fails for an unrelated, already-covered reason
        assert exit_code == 1
        assert "--fork-session" in capsys.readouterr().err

    def test_unknown_grouping_name_is_actionable(self, tmp_path, capsys):
        write_run_artifacts(tmp_path, name="alpha")
        exit_code = main(["run", "--repo", str(tmp_path), "--grouping", "nope"])
        assert exit_code == 1
        assert "no grouping named 'nope'" in capsys.readouterr().err

    def test_legacy_top_level_artifact_is_reported_not_consumed(self, tmp_path, capsys):
        (tmp_path / ".orchestrator").mkdir(parents=True)
        (tmp_path / "plan.md").write_text("# toy plan\n")
        legacy_result = GroupingResult(plan_path="plan.md", groups=[make_group()])
        legacy_path = tmp_path / ".orchestrator" / "groups.json"
        legacy_path.write_text(serialize_grouping(legacy_result))
        (tmp_path / ".orchestrator" / "base-context.md").write_text("ctx\n")

        exit_code = main(["run", "--repo", str(tmp_path)])
        assert exit_code == 1
        err = capsys.readouterr().err
        assert str(legacy_path) in err
        assert "--name" in err  # names the re-group command
        # never consumed: the legacy file is untouched and no run started
        assert legacy_path.is_file()
        assert not (tmp_path / ".orchestrator" / "runs").exists()

    def test_groupings_subcommand_lists_name_plan_and_count(self, tmp_path, capsys):
        write_run_artifacts(tmp_path, [make_group("g1"), make_group("g2")], name="alpha")
        exit_code = main(["groupings", "--repo", str(tmp_path)])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "plan.md" in out
        assert "2 group(s)" in out

    def test_groupings_subcommand_empty(self, tmp_path, capsys):
        exit_code = main(["groupings", "--repo", str(tmp_path)])
        assert exit_code == 0
        assert "no groupings found" in capsys.readouterr().out


class TestPrintOutcomes:
    """Exit-code contract (R3): 0 = complete, 1 = needs-inspection work failure,
    2 = stopped-but-resumable (interrupted, mirroring operator abort)."""

    def test_all_completed_exits_zero(self, capsys):
        state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.COMPLETED)})
        assert _print_outcomes(state) == 0
        assert "all groups completed" in capsys.readouterr().out

    def test_interrupted_groups_exit_two_naming_them_and_the_resume_command(self, capsys):
        state = RunState(
            run_id="r7",
            groups={
                "g1": GroupRunState(
                    state=GroupState.INTERRUPTED, failure="SessionError: claude exited 1"
                ),
                "g2": GroupRunState(state=GroupState.PENDING),
                "g3": GroupRunState(
                    state=GroupState.INTERRUPTED, failure="SessionError: usage limit reached"
                ),
            },
        )
        assert _print_outcomes(state) == 2
        err = capsys.readouterr().err
        assert "g1" in err and "g3" in err
        assert "smart-mcps-orchestrate resume r7" in err

    def test_only_work_failures_keep_exit_one_and_todays_message(self, capsys):
        state = RunState(
            run_id="r1",
            groups={
                "g1": GroupRunState(state=GroupState.FAILED, failure="GroupFailure: blocked"),
                "g2": GroupRunState(state=GroupState.PENDING),
            },
        )
        assert _print_outcomes(state) == 1
        err = capsys.readouterr().err
        assert "did not complete" in err
        assert "resume r1" not in err  # the resume-command line is interrupted-only

    def test_mixed_interrupted_and_failed_still_exits_two(self, capsys):
        state = RunState(
            run_id="r9",
            groups={
                "g1": GroupRunState(state=GroupState.FAILED, failure="GroupFailure: blocked"),
                "g2": GroupRunState(state=GroupState.INTERRUPTED, failure="SessionError: x"),
            },
        )
        assert _print_outcomes(state) == 2
        assert "smart-mcps-orchestrate resume r9" in capsys.readouterr().err


class TestRunBanner:
    """R8: the effective execution config prints before any session spawns."""

    def _setup(self, tmp_path, monkeypatch, *, failures: int) -> Path:
        write_run_artifacts(tmp_path, [make_group("g1"), make_group("g2")])
        for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "-m", "init"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=tmp_path,
                check=True,
                capture_output=True,
            )
        fake_home = tmp_path / "fake-home"
        (fake_home / "sessions").mkdir(parents=True)
        (tmp_path / ".orchestrator" / "config.toml").write_text(
            f'[session]\nclaude_bin = ["{sys.executable}", "{FAKE_CLAUDE}"]\n'
        )
        monkeypatch.setenv("FAKE_CLAUDE_HOME", str(fake_home))
        monkeypatch.delenv("FAKE_CLAUDE_HIDE_FLAGS", raising=False)
        # Script every base-session attempt to die at spawn: under the old
        # post-spawn print, this path produced no banner at all — the banner
        # appearing despite the failed spawn pins the output order.
        (fake_home / "script.jsonl").write_text(
            (json.dumps({"exit_code": 1, "stderr": "spawn died"}) + "\n") * failures
        )
        return fake_home

    def _base_calls(self, fake_home: Path) -> list[dict]:
        path = fake_home / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_banner_precedes_the_base_session_spawn(self, tmp_path, capsys, monkeypatch):
        fake_home = self._setup(tmp_path, monkeypatch, failures=1)
        exit_code = main(
            ["run", "--repo", str(tmp_path), "--run-id", "r9", "--sequential", "--hitl"]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "base session failed" in captured.err
        # the spawn was attempted (exactly one CLI call: the base session)...
        calls = self._base_calls(fake_home)
        assert len(calls) == 1
        argv = calls[0]["argv"]
        assert argv[argv.index("--name") + 1] == "r9-base"
        # ...and the banner still made it out first, naming every R8 item
        banner = captured.out.splitlines()[0]
        assert "run r9" in banner
        assert "2 group(s)" in banner
        assert "sequential" in banner
        assert "HITL on (intensity=on_stuck, source=workers_via_orchestrator)" in banner
        assert "permission-mode acceptEdits" in banner

    def test_banner_names_concurrency_and_disabled_hitl(self, tmp_path, capsys, monkeypatch):
        self._setup(tmp_path, monkeypatch, failures=1)
        exit_code = main(["run", "--repo", str(tmp_path), "--run-id", "r10", "--concurrency", "4"])
        assert exit_code == 1
        banner = capsys.readouterr().out.splitlines()[0]
        assert "run r10" in banner
        assert "concurrency 4" in banner
        assert "HITL off" in banner


class TestWorkspaceProvisioning:
    def test_workspace_for_provisions_the_env_after_creating_the_worktree(
        self, tmp_path, monkeypatch
    ):
        """U6/R16: the provisioning hook fires from the workspace seam, on the
        already-created worktree — create_worktree itself stays pure git."""
        from orchestrator.cli import _workspace_seams
        from orchestrator.execution.merge import IntegrationMerger

        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (["init", "-b", "main"], ["add", "-A"], ["commit", "--allow-empty", "-m", "i"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=repo,
                check=True,
                capture_output=True,
            )
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        recorded: list[tuple[Path, bool]] = []
        monkeypatch.setattr(
            "orchestrator.cli.provision_env",
            lambda path, **kwargs: recorded.append((path, path.is_dir())),
        )
        workspace_for, base_ref_for = _workspace_seams(repo, "r1", merger, RunPaths(repo, "r1"))
        group = make_group("g1")
        path = workspace_for(group)
        assert recorded == [(path, True)]  # invoked once, after the worktree existed
        assert base_ref_for(group)  # the shared tip capture still works


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
