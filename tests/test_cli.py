"""U9 tests: flag > config-file > default precedence, actionable errors, status.

The full run/resume flow is exercised end-to-end in test_e2e_stub.py; this file
covers the config resolution seam and every early-exit path that must fail with
an actionable message before any session is launched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.cli import (
    _config_banner_source,
    _load_config,
    _print_outcomes,
    apply_overrides,
    main,
)
from orchestrator.config import (
    EscalationConfig,
    OrchestratorConfig,
    SessionConfig,
    load_config,
)
from orchestrator.execution.driver import STALE_HEARTBEAT_SECONDS, DriverLock
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.scheduler import (
    GroupHold,
    GroupRunState,
    GroupState,
    HoldReason,
    RunState,
)
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

    def test_config_file_overrides_max_conflict_resolve_attempts(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[execution]\nmax_conflict_resolve_attempts = 0\n")
        loaded = load_config(config_file)
        assert loaded.execution.max_conflict_resolve_attempts == 0
        assert loaded.execution.max_rewrites == 2  # untouched default

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


class TestGranularityConfigAndFlag:
    """Plan U4: --granularity and [partition] granularity must be exactly
    equivalent, and the flag wins when both are set."""

    def test_default_is_independent(self):
        assert OrchestratorConfig().partition.granularity == "independent"

    def test_config_file_sets_it(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[partition]\ngranularity = "balanced"\n')
        assert load_config(config_file).partition.granularity == "balanced"

    def test_flag_sets_it_with_no_config_file(self):
        args = argparse.Namespace(granularity="monolithic")
        merged = apply_overrides(OrchestratorConfig(), args)
        assert merged.partition.granularity == "monolithic"

    def test_absent_flag_leaves_the_config_file_value(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[partition]\ngranularity = "balanced"\n')
        loaded = load_config(config_file)
        merged = apply_overrides(loaded, argparse.Namespace(granularity=None))
        assert merged.partition.granularity == "balanced"

    def test_flag_wins_over_config_file(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[partition]\ngranularity = "balanced"\n')
        loaded = load_config(config_file)
        merged = apply_overrides(loaded, argparse.Namespace(granularity="monolithic"))
        assert merged.partition.granularity == "monolithic"

    def test_invalid_value_in_config_file_rejected(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[partition]\ngranularity = "extreme"\n')
        with pytest.raises(Exception):
            load_config(config_file)


GRANULARITY_LADDER_PLAN = """# feat: granularity ladder

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: root
    description: root
    files: [app/root.py]
  - task_id: alpha1
    description: alpha1
    files: [app/alpha1.py]
    depends_on: [root]
  - task_id: alpha2
    description: alpha2
    files: [app/alpha2.py]
    depends_on: [alpha1]
  - task_id: beta1
    description: beta1
    files: [app/beta1.py]
    depends_on: [root]
  - task_id: beta2
    description: beta2
    files: [app/beta2.py]
    depends_on: [beta1]
  - task_id: beta3
    description: beta3
    files: [app/beta3.py]
    depends_on: [beta2]
  - task_id: gamma1
    description: gamma1
    files: [app/gamma1.py]
    depends_on: [root]
  - task_id: leaf
    description: leaf
    files: [app/leaf.py]
    depends_on: [alpha2, beta3, gamma1]
