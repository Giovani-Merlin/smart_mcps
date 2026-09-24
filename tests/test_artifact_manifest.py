"""U6 tests: the run-level Artifact Manifest (plan Unit Recipes v1, g5)."""

from __future__ import annotations

import dataclasses
import re
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.artifacts import (
    ARTIFACT_SUMMARY_MAX_CHARS,
    ArtifactEntry,
    ArtifactManifestStore,
    render_artifact_inputs_block,
)
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.review import ReviewDeps, make_executor
from orchestrator.execution.scheduler import (
    GroupContext,
    GroupFailure,
    GroupState,
    ResolveDeps,
    Scheduler,
)
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.execution.worktrees import create_worktree, group_branch
from orchestrator.model import ReviewIntensity, RunManifest

from tests.test_review_loop import (
    BASE_CONTEXT,
    Harness,
    StubRunner,
    coder_report,
    make_group,
    verdict,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "shared.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def commit_group_work(worktree: Path, filename: str, content: str, message: str) -> None:
    (worktree / filename).write_text(content)
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", message)


@pytest.mark.asyncio
async def test_two_code_groups_merge_and_register_resolvable_complete_entries(repo, tmp_path):
    # [g5-1] a real-git run of two code groups registers two `complete`
    # entries whose commits resolve to real merge commits.
    run_dir = tmp_path / "run"
    paths = RunPaths(repo, "r1", run_dir=run_dir)
    merger = IntegrationMerger(repo, "r1")
    artifact_store = ArtifactManifestStore(paths)
    store = ManifestStore(paths)
    manifest = RunManifest(run_id="r1", plan_path="p.md")

    g1 = make_group("g1", intensity=ReviewIntensity.SELF_VERIFY)
    g2 = make_group("g2", intensity=ReviewIntensity.SELF_VERIFY)
    wt1 = create_worktree(
        repo,
        run_id="r1",
        group_id="g1",
        name=g1.name,
        branch=group_branch("r1", "g1"),
        start_point=merger.tip(),
    )
    commit_group_work(wt1, "one.txt", "g1 work\n", "feat: g1")
    wt2 = create_worktree(
        repo,
        run_id="r1",
        group_id="g2",
        name=g2.name,
        branch=group_branch("r1", "g2"),
        start_point=merger.tip(),
    )
    commit_group_work(wt2, "two.txt", "g2 work\n", "feat: g2")

    runner = StubRunner({"r1-g1-coder-g1": [coder_report()], "r1-g2-coder-g1": [coder_report()]})
    workspaces = {"g1": wt1, "g2": wt2}
    deps = ReviewDeps(
        run_id="r1",
        runner=runner,
        store=store,
        manifest=manifest,
        base_context="",
        base_session_id=None,
        fork_base_session=False,
        breaker=BreakerConfig(),
        execution=ExecutionConfig(),
        board=SurpriseBoard(),
        workspace_for=lambda group: workspaces[group.id],
        merge_group=merger.merge_group,
        rewrite_spec=lambda group, surprises: group,
        base_ref_for=lambda group: "main",
        artifacts=artifact_store,
    )

    def context(group) -> GroupContext:
        return GroupContext(
            group=group, generation=1, set_state=lambda s: None, set_generation=lambda g: None
        )

    for group in (g1, g2):
        state = await make_executor(deps)(context(group))
        assert state == GroupState.COMPLETED

    loaded = artifact_store.load()
    assert set(loaded.entries) == {"g1", "g2"}
    for gid in ("g1", "g2"):
        entry = loaded.entries[gid]
        assert entry.status == "complete"
        assert entry.recipe == "code"
        assert entry.commit is not None
        kind = git(repo, "cat-file", "-t", entry.commit).strip()
        assert kind == "commit"


@pytest.mark.asyncio
async def test_resolved_stranded_work_produces_a_partial_entry(tmp_path):
    # [g5-2] a group RESOLVED from Stranded Work gets a `partial` entry.
    paths = RunPaths(tmp_path, "r1")
    artifact_store = ArtifactManifestStore(paths)

    def merge_group(group) -> str:
        return "deadbeefcafef00d"

    resolve = ResolveDeps(
        commit_stranded=lambda group: True,
        commits_ahead=lambda group: 1,
        merge_group=merge_group,
    )

    async def executor(ctx):
        raise GroupFailure("coder crashed mid-round")

    scheduler = Scheduler(
        groups=[make_group("g1")],
        paths=paths,
        executor=executor,
        resolve=resolve,
        artifacts=artifact_store,
    )
    states = await scheduler.run()
    assert states["g1"] == GroupState.RESOLVED

    entry = artifact_store.load().entries["g1"]
    assert entry.status == "partial"
    assert entry.commit == "deadbeefcafef00d"


def test_summary_over_2000_chars_is_rejected_naming_summary():
    # [g5-4]
    with pytest.raises(ValidationError, match="summary"):
        ArtifactEntry(
            artifact_id="g1",
            group_id="g1",
            recipe="code",
            schema="CoderReport",
            summary="x" * (ARTIFACT_SUMMARY_MAX_CHARS + 1),
            status="complete",
        )


def test_measurement_block_caps_at_8000_chars_and_ends_with_a_more_line():
    # [g5-5]
    measurements = {f"metric_{i:04d}_reasonably_long_measurement_name": i for i in range(300)}
    entry = ArtifactEntry(
        artifact_id="run1",
        group_id="g_run",
        recipe="run",
        schema="RunRecord",
        summary="a run entry with many measurements",
        status="complete",
        measurements=measurements,
    )
    block = render_artifact_inputs_block([entry])
    assert len(block) <= 8000
    last_line = block.rstrip("\n").splitlines()[-1]
    assert re.match(r"^\+\d+ more — see artifacts\.json$", last_line)


@pytest.mark.asyncio
async def test_downstream_prompt_carries_upstream_artifact_and_is_unchanged_without_one(tmp_path):
    # [g5-3]
    upstream = make_group("gU", intensity=ReviewIntensity.SELF_VERIFY, recipe="run")
    artifact_store = ArtifactManifestStore(RunPaths(tmp_path / "with", "r1"))
    artifact_store.register(
        ArtifactEntry(
            artifact_id="gU",
            group_id="gU",
            recipe="run",
            schema="RunRecord",
            summary="the run group's own summary",
            status="complete",
            measurements={"duration_s": 42, "exit_code": 0},
        )
    )

    downstream = make_group("g1", dependencies=["gU"])
    runner = StubRunner(
        {"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict("approved")]}
    )
    (tmp_path / "with").mkdir(exist_ok=True)
    harness = Harness(tmp_path / "with", runner)
    harness.deps = dataclasses.replace(
        harness.deps, artifacts=artifact_store, groups_by_id={"gU": upstream, "g1": downstream}
    )
    state = await harness.run(downstream)
    assert state == GroupState.COMPLETED
    sent = runner.prompts[runner.session_ids["r1-g1-coder-g1"]][0]
    assert "gU" in sent
    assert "the run group's own summary" in sent
    assert "duration_s: 42" in sent
    assert "one.txt" not in sent  # never file bodies/paths, only the index

    # Control: a group with no non-code upstream is byte-identical to a plain
    # `render_coder_prompt` call — no artifact block at all.
    from orchestrator.execution.prompting import render_coder_prompt

    plain_group = make_group("g2")
    runner2 = StubRunner(
        {"r1-g2-coder-g1": [coder_report()], "r1-g2-reviewer-g1": [verdict("approved")]}
    )
    (tmp_path / "without").mkdir(exist_ok=True)
    harness2 = Harness(tmp_path / "without", runner2)
    harness2.deps = dataclasses.replace(
        harness2.deps, artifacts=artifact_store, groups_by_id={"g2": plain_group}
    )
    state2 = await harness2.run(plain_group)
    assert state2 == GroupState.COMPLETED
    sent2 = runner2.prompts[runner2.session_ids["r1-g2-coder-g1"]][0]
    expected = f"{BASE_CONTEXT}\n\n{render_coder_prompt('r1', plain_group, decisions='')}"
    assert sent2 == expected


@pytest.mark.asyncio
async def test_retired_generation_handoff_prompt_carries_the_same_upstream_block(tmp_path):
    # [g5-6]
    upstream = make_group("gU", intensity=ReviewIntensity.SELF_VERIFY, recipe="run")
    artifact_store = ArtifactManifestStore(RunPaths(tmp_path, "r1"))
    artifact_store.register(
        ArtifactEntry(
            artifact_id="gU",
            group_id="gU",
            recipe="run",
            schema="RunRecord",
            summary="upstream run summary",
            status="complete",
        )
    )
    downstream = make_group("g1", dependencies=["gU"])

    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["fix y"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(
        tmp_path, runner, breaker=BreakerConfig(max_rounds_per_generation=1, max_generations=3)
    )
    harness.deps = dataclasses.replace(
        harness.deps, artifacts=artifact_store, groups_by_id={"gU": upstream, "g1": downstream}
    )
    state = await harness.run(downstream)
    assert state == GroupState.COMPLETED
    handoff = runner.prompts[runner.session_ids["r1-g1-coder-g2"]][0]
    assert "generation 2" in handoff
    assert "upstream run summary" in handoff
    assert "gU" in handoff
