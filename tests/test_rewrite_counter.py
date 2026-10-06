"""U9: the rewrite count and whether it was charged persist in state.json,
survive a resume, show in `status` and export on the group."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.config import ExecutionConfig
from orchestrator.execution.export import build_export
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.review import _GroupExecution, make_executor
from orchestrator.execution.scheduler import GroupContext, GroupState, Scheduler
from orchestrator.model import ReviewIntensity, Surprise
from test_review_loop import Harness, StubRunner, coder_report, make_group, verdict

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "r20260828-220035"


def _scheduler(tmp_path: Path, group, *, resume: bool = False) -> Scheduler:
    paths = RunPaths(tmp_path, "r1")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    return Scheduler(groups=[group], paths=paths, executor=lambda ctx: None, resume=resume)


def _context(harness: Harness, scheduler: Scheduler, group) -> GroupContext:
    entry = scheduler.state.groups[group.id]
    return GroupContext(
        group=group,
        generation=1,
        set_state=harness.states.append,
        set_generation=harness.generations.append,
        rewrites=entry.rewrites_charged,
        record_rewrite=lambda counted: scheduler.record_rewrite(group.id, counted),
    )


def _state_entry(scheduler: Scheduler, gid: str) -> dict:
    return json.loads(scheduler.paths.state_path.read_text())["groups"][gid]


@pytest.mark.asyncio
async def test_blocked_report_persists_a_counted_rewrite(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report("blocked")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    group = make_group()
    scheduler = _scheduler(tmp_path, group)

    state = await make_executor(harness.deps)(_context(harness, scheduler, group))

    assert state == GroupState.COMPLETED
    entry = _state_entry(scheduler, "g1")
    assert entry["rewrites"] == 1
    assert entry["last_rewrite_counted"] is True


@pytest.mark.asyncio
async def test_refinement_only_rewrite_persists_uncounted(tmp_path):
    surprise = Surprise(
        kind="spec_refinement", description="[from g1] tighten it", affected_groups=["g2"]
    )
    runner = StubRunner({"r1-g2-coder-g1": [coder_report()], "r1-g2-reviewer-g1": []})
    harness = Harness(tmp_path, runner, execution=ExecutionConfig(max_rewrites=0))
    harness.board.mark(surprise, source_group="g1")
    group = make_group("g2", intensity=ReviewIntensity.SELF_VERIFY)
    scheduler = _scheduler(tmp_path, group)

    await make_executor(harness.deps)(_context(harness, scheduler, group))

    entry = _state_entry(scheduler, "g2")
    assert entry["rewrites"] == 1
    assert entry["last_rewrite_counted"] is False
    assert entry["rewrites_charged"] == 0


def test_resume_starts_the_cap_from_persisted_charges(tmp_path):
    group = make_group()
    scheduler = _scheduler(tmp_path, group)
    scheduler.record_rewrite("g1", True)
    scheduler.record_rewrite("g1", False)

    resumed = _scheduler(tmp_path, group, resume=True)
    entry = resumed.state.groups["g1"]
    assert (entry.rewrites, entry.rewrites_charged, entry.last_rewrite_counted) == (2, 1, False)

    harness = Harness(tmp_path, StubRunner({}))
    execution = _GroupExecution(harness.deps, _context(harness, resumed, group))
    assert execution.rewrites == 1


def _fixture_run(tmp_path: Path) -> RunPaths:
    run_dir = tmp_path / ".orchestrator" / "runs" / FIXTURE.name
    shutil.copytree(FIXTURE, run_dir)
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text())
    state["groups"]["g1"]["rewrites"] = 2
    state["groups"]["g1"]["last_rewrite_counted"] = False
    state_path.write_text(json.dumps(state))
    return RunPaths(tmp_path, FIXTURE.name)


def test_status_prints_rewrites_line(tmp_path):
    _fixture_run(tmp_path)
    out = subprocess.run(
        [sys.executable, "-m", "orchestrator.cli", "status", FIXTURE.name, "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.count("rewrites:") == 1
    g1_block = out.split("g1:")[1].split("\ng2:")[0]
    assert "rewrites: 2 (last: spec refinement)" in g1_block
    assert "rewrites:" not in out.split("\ng2:")[1]


def test_export_carries_rewrite_count(tmp_path):
    paths = _fixture_run(tmp_path)
    export = build_export(paths, project="proj", transcript_root=tmp_path / "none")
    by_id = {g.id: g for g in export.groups}
    assert by_id["g1"].rewrite_count == 2
    assert by_id["g1"].last_rewrite_counted is False
    assert by_id["g2"].rewrite_count == 0
    assert by_id["g2"].last_rewrite_counted is None
