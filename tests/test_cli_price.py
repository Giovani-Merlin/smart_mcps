"""Tests for `group --price` (plan U7/C3): sub-second task-map pricing with no
graph build and no codegraph client.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.estimator import price_plan
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.grouping.plan_reader import parse_task_map_for_pricing
from orchestrator.grouping.trace import GroupingTrace

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PLAN = (
    REPO_ROOT / "docs" / "plans" / "2026-08-26-001-fix-observatory-and-run-resilience-plan.md"
)


def _raising_runner(args):
    raise AssertionError(f"--price must never call codegraph (args={args})")


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
    raise AssertionError(f"unexpected codegraph call in a price-mode fixture test: {args}")


OVERSIZED_PRICE_PLAN = """# feat: oversized slice for pricing

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


class TestOverCapSliceCli:
    def _repo(self, tmp_path, token_budget: int | None = None):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(OVERSIZED_PRICE_PLAN)
        if token_budget is not None:
            config_dir = repo / ".orchestrator"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text(f"[estimator]\ntoken_budget = {token_budget}\n")
        return repo, plan

    def test_exits_nonzero_and_prints_member_detail(self, tmp_path, capsys):
        repo, plan = self._repo(tmp_path, token_budget=6000)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--price"],
            client=CodegraphClient(repo_root=repo, runner=_raising_runner),
        )
        assert exit_code != 0
        out = capsys.readouterr().out
        assert "reports [FAIL]" in out
        assert "reports-api" in out
        assert "reports-ui" in out
        assert "overshoot" in out
        assert "node work" in out
        assert "coder work" in out
        assert "result: FAIL" in out

    def test_under_cap_exits_zero(self, tmp_path, capsys):
        repo, plan = self._repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--price"],
            client=CodegraphClient(repo_root=repo, runner=_raising_runner),
        )
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "reports [pass]" in out
        assert "result: pass" in out


class TestPriceNeverTouchesCodegraph:
    def test_states_cap_is_approximate_and_never_calls_the_client(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(OVERSIZED_PRICE_PLAN)

        # A stub whose runner raises on any call — if --price ever touched
        # codegraph, this call would raise and fail the test.
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--price"],
            client=CodegraphClient(repo_root=repo, runner=_raising_runner),
        )
        assert exit_code in (0, 1)
        out = capsys.readouterr().out
        assert "cap is approximate" in out
        assert "empty codegraph summary" in out

    def test_default_client_construction_is_also_skipped(self, tmp_path, capsys):
        """No client injected at all (the production default path): --price
        must not construct a real ``CodegraphClient`` either, or this would
        shell out to the ``codegraph`` CLI and blow the sub-second budget."""
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(OVERSIZED_PRICE_PLAN)

        started = time.perf_counter()
        exit_code = main(["group", str(plan), "--repo", str(repo), "--price"])
        elapsed = time.perf_counter() - started
        assert exit_code == 0
        assert elapsed < 1.0, f"--price took {elapsed:.2f}s, want < 1s"


class TestNoTaskMap:
    def test_a_plan_without_a_task_map_is_actionable(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text("# feat: no task map\n\n- T1: do a thing\n")
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--price"],
            client=CodegraphClient(repo_root=repo, runner=_raising_runner),
        )
        assert exit_code != 0
        err = capsys.readouterr().err
        assert "task-mapped plan" in err


@pytest.mark.skipif(not REAL_PLAN.is_file(), reason="reference plan document missing")
class TestRealPlanCrossCheck:
    """Plan U7/C3: --price's per-task node work must match what the real,
    zero-LLM `group --no-spec` pipeline (this plan's tasks all carry empty
    ``symbols``, so no codegraph-derived edges) records in
    ``grouping-trace.json`` for the same map — proven against this repo's own
    2026-08-26 plan document, copied into an isolated repo so the comparison
    has no side effects on the real worktree or its own `.orchestrator/`.
    """

    def _isolated_repo(self, tmp_path):
        text = REAL_PLAN.read_text()
        # Repo drift since authoring: several units have since landed, so a
        # handful of `size_hints`-declared files now exist on disk — invalid
        # input either parser rejects identically (a size_hints entry only
        # prices prospective work). Stripped here so both parsers see the
        # same, internally consistent map; every `files:` entry is untouched.
        fixed_text = re.sub(r"    size_hints:\n(?:      [^\n]+\n)+", "", text)
        assert fixed_text != text, "expected at least one size_hints block to strip"

        repo = tmp_path / "repo"
        repo.mkdir()
        plan_path = repo / "plan.md"
        plan_path.write_text(fixed_text)

        mappings = parse_task_map_for_pricing(fixed_text, REPO_ROOT)
        assert mappings, "reference plan produced no task mappings"
        for mapping in mappings:
            for file in mapping.files:
                dst = repo / file
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / file, dst)
        return repo, plan_path

    def test_price_is_subsecond_and_matches_the_real_trace(self, tmp_path):
        repo, plan_path = self._isolated_repo(tmp_path)

        started = time.perf_counter()
        report = price_plan(plan_path=plan_path, repo_root=repo, config=OrchestratorConfig())
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"--price took {elapsed:.2f}s, want < 1s"
        assert report.tasks

        exit_code = main(
            [
                "group",
                str(plan_path),
                "--repo",
                str(repo),
                "--no-spec",
                "--allow-unknown-symbols",
                # This test prices the *live* working-tree bytes of the files
                # the 2026-08-26 plan happens to name, so a slice sits within a
                # few hundred work units of the cap and any ordinary commit that
                # grows one of those files (adding ~30 lines to model.py did it)
                # fails the partition on R5 — a signal about repo size, not about
                # the thing under test. What is under test is that `--price` and
                # `grouping-trace.json` agree on node work for the same map, and
                # that comparison needs a complete trace, not a partition that
                # fits a budget.
                "--allow-oversized-slice",
            ],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        trace_path = (
            repo / ".orchestrator" / "groupings" / "plan" / "preview" / "grouping-trace.json"
        )
        trace = GroupingTrace.model_validate_json(trace_path.read_text())
        trace_node_work = {entry.node: entry.total for entry in trace.node_work}
        assert trace_node_work, "reference trace produced no node_work entries"

        assert {t.task_id for t in report.tasks} == set(trace_node_work)
        for task in report.tasks:
            assert task.node_work == pytest.approx(
                trace_node_work[task.task_id], rel=1e-6, abs=1e-3
            )

    def test_cli_output_names_node_work_and_coder_work(self, tmp_path, capsys):
        repo, plan_path = self._isolated_repo(tmp_path)
        main(
            ["group", str(plan_path), "--repo", str(repo), "--price"],
            client=CodegraphClient(repo_root=repo, runner=_raising_runner),
        )
        out = capsys.readouterr().out
        assert "node work" in out
        assert "coder work" in out