```
"""


class TestGranularityCliFlag:
    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(GRANULARITY_LADDER_PLAN)
        return repo, plan

    def test_accepts_the_three_named_levels(self, tmp_path):
        repo, plan = self._repo(tmp_path)
        for level in ("independent", "balanced", "monolithic"):
            exit_code = main(
                ["group", str(plan), "--repo", str(repo), "--no-spec", "--granularity", level],
                client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
            )
            assert exit_code == 0

    def test_rejects_an_unknown_value(self, tmp_path, capsys):
        repo, plan = self._repo(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            main(["group", str(plan), "--repo", str(repo), "--granularity", "bogus"])
        assert excinfo.value.code != 0
        assert "invalid choice" in capsys.readouterr().err


class TestScorecardAndMetricsLogCli:
    """Plan U5: `group --no-spec` prints the scorecard, the printed values
    equal what's recorded in grouping-trace.json, and one line is appended to
    .orchestrator/grouping-metrics.jsonl per invocation that produces a
    partition — never on a rejected grouping."""

    def _repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(GRANULARITY_LADDER_PLAN)
        return repo, plan

    def test_no_spec_prints_the_scorecard(self, tmp_path, capsys):
        repo, plan = self._repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "scorecard:" in out
        assert "cross-group edges:" in out
        assert "work fraction of cap" in out
        assert "critical path length:" in out
        assert "modularity:" in out
        assert "slice integrity:" in out

    def test_printed_scorecard_matches_the_trace(self, tmp_path, capsys):
        from orchestrator.grouping.trace import GroupingTrace

        repo, plan = self._repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        trace_path = (
            repo / ".orchestrator" / "groupings" / "plan" / "preview" / "grouping-trace.json"
        )
        trace = GroupingTrace.model_validate_json(trace_path.read_text())
        sc = trace.scorecard
        assert sc is not None
        assert f"groups: {sc.group_count}" in out
        assert f"cross-group edges: {sc.cross_group_edges}" in out
        assert f"critical path length: {sc.critical_path_length}" in out
        assert f"modularity: {sc.modularity:.3f}" in out

    def test_metrics_log_appends_one_line_per_invocation(self, tmp_path):
        repo, plan = self._repo(tmp_path)
        metrics_path = repo / ".orchestrator" / "grouping-metrics.jsonl"

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        lines = metrics_path.read_text().splitlines()
        assert len(lines) == 1

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        lines = metrics_path.read_text().splitlines()
        assert len(lines) == 2

    def test_every_metrics_line_parses_and_carries_scorecard_and_provenance(self, tmp_path):
        repo, plan = self._repo(tmp_path)
        metrics_path = repo / ".orchestrator" / "grouping-metrics.jsonl"
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        for line in metrics_path.read_text().splitlines():
            record = json.loads(line)
            assert "scorecard" in record and record["scorecard"] is not None
            assert "provenance" in record and record["provenance"] is not None

    def test_a_grouping_that_fails_appends_no_metrics_line(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(OVERSIZED_SLICE_PLAN)
        config_dir = repo / ".orchestrator"
        config_dir.mkdir()
        (config_dir / "config.toml").write_text("[estimator]\ntoken_budget = 6000\n")
        metrics_path = repo / ".orchestrator" / "grouping-metrics.jsonl"

        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code != 0
        assert not metrics_path.exists()


def _stub_codegraph_runner(args):
    if args[0] == "sync":
        return ""
    if args[0] == "files":
        return "stub repo\n"
    if args[0] == "status":
        return json.dumps(
            {
                "initialized": True,
                "fileCount": 1,
                "nodeCount": 1,
                "edgeCount": 0,
                "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            }
        )
    if args[0] == "query":
        return "[]"
    raise AssertionError(f"unexpected codegraph call in a fixture test: {args}")


class TestGroupStageProgressUnbuffered:
    """Plan U24: the whole failure mode being fixed is that a `group` job shows
    nothing for minutes. Verified against a real, separate OS process writing to
    a real log file — a captured `capsys` run proves the lines exist, not that
    they arrived while the command was still running."""

    def test_progress_lines_land_in_the_log_while_the_process_is_still_running(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "server.py").write_bytes(b"def real_fn():\n    pass\n" * 20)
        (repo / "test_server.py").write_bytes(b"def test_real_fn():\n    pass\n" * 10)
        plan = repo / "plan.md"
        plan.write_text(
            "# feat: toy plan\n\n## Tasks\n\n"
            "- T1: extend the proxy server tool list\n"
            "- T2: cover the proxy with tests\n"
        )
        script = tmp_path / "run_group.py"
        script.write_text(
            "import json, sys, time\n"
            "from pathlib import Path\n"
            "from orchestrator.cli import main\n"
            "from orchestrator.grouping.graphing import CodegraphClient\n"
            "\n"
            "def codegraph_response(args):\n"
            "    command = args[0]\n"
            "    if command == 'sync':\n"
            "        return ''\n"
            "    if command == 'files':\n"
            "        return 'repo files: server.py, test_server.py'\n"
            "    if command == 'status':\n"
            "        return json.dumps({'initialized': True, 'fileCount': 2, 'nodeCount': 4,\n"
            "            'edgeCount': 1,\n"
            "            'pendingChanges': {'added': 0, 'modified': 0, 'removed': 0}})\n"
            "    symbol = args[1]\n"
            "    if command == 'query':\n"
            "        if symbol in ('real_fn', 'test_real_fn'):\n"
            "            return json.dumps([{'node': {'name': symbol, 'filePath': 'server.py'}}])\n"
            "        return json.dumps([])\n"
            "    if command == 'callers' and symbol == 'real_fn':\n"
            "        return json.dumps({'symbol': symbol, 'callers': [\n"
            "            {'name': 'test_real_fn', 'kind': 'function', 'filePath': 'test_server.py'}]})\n"
            "    key = {'callers': 'callers', 'callees': 'callees', 'impact': 'affected'}[command]\n"
            "    return json.dumps({'symbol': symbol, key: []})\n"
            "\n"
            "def llm_runner(prompt, schema):\n"
            "    # Deliberately slow, so the parent test can observe the first\n"
            "    # progress line while this child process is still alive.\n"
            "    time.sleep(1.5)\n"
            "    return json.dumps({'tasks': [\n"
            "        {'task_id': 't1', 'description': 'd1', 'files': ['server.py'],\n"
            "         'symbols': ['real_fn']},\n"
            "        {'task_id': 't2', 'description': 'd2', 'files': ['test_server.py'],\n"
            "         'symbols': ['test_real_fn']}]})\n"
            "\n"
            "repo, plan = Path(sys.argv[1]), sys.argv[2]\n"
            "exit_code = main(\n"
            "    ['group', plan, '--repo', str(repo), '--no-spec'],\n"
            "    llm_runner=llm_runner,\n"
            "    client=CodegraphClient(repo_root=repo, runner=codegraph_response),\n"
            ")\n"
            "sys.exit(exit_code)\n"
        )
        log_path = tmp_path / "job.log"
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                [sys.executable, str(script), str(repo), str(plan)],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 5
            observed_while_running = False
            while time.monotonic() < deadline:
                if "progress: stage: mapper" in log_path.read_text():
                    observed_while_running = proc.poll() is None
                    break
                time.sleep(0.05)
            assert observed_while_running, (
                "first progress line did not land in the log within 5s of "
                "process start, or only appeared after the process had exited"
            )
        finally:
            proc.wait(timeout=10)
        assert proc.returncode == 0
        text = log_path.read_text()
        assert "progress: stage: mapper" in text
        assert "progress: stage: graph" in text
        assert "progress: stage: partition" in text


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
        # F1: HITL is opt-in. The failure-overlap gate it once defended is now
        # covered mechanically by ExecutionConfig.on_group_failure="halt", so a
        # plain `run` is unattended and never blocks on an operator.
        merged = apply_overrides(load_config(None), self._args())
        assert merged.escalation.enabled is False
        assert merged.escalation.intensity == "autonomous"

    def test_hitl_flag_enables_with_default_tier(self):
        # Regression guard for the no-op: with the library tier now
        # `autonomous`, --hitl alone has to supply on_stuck too, or it would
        # enable an escalation surface that never escalates.
        merged = apply_overrides(load_config(None), self._args(hitl=True))
        assert merged.escalation.enabled is True
        assert merged.escalation.intensity == "on_stuck"

    def test_hitl_flag_does_not_clobber_a_config_file_tier(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[escalation]\nintensity = "interactive"\n')
        merged = apply_overrides(load_config(config_file), self._args(hitl=True))
        assert merged.escalation.enabled is True
        assert merged.escalation.intensity == "interactive"

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


class TestLoadConfigPersistedEscalation:
    """Plan U2: `_load_config`'s `persisted_escalation` param is the fourth rung
    (below CLI flags, above the library default) that lets `resume` restore a
    run's own recorded escalation tier."""

    def _args(self, **overrides) -> argparse.Namespace:
        base = dict(
            config=None,
            sequential=False,
            concurrency=None,
            permission_mode=None,
            token_budget=None,
            allow_oversized_slice=False,
            allow_degenerate_partition=False,
            granularity=None,
            hitl=False,
            intensity=None,
            escalation_source=None,
            escalation_timeout=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_no_persisted_value_keeps_library_default(self, tmp_path):
        config = _load_config(self._args(), tmp_path)
        assert config.escalation.intensity == "autonomous"
        assert config.escalation.enabled is False

    def test_persisted_value_replaces_the_default_when_no_flag_is_passed(self, tmp_path):
        # The persisted tier must be non-default for this to mean anything —
        # HITL now defaults off/autonomous, so a run started with --hitl is the
        # case a bare `resume` has to restore.
        persisted = EscalationConfig(enabled=True, intensity="on_stuck")
        config = _load_config(self._args(), tmp_path, persisted_escalation=persisted)
        assert config.escalation.intensity == "on_stuck"
        assert config.escalation.enabled is True

    def test_explicit_flag_still_overrides_the_persisted_value(self, tmp_path):
        # `interactive` is neither the flag's value nor the library default, so
        # the result pins the flag as the winner rather than either fallback.
        persisted = EscalationConfig(enabled=True, intensity="interactive")
        config = _load_config(
            self._args(intensity="on_stuck"), tmp_path, persisted_escalation=persisted
        )
        assert config.escalation.intensity == "on_stuck"
        assert config.escalation.enabled is True


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

    def test_retry_action_round_trips(self, tmp_path):
        paths = self._write_request(tmp_path, esc_id="e4", kind="coder_blocked")
        argv = ["answer", "r1", "e4", "--action", "retry", "--text", "env fixed"]
        assert main([*argv, "--repo", str(tmp_path)]) == 0
        raw = json.loads((paths.escalations_dir / "response-e4.json").read_text())
        assert raw["action"] == "retry" and raw["answer"] == "env fixed"
        assert EscalationResponse.model_validate(raw).action == HumanAction.RETRY

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


class TestStatusReportsHolds:
    """Plan U9: a held group says *why*, and the three reasons read differently."""

    def _status_out(self, tmp_path, capsys, holds: list[GroupHold]) -> str:
        paths = RunPaths(tmp_path, "r1")
        paths.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            paths.state_path,
            RunState(
                run_id="r1", groups={"g2": GroupRunState(state=GroupState.PENDING, holds=holds)}
            ).model_dump_json(),
        )
        assert main(["status", "r1", "--repo", str(tmp_path)]) == 0
        return capsys.readouterr().out

    def test_overlap_hold_names_the_locking_group_and_the_shared_files(self, tmp_path, capsys):
        out = self._status_out(
            tmp_path,
            capsys,
            [
                GroupHold(
                    reason=HoldReason.FILE_OVERLAP, group_id="g1", files=["cli.py", "model.py"]
                )
            ],
        )
        assert "held (file_overlap) by g1 on cli.py, model.py" in out

    def test_the_three_hold_reasons_are_distinguishable(self, tmp_path, capsys):
        out = self._status_out(
            tmp_path,
            capsys,
            [
                GroupHold(reason=HoldReason.DAG_DEPENDENCY, group_id="g0"),
                GroupHold(reason=HoldReason.FAILURE_GATE, group_id="g1", files=["cli.py"]),
                GroupHold(reason=HoldReason.FILE_OVERLAP, group_id="g3", files=["cli.py"]),
            ],
        )
        assert "held (dag_dependency) by g0" in out  # no files: not a file relation
        assert "held (failure_gate) by g1 on cli.py" in out
        assert "held (file_overlap) by g3 on cli.py" in out

    def test_an_unheld_group_prints_no_hold_line(self, tmp_path, capsys):
        assert "held (" not in self._status_out(tmp_path, capsys, [])


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

    def test_resolved_group_is_reported_distinctly_from_completed(self, capsys):
        """Plan U2: a resolved group's stranded work landed, but it never
        claimed a review verdict — the listing must not blur it into 'completed'."""
        state = RunState(
            run_id="r11",
            groups={
                "g1": GroupRunState(
                    state=GroupState.RESOLVED, failure="GroupFailure: reviewer said too_hard"
                ),
                "g2": GroupRunState(state=GroupState.COMPLETED),
            },
        )
        assert _print_outcomes(state) == 1  # not "all completed" — g1 needs inspection
        out = capsys.readouterr().out
        assert "g1: resolved" in out
        assert "g2: completed" in out
        assert "g1: completed" not in out

    def test_omitting_paths_skips_the_residue_section_unchanged(self, capsys):
        # Every test above calls _print_outcomes(state) with no paths, exactly
        # as every pre-U12 caller does — must keep behaving identically.
        state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.COMPLETED)})
        assert _print_outcomes(state) == 0
        assert "surprises pending" not in capsys.readouterr().out

    def test_residue_section_reports_a_pending_bucket_with_its_reason(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        paths.run_dir.mkdir(parents=True)
        atomic_write_text(
            paths.surprises_path,
            json.dumps(
                {"g1": [{"kind": "other", "description": "late finding", "affected_groups": []}]}
            ),
        )
        state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.COMPLETED)})
        assert _print_outcomes(state, paths) == 0
        out = capsys.readouterr().out
        assert "surprises pending at end of run" in out
        assert "g1: 1 pending" in out
        assert "already completed" in out

    def test_residue_section_prints_none_pending_for_an_empty_board(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        paths.run_dir.mkdir(parents=True)
        state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.COMPLETED)})
        assert _print_outcomes(state, paths) == 0
        out = capsys.readouterr().out
        assert "surprises pending at end of run" in out
        assert "none pending" in out

    def test_stall_report_names_failure_holds_branch_reentry_and_resume_command(self, capsys):
        """Plan U3: a stalled group's line carries its failure text verbatim,
        the groups it holds and on which files, its branch, its reentry_count,
        and the exact resume command."""
        state = RunState(
            run_id="rstall",
            groups={
                "g1": GroupRunState(
                    state=GroupState.INTERRUPTED,
                    failure="WorktreeError: refusing to overwrite",
                    reentry_count=2,
                ),
                "g2": GroupRunState(
                    state=GroupState.PENDING,
                    holds=[
                        GroupHold(reason=HoldReason.FAILURE_GATE, group_id="g1", files=["a.py"])
                    ],
                ),
            },
        )
        exit_code = _print_outcomes(state)
        assert exit_code == 2
        out = capsys.readouterr().out
        assert "WorktreeError: refusing to overwrite" in out
        assert "g2 (a.py)" in out
        assert "orchestrator/rstall-g1" in out
        assert "reentry_count 2" in out
        assert "smart-mcps-orchestrate resume rstall" in out

    def test_quarantined_group_points_at_retry_not_resume(self, capsys):
        state = RunState(
            run_id="rq",
            groups={
                "g1": GroupRunState(
                    state=GroupState.INTERRUPTED,
                    failure="RuntimeError: still broken",
                    reentry_count=4,
                    quarantined=True,
                )
            },
        )
        _print_outcomes(state)
        out = capsys.readouterr().out
        assert "[quarantined]" in out
        assert "smart-mcps-orchestrate retry --repo <repo> rq g1" in out
        assert "smart-mcps-orchestrate resume rq" not in out

    def test_report_is_read_only(self, capsys):
        """Plan U3: printing the report never mutates state.json."""
        state = RunState(
            run_id="rro",
            groups={
                "g1": GroupRunState(state=GroupState.INTERRUPTED, failure="x"),
                "g2": GroupRunState(state=GroupState.COMPLETED),
            },
        )
        before = state.model_dump_json()
        _print_outcomes(state)
        capsys.readouterr()
        assert state.model_dump_json() == before

    def test_no_interrupted_or_failed_group_prints_no_stall_section(self, capsys):
        state = RunState(
            run_id="rnone",
            groups={"g1": GroupRunState(state=GroupState.COMPLETED)},
        )
        _print_outcomes(state)
        out = capsys.readouterr().out
        assert "holds:" not in out

    def test_halted_run_names_trigger_not_admitted_and_both_ways_forward(self, capsys):
        """Plan U3/R41: a halted run's report names the triggering group, the
        groups it kept off admission, and both the fix-and-resume path and the
        --on-failure overlap escape hatch."""
        state = RunState(
            run_id="rhalt",
            groups={
                "g1": GroupRunState(state=GroupState.FAILED, failure="GroupFailure: boom"),
                "g2": GroupRunState(
                    state=GroupState.PENDING,
                    holds=[GroupHold(reason=HoldReason.RUN_HALTED, group_id="g1")],
                ),
                "g3": GroupRunState(
                    state=GroupState.PENDING,
                    holds=[GroupHold(reason=HoldReason.RUN_HALTED, group_id="g1")],
                ),
            },
        )
        _print_outcomes(state)
        out = capsys.readouterr().out
        assert "run halted: group g1 ended failed" in out
        assert "g2" in out and "g3" in out
        assert "smart-mcps-orchestrate retry --repo <repo> rhalt g1" in out
        assert "--on-failure overlap" in out

    def test_resuming_a_failed_halt_says_retry_clears_it(self, capsys):
        # A resume that re-admits nothing (the only unsuccessful group is
        # terminally FAILED) still has a pending group carrying the RUN_HALTED
        # hold from the scheduler's last admission pass before it returned.
        state = RunState(
            run_id="rretry",
            groups={
                "g1": GroupRunState(state=GroupState.FAILED, failure="GroupFailure: boom"),
                "g2": GroupRunState(
                    state=GroupState.PENDING,
                    holds=[GroupHold(reason=HoldReason.RUN_HALTED, group_id="g1")],
                ),
            },
        )
        _print_outcomes(state)
        out = capsys.readouterr().out
        assert "smart-mcps-orchestrate retry --repo <repo> rretry g1" in out


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
            f'transcript_root = "{tmp_path / "claude-home" / "projects"}"\n'
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
        # `--fork-base`: these assertions key off the run's *first* CLI spawn
        # dying, and with workers starting fresh (ADR 0007) the first spawn is
        # a coder, whose failure lands the group INTERRUPTED instead. The base
        # session is the stable single-spawn observable for banner ordering.
        exit_code = main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--run-id",
                "r9",
                "--sequential",
                "--hitl",
                "--fork-base",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "base session failed" in captured.err
        # the spawn was attempted (exactly one CLI call: the base session)...
        calls = self._base_calls(fake_home)
        assert len(calls) == 1
        argv = calls[0]["argv"]
        assert argv[argv.index("--name") + 1] == "r9-base"
        # ...and the banner still made it out before any session spawn, right
        # after the config echo, naming every R8 item
        banner = next(line for line in captured.out.splitlines() if line.startswith("run r9"))
        assert "run r9" in banner
        assert "2 group(s)" in banner
        assert "sequential" in banner
        assert "HITL on (intensity=on_stuck, source=workers_via_orchestrator)" in banner
        assert "permission-mode acceptEdits" in banner

    def test_banner_names_concurrency_and_disabled_hitl(self, tmp_path, capsys, monkeypatch):
        self._setup(tmp_path, monkeypatch, failures=1)
        # HITL is off by default (F1); this run passes no --hitl, so the
        # banner must report it disabled.
        exit_code = main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--run-id",
                "r10",
                "--concurrency",
                "4",
                "--intensity",
                "autonomous",
                "--fork-base",
            ]
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        banner = next(line for line in out.splitlines() if line.startswith("run r10"))
        assert "run r10" in banner
        assert "concurrency 4" in banner
        assert "HITL off" in banner


class TestConfigBanner:
    """F11: the `config:` line names a path only when one was actually read —
    naming a path that was never opened made two operators once believe a
    `.orchestrator/config.toml` existed when it didn't."""

    def test_no_config_file_reports_defaults(self, tmp_path):
        missing = tmp_path / ".orchestrator" / "config.toml"
        assert not missing.exists()
        assert _config_banner_source(missing) == "defaults (no config file)"

    def test_present_config_file_reports_its_path(self, tmp_path):
        present = tmp_path / ".orchestrator" / "config.toml"
        present.parent.mkdir(parents=True)
        present.write_text("[session]\n")
        assert _config_banner_source(present) == str(present)


class TestModelFlags:
    """Plan U36: --model-worker/--model-base/--model-speccer, flag > config-file
    > built-in default (same precedence as every other override in this file),
    and the resolved trio printed on the run banner."""

    def test_run_help_lists_the_three_flags(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["run", "--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--model-worker" in out
        assert "--model-base" in out
        assert "--model-speccer" in out

    def test_flags_beat_config_file(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[session]\nmodel = "config-worker"\nbase_model = "config-base"\n'
            'speccer_model = "config-speccer"\n'
        )
        args = argparse.Namespace(
            model_worker="flag-worker", model_base="flag-base", model_speccer="flag-speccer"
        )
        merged = apply_overrides(load_config(config_file), args)
        assert merged.session.model == "flag-worker"
        assert merged.session.base_model == "flag-base"
        assert merged.session.speccer_model == "flag-speccer"

    def test_absent_flags_keep_config_file_values(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[session]\nmodel = "config-worker"\nbase_model = "config-base"\n'
            'speccer_model = "config-speccer"\n'
        )
        args = argparse.Namespace(model_worker=None, model_base=None, model_speccer=None)
        merged = apply_overrides(load_config(config_file), args)
        assert merged.session.model == "config-worker"
        assert merged.session.base_model == "config-base"
        assert merged.session.speccer_model == "config-speccer"

    def test_no_flags_and_no_config_leaves_the_built_in_defaults(self):
        args = argparse.Namespace(model_worker=None, model_base=None, model_speccer=None)
        merged = apply_overrides(OrchestratorConfig(), args)
        assert merged.session.model == "claude-sonnet-5"
        assert merged.session.base_model == "claude-opus-5"
        assert merged.session.speccer_model == "claude-opus-5"

    def test_banner_prints_the_three_resolved_model_ids(self, tmp_path, capsys, monkeypatch):
        write_run_artifacts(tmp_path, [make_group("g1")])
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
            f'transcript_root = "{tmp_path / "claude-home" / "projects"}"\n'
        )
        monkeypatch.setenv("FAKE_CLAUDE_HOME", str(fake_home))
        monkeypatch.delenv("FAKE_CLAUDE_HIDE_FLAGS", raising=False)
        (fake_home / "script.jsonl").write_text(
            json.dumps({"exit_code": 1, "stderr": "spawn died"}) + "\n"
        )
        exit_code = main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--run-id",
                "r11",
                "--model-worker",
                "custom-worker-model",
                "--fork-base",
            ]
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        models_line = next(line for line in out.splitlines() if line.startswith("models:"))
        assert "worker=custom-worker-model" in models_line
        assert "base=claude-opus-5" in models_line
        assert "speccer=claude-opus-5" in models_line


class TestReviewIntensityWarning:
    """Plan U8: --review-intensity gets a warning on the effective-config line,
    naming how many groups it changes and the reviewer sessions that implies;
    omitting it warns nothing and never touches the recorded groups.json."""

    def _setup(self, tmp_path, monkeypatch) -> Path:
        write_run_artifacts(
            tmp_path,
            [
                make_group("g1", intensity=ReviewIntensity.PAIRED),
                make_group("g2", intensity=ReviewIntensity.SELF_VERIFY),
            ],
        )
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
            f'transcript_root = "{tmp_path / "claude-home" / "projects"}"\n'
        )
        monkeypatch.setenv("FAKE_CLAUDE_HOME", str(fake_home))
        monkeypatch.delenv("FAKE_CLAUDE_HIDE_FLAGS", raising=False)
        (fake_home / "script.jsonl").write_text(
            json.dumps({"exit_code": 1, "stderr": "spawn died"}) + "\n"
        )
        return fake_home

    def _groups_json(self, tmp_path) -> str:
        return (tmp_path / ".orchestrator" / "groupings" / "plan" / "groups.json").read_text()

    def test_overriding_intensity_warns_naming_groups_and_sessions(
        self, tmp_path, capsys, monkeypatch
    ):
        self._setup(tmp_path, monkeypatch)
        before = self._groups_json(tmp_path)
        exit_code = main(
            [
                "run",
                "--repo",
                str(tmp_path),
                "--run-id",
                "r12",
                "--sequential",
                "--intensity",
                "autonomous",
                "--review-intensity",
                "paired_plus",
                "--fork-base",
            ]
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        # g1 (paired → paired_plus) and g2 (self_verify → paired_plus) both change;
        # paired_plus spawns 2 reviewer sessions per group (plan _REVIEWER_SESSIONS).
        assert "overrides 2 group(s)" in out
        assert "implies 4 reviewer session(s)" in out
        # groups.json on disk is never rewritten by the override.
        assert self._groups_json(tmp_path) == before

    def test_omitting_intensity_warns_nothing_and_keeps_recorded_intensity(
        self, tmp_path, capsys, monkeypatch
    ):
        self._setup(tmp_path, monkeypatch)
        before = self._groups_json(tmp_path)
        exit_code = main(
            ["run", "--repo", str(tmp_path), "--run-id", "r13", "--sequential", "--fork-base"]
        )
        assert exit_code == 1
        out = capsys.readouterr().out
        assert "overrides" not in out
        assert "reviewer session" not in out
        assert self._groups_json(tmp_path) == before


class TestWorkspaceForFreshCut:
    def test_workspace_for_cuts_a_fresh_worktree_from_the_integration_tip(self, tmp_path):
        """Plan U1 (R3): workspace_for cuts each group's worktree from
        merger.tip() at its ready→running transition, so a group started right
        after a sibling merged already carries that sibling's work — no new cut
        logic, just a regression test pinning the existing behaviour."""
        from orchestrator.cli import _workspace_seams
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.worktrees import create_worktree, group_branch

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

        # a prior group merges its work onto the integration branch immediately
        # before the next group's worktree is cut
        upstream = make_group("g0")
        wt0 = create_worktree(
            repo,
            run_id="r1",
            group_id="g0",
            name=upstream.name,
            branch=group_branch("r1", "g0"),
            start_point=merger.tip(),
        )
        (wt0 / "upstream.txt").write_text("from g0\n")
        for args in (["add", "-A"], ["commit", "-m", "g0 work"]):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=wt0,
                check=True,
                capture_output=True,
            )
        merger.merge_group(upstream, wt0)

        workspace_for, _, _ = _workspace_seams(
            repo, "r1", merger, RunPaths(repo, "r1"), SessionConfig()
        )
        path = workspace_for(make_group("g1"))
        assert (path / "upstream.txt").read_text() == "from g0\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class TestResolveDeps:
    """Plan U2: cli._resolve_deps wires the scheduler's resolve routine to real
    git — exercised directly here against a real repo, since the scheduler-level
    tests (test_scheduler.py) stub this seam out entirely."""

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        _git(repo, "commit", "--allow-empty", "-m", "init")
        return repo

    def _resolve_deps_kwargs(self, repo: Path, run_id: str) -> dict:
        """No manifest on disk and a runner that errors if ever called: none of
        these tests exercise the U5 in-place conflict-resolve ladder — that has
        its own coverage in test_scheduler.py's resolve tests — so the ladder's
        `latest_coder_session_id` must read "no session" and never touch the
        runner."""
        from orchestrator.config import ExecutionConfig
        from orchestrator.execution.manifest import ManifestStore, RunPaths

        class _UnreachableRunner:
            def resume(self, *args, **kwargs):
                raise AssertionError("runner.resume must not be called in this test")

        paths = RunPaths(repo, run_id)
        return {
            "runner": _UnreachableRunner(),
            "store": ManifestStore(paths),
            "execution": ExecutionConfig(),
            "paths": paths,
        }

    def test_autonomous_resolve_commits_stranded_changes_and_merges(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.worktrees import create_worktree, group_branch

        repo = self._repo(tmp_path)
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        group = make_group("g1")
        worktree = create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name=group.name,
            branch=group_branch("r1", "g1"),
            start_point=merger.tip(),
        )
        (worktree / "stranded.txt").write_text("uncommitted work\n")  # never committed

        deps = _resolve_deps(repo, "r1", merger, **self._resolve_deps_kwargs(repo, "r1"))
        assert deps.commit_stranded(group) is True
        assert deps.commits_ahead(group) == 1
        deps.merge_group(group)  # must not raise
        integration_wt = merger.ensure()
        tree = subprocess.run(
            ["git", "ls-tree", "--name-only", merger.branch],
            cwd=integration_wt,
            capture_output=True,
            text=True,
        ).stdout
        assert "stranded.txt" in tree

    def test_commit_stranded_is_a_no_op_on_a_clean_worktree(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.worktrees import create_worktree, group_branch

        repo = self._repo(tmp_path)
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        group = make_group("g1")
        create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name=group.name,
            branch=group_branch("r1", "g1"),
            start_point=merger.tip(),
        )
        deps = _resolve_deps(repo, "r1", merger, **self._resolve_deps_kwargs(repo, "r1"))
        assert deps.commit_stranded(group) is False
        assert deps.commits_ahead(group) == 0

    def test_merge_translates_a_real_conflict_into_resolve_conflict(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.scheduler import ResolveConflict
        from orchestrator.execution.worktrees import create_worktree, group_branch

        repo = self._repo(tmp_path)
        (repo / "shared.txt").write_text("original\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "shared")

        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        g1 = make_group("g1", files=["shared.txt"])
        wt1 = create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name=g1.name,
            branch=group_branch("r1", "g1"),
            start_point=merger.tip(),
        )
        (wt1 / "shared.txt").write_text("g1 version\n")
        _git(wt1, "add", "-A")
        _git(wt1, "commit", "-m", "g1 edits")

        g2 = make_group("g2", files=["shared.txt"])
        wt2 = create_worktree(
            repo,
            run_id="r1",
            group_id="g2",
            name=g2.name,
            branch=group_branch("r1", "g2"),
            start_point=merger.tip(),
        )
        (wt2 / "shared.txt").write_text("g2 version\n")
        _git(wt2, "add", "-A")
        _git(wt2, "commit", "-m", "g2 edits")
        merger.merge_group(g1, wt1)
        tip_before = merger.tip()

        deps = _resolve_deps(repo, "r1", merger, **self._resolve_deps_kwargs(repo, "r1"))
        with pytest.raises(ResolveConflict, match="g2"):
            deps.merge_group(g2)
        assert merger.tip() == tip_before  # U1's gate left the integration tip untouched


class TestResolveConflictLadder:
    """Plan U5: merge_for_resolve's in-place conflict-resolution ladder."""

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        (repo / "shared.txt").write_text("original\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "init")
        return repo

    def _conflicting_groups(self, repo: Path, merger):
        from orchestrator.execution.worktrees import create_worktree, group_branch

        launch_tip = merger.tip()
        g1 = make_group("g1", files=["shared.txt"])
        wt1 = create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name=g1.name,
            branch=group_branch("r1", "g1"),
            start_point=launch_tip,
        )
        (wt1 / "shared.txt").write_text("g1 version\n")
        _git(wt1, "add", "-A")
        _git(wt1, "commit", "-m", "g1 edits")

        # g2 forks from the *same* pre-g1 tip, so once g1 merges, g2's own
        # base no longer has g1's edit — the refresh conflicts.
        g2 = make_group("g2", files=["shared.txt"])
        wt2 = create_worktree(
            repo,
            run_id="r1",
            group_id="g2",
            name=g2.name,
            branch=group_branch("r1", "g2"),
            start_point=launch_tip,
        )
        (wt2 / "shared.txt").write_text("g2 version\n")
        _git(wt2, "add", "-A")
        _git(wt2, "commit", "-m", "g2 edits")
        merger.merge_group(g1, wt1)
        return g2, wt2

    def _manifest_with_coder(self, paths, gid: str, session_id: str) -> None:
        from orchestrator.execution.manifest import ManifestStore
        from orchestrator.model import GroupManifestEntry, RunManifest, SessionEntry, SessionRole

        manifest = RunManifest(run_id="r1", plan_path="p.md", base_session_id="base-0")
        manifest.groups[gid] = GroupManifestEntry(
            group_id=gid,
            group_name=gid,
            summary="s",
            sessions=[SessionEntry(session_id=session_id, role=SessionRole.CODER)],
        )
        ManifestStore(paths).save(manifest)

    def _fake_resume_runner(self, *, resolve: bool, integration_branch: str = ""):
        """Mirrors what conflict_resolve.md actually asks a coder to do: merge
        the integration branch into its own worktree by hand and resolve the
        conflict markers — not just overwrite the file, since a plain
        overwrite (with no merge ever attempted) diverges again exactly the
        same way on the retry's own refresh."""
        import json as _json

        from orchestrator.execution.sessions import RoundResult, RoundUsage

        class FakeResumeRunner:
            def __init__(self):
                self.calls = 0

            def resume(self, *, session_id, prompt, cwd, json_schema=None, on_turn=None):
                self.calls += 1
                if resolve:
                    subprocess.run(
                        ["git", "merge", integration_branch], cwd=cwd, capture_output=True
                    )
                    (cwd / "shared.txt").write_text("resolved version\n")
                    _git(cwd, "add", "-A")
                    _git(cwd, "commit", "--no-edit")
                body = {
                    "status": "completed",
                    "summary": "resolved",
                    "verification_results": [],
                    "surprises": [],
                }
                text = f'<run-report status="completed">\n{_json.dumps(body)}\n</run-report>'
                return RoundResult(
                    session_id=session_id, text=text, usage=RoundUsage(), envelope={}
                )

        return FakeResumeRunner()

    def test_warm_resume_resolves_and_the_retry_merges(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.config import ExecutionConfig
        from orchestrator.execution.manifest import ManifestStore, RunPaths
        from orchestrator.execution.merge import IntegrationMerger

        repo = self._repo(tmp_path)
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        g2, wt2 = self._conflicting_groups(repo, merger)
        paths = RunPaths(repo, "r1")
        self._manifest_with_coder(paths, "g2", "coder-1")
        runner = self._fake_resume_runner(resolve=True, integration_branch=merger.branch)

        deps = _resolve_deps(
            repo,
            "r1",
            merger,
            runner=runner,
            store=ManifestStore(paths),
            execution=ExecutionConfig(max_conflict_resolve_attempts=1),
            paths=paths,
        )
        deps.merge_group(g2)  # must not raise
        assert runner.calls == 1
        assert deps.commits_ahead(g2) == 0

    def test_attempts_never_exceed_the_configured_bound(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.config import ExecutionConfig
        from orchestrator.execution.manifest import ManifestStore, RunPaths
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.scheduler import ResolveConflict

        repo = self._repo(tmp_path)
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        g2, wt2 = self._conflicting_groups(repo, merger)
        paths = RunPaths(repo, "r1")
        self._manifest_with_coder(paths, "g2", "coder-1")
        runner = self._fake_resume_runner(resolve=False)  # never actually fixes the conflict

        deps = _resolve_deps(
            repo,
            "r1",
            merger,
            runner=runner,
            store=ManifestStore(paths),
            execution=ExecutionConfig(max_conflict_resolve_attempts=1),
            paths=paths,
        )
        with pytest.raises(ResolveConflict):
            deps.merge_group(g2)
        assert runner.calls == 1  # exactly the configured bound, never more

    def test_no_reachable_session_raises_on_the_first_conflict_with_zero_attempts(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.config import ExecutionConfig
        from orchestrator.execution.manifest import ManifestStore, RunPaths
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.scheduler import ResolveConflict

        repo = self._repo(tmp_path)
        merger = IntegrationMerger(repo, "r1")
        merger.ensure()
        g2, wt2 = self._conflicting_groups(repo, merger)
        paths = RunPaths(repo, "r1")  # no manifest written — no session to find
        runner = self._fake_resume_runner(resolve=True)

        deps = _resolve_deps(
            repo,
            "r1",
            merger,
            runner=runner,
            store=ManifestStore(paths),
            execution=ExecutionConfig(max_conflict_resolve_attempts=3),
            paths=paths,
        )
        with pytest.raises(ResolveConflict):
            deps.merge_group(g2)
        assert runner.calls == 0

    def test_preflight_failure_on_resolve_path_logs_branch_reason_and_retry_command(self, tmp_path):
        from orchestrator.cli import _resolve_deps
        from orchestrator.config import ExecutionConfig, PreflightConfig
        from orchestrator.execution.manifest import ManifestStore, RunPaths, log_event
        from orchestrator.execution.merge import IntegrationMerger
        from orchestrator.execution.scheduler import ResolvePreflightFailed
        from orchestrator.execution.worktrees import create_worktree, group_branch

        repo = self._repo(tmp_path)
        paths = RunPaths(repo, "r1")
        merger = IntegrationMerger(
            repo,
            "r1",
            preflight_config=PreflightConfig(check_command=["false"]),
            preflight_output_dir=paths.group_dir,
            log=lambda message: log_event(paths, message),
        )
        merger.ensure()
        g1 = make_group("g1", files=["own.txt"])
        wt1 = create_worktree(
            repo,
            run_id="r1",
            group_id="g1",
            name=g1.name,
            branch=group_branch("r1", "g1"),
            start_point=merger.tip(),
        )
        (wt1 / "own.txt").write_text("g1 work\n")
        _git(wt1, "add", "-A")
        _git(wt1, "commit", "-m", "g1 work")

        deps = _resolve_deps(
            repo,
            "r1",
            merger,
            runner=self._fake_resume_runner(resolve=True),
            store=ManifestStore(paths),
            execution=ExecutionConfig(),
            paths=paths,
        )
        with pytest.raises(ResolvePreflightFailed):
            deps.merge_group(g1)
        log_text = paths.event_log_path.read_text()
        assert group_branch("r1", "g1") in log_text
        assert "retry" in log_text
        assert "smart-mcps-orchestrate retry" in log_text


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
        workspace_for, base_ref_for, _ = _workspace_seams(
            repo, "r1", merger, RunPaths(repo, "r1"), SessionConfig()
        )
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


class TestStatusDriverLiveness:
    """Plan U11: `status` reports whether a process is driving the run — the
    advisory flock, not `state.json`'s worker pids — separately from whether it
    looks like it is making progress, read from the freshest active group's
    heartbeat file mtime."""

    def _write_state(self, tmp_path, *, group_state: GroupState = GroupState.RUNNING) -> RunPaths:
        paths = RunPaths(tmp_path, "r1")
        atomic_write_text(
            paths.state_path,
            RunState(
                run_id="r1", groups={"g1": GroupRunState(state=group_state)}
            ).model_dump_json(),
        )
        return paths

    def test_no_driver_record_at_all(self, tmp_path, capsys):
        self._write_state(tmp_path)
        exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        assert exit_code == 0
        assert "no process is driving this run" in capsys.readouterr().out

    def test_a_process_holding_the_lock_is_reported_as_driving(self, tmp_path, capsys):
        paths = self._write_state(tmp_path)
        lock = DriverLock(paths)
        lock.acquire()
        try:
            exit_code = main(["status", "r1", "--repo", str(tmp_path)])
            assert exit_code == 0
            out = capsys.readouterr().out
            assert "a process is driving this run" in out
        finally:
            lock.release()

    def test_none_is_driving_once_the_lock_is_released(self, tmp_path, capsys):
        paths = self._write_state(tmp_path)
        lock = DriverLock(paths)
        lock.acquire()
        lock.release()
        exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        assert exit_code == 0
        assert "no process is driving this run" in capsys.readouterr().out

    def test_a_stale_heartbeat_is_reported_from_the_files_mtime(self, tmp_path, capsys):
        paths = self._write_state(tmp_path)
        hb_path = paths.group_dir("g1") / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        # Content lies and claims "just now" — only the mtime should count.
        atomic_write_text(hb_path, '{"updated_at": "2099-01-01T00:00:00+00:00"}')
        old = time.time() - (STALE_HEARTBEAT_SECONDS + 30)
        os.utime(hb_path, (old, old))

        lock = DriverLock(paths)
        lock.acquire()
        try:
            exit_code = main(["status", "r1", "--repo", str(tmp_path)])
            assert exit_code == 0
            out = capsys.readouterr().out
            assert "a process is driving this run" in out
            assert "stale" in out
        finally:
            lock.release()


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


class TestPartitionReportDependencyDirection:
    """Plan U8: trace.dag maps upstream_gid -> {downstream_gids} (build_group_dag
    in orchestrator/grouping/partition.py), so printing it directly under each
    group listed a group's *dependents*, mislabeled as what it "depends on"."""

    def test_prints_upstream_dependencies_not_downstream_dependents(self, capsys):
        from orchestrator.cli import _print_partition_report
        from orchestrator.grouping.trace import GroupingTrace, StageSnapshot

        trace = GroupingTrace(
            stages=[StageSnapshot(stage="final", partition={"a": 0, "b": 1, "c": 2})],
            dag={0: [1, 2]},  # g1 (task a) is upstream of g2 (task b) and g3 (task c)
        )
        _print_partition_report(trace)
        out = capsys.readouterr().out
        sections = {}
        for block in out.split("\n\n"):
            lines = block.strip().splitlines()
            if lines and lines[0].rstrip(":") in ("g1", "g2", "g3"):
                sections[lines[0].rstrip(":")] = block

        assert "depends on: none" in sections["g1"]
        assert "depends on: g1" in sections["g2"]
        assert "depends on: g1" in sections["g3"]
        # the old (buggy) label never appears again
        assert "downstream" not in out


class TestStdoutBuffering:
    """P5: a backgrounded run's log must not be empty until the run ends.

    Python block-buffers stdout at 8KB when it is not a tty, so `… > run.log 2>&1 &`
    left the file empty for the life of the run: the confinement header was
    invisible and a healthy run was indistinguishable from a hang. `-u` is not
    available to a console-script entry point, so `main()` reconfigures the stream
    itself.

    Asserted against a real subprocess writing to a real file, because that is the
    only place buffering is observable — an in-process assertion after the fact
    would pass either way.
    """

    def test_output_reaches_a_redirected_file_before_the_process_exits(self, tmp_path):
        log = tmp_path / "run.log"
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import sys, time\n"
            "from orchestrator.cli import main\n"
            # `status` on an empty repo prints one short line — far under the 8KB
            # block-buffer threshold, which is exactly the case that used to vanish.
            f"main(['status', '--repo', {str(tmp_path)!r}])\n"
            "time.sleep(30)\n"
        )
        with log.open("w") as sink:
            proc = subprocess.Popen([sys.executable, str(probe)], stdout=sink, stderr=sink)
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not log.read_text().strip():
                time.sleep(0.05)
            assert proc.poll() is None, "the probe exited early; the test proved nothing"
            assert "no runs" in log.read_text(), "stdout was still buffered in the process"
        finally:
            proc.kill()
            proc.wait(timeout=10)


class TestReportOnePagerCli:
    """`report --scaffold one-pager` / `--validate` (plan U5), exercised
    against the real ``r20260829-162627`` fixture run so the pointer set
    comes from an actual plan and git range, not a synthetic stand-in."""

    FIXTURE_RUN_ID = "r20260829-162627"
    FIXTURE_RUN_DIR = Path(__file__).parent / "fixtures" / "runs" / "r20260829-162627"

    def test_scaffold_then_validate_untouched_fails_naming_violations(self, tmp_path, capsys):
        out_dir = tmp_path / "op"
        exit_code = main(
            [
                "report",
                self.FIXTURE_RUN_ID,
                "--run-dir",
                str(self.FIXTURE_RUN_DIR),
                "--scaffold",
                "one-pager",
                "--out",
                str(out_dir),
            ]
        )
        assert exit_code == 0
        one_pager = out_dir / "one-pager.md"
        assert one_pager.is_file()

        capsys.readouterr()
        exit_code = main(
            [
                "report",
                self.FIXTURE_RUN_ID,
                "--run-dir",
                str(self.FIXTURE_RUN_DIR),
                "--validate",
                str(one_pager),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 1
        assert out.count("unknown pointer") >= 3

    def test_validate_clean_one_pager_exits_zero_silently_then_flips_on_banned_phrase(
        self, tmp_path, capsys
    ):
        one_pager = tmp_path / "one-pager.md"
        one_pager.write_text(
            "# Plan split and deepen — r20260829-162627\n\n"
            "## TL;DR\n\n"
            "- Three groups landed cleanly with verification passing (g1)\n"
            "- The plan-edit module backs the split and deepen commands "
            "(orchestrator/grouping/plan_edit.py)\n"
            "- Requirement sixteen on mechanical plan splitting is satisfied (R16)\n\n"
            "## Problems found\n\n"
            "- No problems were recorded for this run (g1)\n\n"
            "## Next steps\n\n"
            "- Watch the plan-edit surgery contract for drift next run "
            "(orchestrator/grouping/plan_edit.py)\n"
        )

        capsys.readouterr()
        exit_code = main(
            [
                "report",
                self.FIXTURE_RUN_ID,
                "--run-dir",
                str(self.FIXTURE_RUN_DIR),
                "--validate",
                str(one_pager),
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == ""

        before = one_pager.read_bytes()
        text = one_pager.read_text().replace(
            "- No problems were recorded for this run (g1)",
            "- No problems were recorded for this run Overall (g1)",
        )
        one_pager.write_text(text)
        exit_code = main(
            [
                "report",
                self.FIXTURE_RUN_ID,
                "--run-dir",
                str(self.FIXTURE_RUN_DIR),
                "--validate",
                str(one_pager),
            ]
        )
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "banned phrase: 'overall'" in out
        # The earlier clean validation call never wrote back to the file.
        assert (
            before
            == (
                "# Plan split and deepen — r20260829-162627\n\n"
                "## TL;DR\n\n"
                "- Three groups landed cleanly with verification passing (g1)\n"
                "- The plan-edit module backs the split and deepen commands "
                "(orchestrator/grouping/plan_edit.py)\n"
                "- Requirement sixteen on mechanical plan splitting is satisfied (R16)\n\n"
                "## Problems found\n\n"
                "- No problems were recorded for this run (g1)\n\n"
                "## Next steps\n\n"
                "- Watch the plan-edit surgery contract for drift next run "
                "(orchestrator/grouping/plan_edit.py)\n"
            ).encode()
        )
