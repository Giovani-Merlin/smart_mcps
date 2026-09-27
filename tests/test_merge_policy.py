"""U15 tests: the merge ladder enforces a recipe's declared commit globs and
registers the recipe's schema (plan Unit Recipes R/E/O, g6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ConfigDict

import orchestrator.recipes.registry as registry_mod
from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.review import ReviewDeps, make_executor
from orchestrator.execution.scheduler import GroupContext, GroupFailure, GroupState, Scheduler
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.execution.worktrees import create_worktree, group_branch
from orchestrator.model import CoderReport, ReviewIntensity, RunManifest, SessionRole
from orchestrator.recipes.registry import MergePolicy, RecipePrice, UnitRecipe

from tests.test_review_loop import StubRunner, coder_report, make_group


class StubReport(CoderReport):
    """A recipe contract distinct from ``CoderReport`` by class name only, so
    the Artifact Manifest's ``schema`` field can be told apart from the code
    recipe's."""

    model_config = ConfigDict(extra="forbid")


def _stub_price(args, metadata, config):
    return RecipePrice(tokens=0)


STUB_RECIPE = UnitRecipe(
    name="stub",
    args_model=None,
    price=_stub_price,
    contract=StubReport,
    worker_prompt="coder",
    worker_role=SessionRole.CODER,
    merge=lambda args: MergePolicy(commit_globs=("docs/research/*.md",)),
    reviewer_prompt="reviewer",
    handoff_prompt="handoff",
    executor="orchestrator.execution.review:make_executor",
)


@pytest.fixture
def stub_recipe(monkeypatch):
    """Registers ``STUB_RECIPE`` under ``"stub"`` for the duration of a test,
    without touching the real registry entries — every caller of
    ``get_recipe`` (``merge_ladder``, ``generation``) resolves through the
    same ``_entries`` function object."""
    original = registry_mod._entries

    def with_stub():
        entries = dict(original())
        entries["stub"] = STUB_RECIPE
        return entries

    monkeypatch.setattr(registry_mod, "_entries", with_stub)


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
    (repo / "README.md").write_text("original\n")
    (repo / "docs" / "research").mkdir(parents=True)
    (repo / "docs" / "research" / ".gitkeep").write_text("")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def _make_worktree(repo: Path, merger: IntegrationMerger, group_id: str = "g1") -> Path:
    return create_worktree(
        repo,
        run_id="r1",
        group_id=group_id,
        name=f"stub group {group_id}",
        branch=group_branch("r1", group_id),
        start_point=merger.tip(),
    )


def _deps(
    repo: Path,
    tmp_path: Path,
    runner: StubRunner,
    workspace: Path,
    merger: IntegrationMerger,
    *,
    artifacts: ArtifactManifestStore | None = None,
) -> ReviewDeps:
    paths = RunPaths(repo, "r1", run_dir=tmp_path / "run")
    return ReviewDeps(
        run_id="r1",
        runner=runner,
        store=ManifestStore(paths),
        manifest=RunManifest(run_id="r1", plan_path="p.md"),
        base_context="",
        base_session_id=None,
        fork_base_session=False,
        breaker=BreakerConfig(),
        execution=ExecutionConfig(),
        board=SurpriseBoard(),
        workspace_for=lambda group: workspace,
        merge_group=merger.merge_group,
        rewrite_spec=lambda group, surprises: group,
        base_ref_for=lambda group: "main",
        artifacts=artifacts,
    )


def _context(group) -> GroupContext:
    return GroupContext(
        group=group, generation=1, set_state=lambda s: None, set_generation=lambda g: None
    )


@pytest.mark.asyncio
async def test_merge_refuses_a_path_outside_the_declared_commit_globs(repo, tmp_path, stub_recipe):
    # [g6-1] a change outside the recipe's declared globs fails the merge,
    # naming the offender and never the in-glob path.
    merger = IntegrationMerger(repo, "r1")
    wt = _make_worktree(repo, merger)
    (wt / "docs" / "research" / "x.md").write_text("finding\n")
    (wt / "README.md").write_text("changed\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "feat: research plus an out-of-scope readme edit")

    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    deps = _deps(repo, tmp_path, runner, wt, merger)
    group = make_group("g1", intensity=ReviewIntensity.SELF_VERIFY, recipe="stub")

    with pytest.raises(GroupFailure) as excinfo:
        await make_executor(deps)(_context(group))
    message = str(excinfo.value)
    assert "README.md" in message
    assert "docs/research/x.md" not in message


@pytest.mark.asyncio
async def test_merge_within_globs_merges_and_registers_the_recipe_schema(
    repo, tmp_path, stub_recipe
):
    # [g6-1] a change confined to the declared globs merges normally and the
    # Artifact Manifest entry's schema names the recipe's own contract class.
    merger = IntegrationMerger(repo, "r1")
    wt = _make_worktree(repo, merger)
    (wt / "docs" / "research" / "x.md").write_text("finding\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "feat: research finding only")

    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    artifact_store = ArtifactManifestStore(RunPaths(repo, "r1", run_dir=tmp_path / "run"))
    deps = _deps(repo, tmp_path, runner, wt, merger, artifacts=artifact_store)
    group = make_group("g1", intensity=ReviewIntensity.SELF_VERIFY, recipe="stub")

    state = await make_executor(deps)(_context(group))
    assert state == GroupState.COMPLETED

    entry = artifact_store.load().entries["g1"]
    assert entry.schema_name == "StubReport"
    assert entry.paths == ["docs/research/x.md"]
    assert entry.status == "complete"


@pytest.mark.asyncio
async def test_merge_refusal_fails_the_group_with_no_rewrite_and_retry_is_the_recovery(
    repo, tmp_path, stub_recipe
):
    # [g6-3] a refused merge lands the group in terminal FAILED in state.json,
    # naming the offending path in `failure`, and never spends a rewrite —
    # `retry` (not a resumed/rewritten spec) is its documented recovery.
    merger = IntegrationMerger(repo, "r1")
    wt = _make_worktree(repo, merger)
    (wt / "docs" / "research" / "x.md").write_text("finding\n")
    (wt / "README.md").write_text("changed\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "feat: research plus an out-of-scope readme edit")

    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    paths = RunPaths(repo, "r1", run_dir=tmp_path / "run")
    rewrite_calls: list[list] = []

    deps = ReviewDeps(
        run_id="r1",
        runner=runner,
        store=ManifestStore(paths),
        manifest=RunManifest(run_id="r1", plan_path="p.md"),
        base_context="",
        base_session_id=None,
        fork_base_session=False,
        breaker=BreakerConfig(),
        execution=ExecutionConfig(),
        board=SurpriseBoard(),
        workspace_for=lambda group: wt,
        merge_group=merger.merge_group,
        rewrite_spec=lambda group, surprises: rewrite_calls.append(surprises) or group,
        base_ref_for=lambda group: "main",
    )
    group = make_group("g1", intensity=ReviewIntensity.SELF_VERIFY, recipe="stub")

    scheduler = Scheduler(groups=[group], paths=paths, executor=make_executor(deps))
    states = await scheduler.run()

    assert states["g1"] == GroupState.FAILED
    assert rewrite_calls == []  # no rewrite spent

    persisted = RunPaths(repo, "r1", run_dir=tmp_path / "run").state_path.read_text()
    assert '"state": "failed"' in persisted or '"state":"failed"' in persisted
    entry = scheduler.state.groups["g1"]
    assert entry.state == GroupState.FAILED
    assert entry.failure is not None and "README.md" in entry.failure
