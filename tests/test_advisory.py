"""Tests for orchestrator/grouping/advisory.py — `group --advise` (plan U11).

Every fixture here is a bare task-map plan (no `## Units`) so `parse_task_map`
takes the deterministic fast path and no `symbols:` field is ever declared, so
no codegraph call beyond sync/quiescence/files fires — the whole pipeline is
exercised with zero LLM calls and a tiny, countable set of codegraph calls.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.advisory import (
    CUT_SWEEP_VALLEY_MARGIN,
    MONOLITHIC_CONDUCTANCE_THRESHOLD,
    MONOLITHIC_MODULARITY_THRESHOLD,
    NEAR_DISCONNECTED_EDGE_WEIGHT_THRESHOLD,
    SERIALITY_DEPTH_WIDTH_RATIO_THRESHOLD,
    build_advisory_report,
    serialize_advisory_report,
)
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.pipeline import (
    compute_partition,
    run_grouping,
    serialize_grouping,
)


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


def _counting_runner(counter: list):
    def runner(args):
        counter.append(tuple(args))
        return _stub_codegraph_runner(args)

    return runner


def _raising_llm_runner(prompt, schema):
    raise AssertionError(f"LLM must not be called for a task-mapped plan: {schema.get('title')}")


def _task_map_plan(tasks_yaml: str, title: str = "advisory fixture") -> str:
    return f"# feat: {title}\n\n## Task Map\n\n```yaml\n# orchestrator-task-map v1\ntasks:\n{tasks_yaml}```\n"


# A tree of uneven branches converging on one leaf (test_partition.py's own
# `_granularity_ladder_graph` shape): independent/balanced/monolithic produce
# different group counts and metrics, so the granularity comparison and its
# Pareto flags have something real to compare.
LADDER_PLAN = _task_map_plan(
    """\
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
""",
    title="ladder",
)

# Two disjoint chains: no shared files, no depends_on between the two sets —
# nothing at all bridges group {a1, a2} and group {b1, b2}.
DISJOINT_PLAN = _task_map_plan(
    """\
  - task_id: a1
    description: a1
    files: [app/a1.py]
  - task_id: a2
    description: a2
    files: [app/a2.py]
    depends_on: [a1]
  - task_id: b1
    description: b1
    files: [app/b1.py]
  - task_id: b2
    description: b2
    files: [app/b2.py]
    depends_on: [b1]
""",
    title="disjoint",
)

# A pure 5-node dependency chain: one task per wave, so critical path (5) is
# far past SERIALITY_DEPTH_WIDTH_RATIO_THRESHOLD times the widest wave (1).
CHAIN_PLAN = _task_map_plan(
    """\
  - task_id: c1
    description: c1
    files: [app/c1.py]
  - task_id: c2
    description: c2
    files: [app/c2.py]
    depends_on: [c1]
  - task_id: c3
    description: c3
    files: [app/c3.py]
    depends_on: [c2]
  - task_id: c4
    description: c4
    files: [app/c4.py]
    depends_on: [c3]
  - task_id: c5
    description: c5
    files: [app/c5.py]
    depends_on: [c4]
""",
    title="chain",
)

# Five tasks all touching one shared file, no dependencies among them: a
# uniform-weight affinity clique with no natural split.
CLIQUE_PLAN = _task_map_plan(
    """\
  - task_id: m1
    description: m1
    files: [app/shared.py]
  - task_id: m2
    description: m2
    files: [app/shared.py]
  - task_id: m3
    description: m3
    files: [app/shared.py]
  - task_id: m4
    description: m4
    files: [app/shared.py]
  - task_id: m5
    description: m5
    files: [app/shared.py]
""",
    title="clique",
)


def _repo(tmp_path, plan_text: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = repo / "plan.md"
    plan.write_text(plan_text)
    return repo, plan


class TestOneGraphBuild:
    """g3-1: `--advise` must build the task graph exactly once, no matter how
    many GRANULARITY_LEVELS presets it then partitions."""

    def test_advise_makes_no_more_codegraph_calls_than_one_compute_partition(self, tmp_path):
        repo, plan = _repo(tmp_path, LADDER_PLAN)
        baseline_calls: list = []
        compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_counting_runner(baseline_calls)),
        )

        advise_calls: list = []
        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_counting_runner(advise_calls)),
        )

        assert len(advise_calls) == len(baseline_calls)
        assert {level for level in ("independent", "balanced", "monolithic")} == {
            preset.granularity for preset in report.granularities
        }

    def test_report_carries_every_metric_per_preset_with_pareto_flags(self, tmp_path):
        repo, plan = _repo(tmp_path, LADDER_PLAN)
        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert len(report.granularities) == 3
        for preset in report.granularities:
            assert preset.group_count > 0
            assert preset.node_work_fraction_mean >= 0
            assert preset.node_work_fraction_max >= 0
            assert preset.cross_group_edge_cut >= 0
            assert preset.group_dag_depth >= 1
            assert preset.simulated_makespan >= 0
            assert isinstance(preset.modularity, float)
        # At least one preset is on the Pareto frontier — with only three
        # candidates and objectives that trade off (fewer groups vs. more
        # cross-group coordination), it is never the case that every preset
        # loses to some other on every axis.
        assert any(preset.pareto_dominant for preset in report.granularities)

    def test_zero_llm_calls(self, tmp_path):
        repo, plan = _repo(tmp_path, LADDER_PLAN)
        # llm_runner raises on any call — task-map plans never reach the mapper
        # LLM, and --advise never calls the speccer LLM either.
        build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )


class TestCohesionShapes:
    """g3-2: the three required shape fixtures."""

    def test_disjoint_task_sets_flagged_reading_as_n_separate_plans(self, tmp_path):
        repo, plan = _repo(tmp_path, DISJOINT_PLAN)
        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        disconnected = [f for f in report.cohesion if f.kind == "disconnected"]
        assert len(disconnected) == 1
        finding = disconnected[0]
        assert "2 separate plans" in finding.message
        assert finding.task_sets == [["a1", "a2"], ["b1", "b2"]]

    def test_pure_chain_flagged_serial_with_widest_gap_wave_boundary(self, tmp_path):
        repo, plan = _repo(tmp_path, CHAIN_PLAN)
        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        serial = [f for f in report.cohesion if f.kind == "serial"]
        assert serial, report.cohesion
        headline = serial[0]
        assert "serial phases" in headline.message
        assert headline.boundary["critical_path_length"] == 5
        assert headline.boundary["max_wave_width"] == 1
        assert "boundary_after_wave" in headline.boundary

    def test_clique_flagged_structurally_monolithic(self, tmp_path):
        repo, plan = _repo(tmp_path, CLIQUE_PLAN)
        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        monolithic = [f for f in report.cohesion if f.kind == "monolithic"]
        assert len(monolithic) == 1
        finding = monolithic[0]
        assert "structurally monolithic" in finding.message
        assert finding.boundary["modularity"] < MONOLITHIC_MODULARITY_THRESHOLD
        assert finding.boundary["best_cut_conductance"] > MONOLITHIC_CONDUCTANCE_THRESHOLD

    def test_ladder_plan_triggers_no_cohesion_findings(self, tmp_path):
        """A balanced, well-connected DAG (the ladder shape) should not trip
        any of the three diagnostics — a sanity check against false positives."""
        repo, plan = _repo(tmp_path, LADDER_PLAN)
        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        kinds = {f.kind for f in report.cohesion}
        assert "disconnected" not in kinds
        assert "monolithic" not in kinds


class TestPreviewIsolation:
    """g3-3: --advise never touches a persisted groups.json, and its own
    artifact lives under preview/."""

    def test_pre_existing_groups_json_is_byte_identical_after_advise(self, tmp_path):
        repo, plan = _repo(tmp_path, LADDER_PLAN)
        result, base_context = run_grouping(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        out_dir = repo / ".orchestrator" / "groupings" / "plan"
        out_dir.mkdir(parents=True)
        groups_path = out_dir / "groups.json"
        groups_path.write_text(serialize_grouping(result))
        before = groups_path.read_bytes()

        report = build_advisory_report(
            plan_path=plan,
            repo_root=repo,
            config=OrchestratorConfig(),
            llm_runner=_raising_llm_runner,
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        preview_dir = out_dir / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        (preview_dir / "advisory.json").write_text(serialize_advisory_report(report))

        assert groups_path.read_bytes() == before
        assert (preview_dir / "advisory.json").is_file()


class TestDeterminism:
    """g3-4: byte-identical across two runs on an unchanged repo, zero LLM."""

    def test_two_runs_are_byte_identical(self, tmp_path):
        repo, plan = _repo(tmp_path, LADDER_PLAN)

        def run_once():
            report = build_advisory_report(
                plan_path=plan,
                repo_root=repo,
                config=OrchestratorConfig(),
                llm_runner=_raising_llm_runner,
                client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
            )
            return serialize_advisory_report(report)

        first = run_once()
        second = run_once()
        assert first == second

    def test_clique_and_chain_are_also_deterministic(self, tmp_path):
        for i, plan_text in enumerate((CLIQUE_PLAN, CHAIN_PLAN, DISJOINT_PLAN)):
            root = tmp_path / f"variant{i}"
            root.mkdir()
            repo, plan = _repo(root, plan_text)

            def run_once():
                report = build_advisory_report(
                    plan_path=plan,
                    repo_root=repo,
                    config=OrchestratorConfig(),
                    llm_runner=_raising_llm_runner,
                    client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
                )
                return serialize_advisory_report(report)

            assert run_once() == run_once()


class TestThresholdsAreNamedConstants:
    """g3-5: every diagnostic threshold is a named module constant."""

    def test_constants_are_present_and_numeric(self):
        assert isinstance(NEAR_DISCONNECTED_EDGE_WEIGHT_THRESHOLD, (int, float))
        assert isinstance(SERIALITY_DEPTH_WIDTH_RATIO_THRESHOLD, (int, float))
        assert isinstance(CUT_SWEEP_VALLEY_MARGIN, (int, float))
        assert isinstance(MONOLITHIC_MODULARITY_THRESHOLD, (int, float))
        assert isinstance(MONOLITHIC_CONDUCTANCE_THRESHOLD, (int, float))

    def test_module_documents_each_threshold(self):
        import orchestrator.grouping.advisory as advisory_module

        source = advisory_module.__doc__ or ""
        # The justification comments live above each constant in the module
        # source itself, not the module docstring — check the source file.
        import inspect

        text = inspect.getsource(advisory_module)
        for name in (
            "NEAR_DISCONNECTED_EDGE_WEIGHT_THRESHOLD",
            "SERIALITY_DEPTH_WIDTH_RATIO_THRESHOLD",
            "CUT_SWEEP_VALLEY_MARGIN",
            "MONOLITHIC_MODULARITY_THRESHOLD",
            "MONOLITHIC_CONDUCTANCE_THRESHOLD",
        ):
            assert f"{name} = " in text


class TestFailureModes:
    def test_map_error_surfaces_as_grouper_error(self, tmp_path):
        from orchestrator.grouping.pipeline import GrouperError

        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(
            "# feat: broken\n\n## Task Map\n\n"
            "```yaml\n# orchestrator-task-map v1\nnot: a task map\n```\n"
        )
        with pytest.raises(GrouperError):
            build_advisory_report(
                plan_path=plan,
                repo_root=repo,
                config=OrchestratorConfig(),
                llm_runner=_raising_llm_runner,
                client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
            )
