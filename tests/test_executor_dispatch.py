"""U5 tests: dispatch.make_executor routes on Group.recipe, the `code` path is
byte-for-byte the same sequence of SessionRunner calls as review.make_executor,
and the recipe gate refuses before any worktree is created."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.dispatch import (
    _resolve_factory,
    RecipeDispatchError,
    make_executor,
    recipe_gate_violations,
    unknown_recipe_groups,
)
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.review import ReviewDeps
from orchestrator.execution.review import make_executor as make_code_executor
from orchestrator.execution.scheduler import GroupContext, GroupRunState, GroupState
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.model import Group, ReviewIntensity, RunManifest, Surprise
from orchestrator.recipes import registered_names

from tests.test_review_loop import BASE_CONTEXT, StubRunner, coder_report, make_group


def build_deps(tmp_path: Path, runner: StubRunner) -> ReviewDeps:
    store = ManifestStore(RunPaths(tmp_path, "r1"))
    manifest = RunManifest(run_id="r1", plan_path="p.md", base_session_id=None)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)

    def merge_group(group: Group, ws: Path) -> None:
        pass

    def rewrite_spec(group: Group, surprises: list[Surprise]) -> Group:
        return group

    return ReviewDeps(
        run_id="r1",
        runner=runner,
        store=store,
        manifest=manifest,
        base_context=BASE_CONTEXT,
        base_session_id=None,
        fork_base_session=False,
        breaker=BreakerConfig(),
        execution=ExecutionConfig(),
        board=SurpriseBoard(),
        workspace_for=lambda group: workspace,
        merge_group=merge_group,
        rewrite_spec=rewrite_spec,
        base_ref_for=lambda group: "main",
    )


def context_for(group: Group) -> GroupContext:
    return GroupContext(
        group=group,
        generation=1,
        set_state=lambda s: None,
        set_generation=lambda g: None,
    )


@pytest.mark.asyncio
async def test_code_group_matches_review_make_executor_call_sequence(tmp_path):
    group = make_group(intensity=ReviewIntensity.SELF_VERIFY)

    runner_a = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    deps_a = build_deps(tmp_path, runner_a)
    state_a = await make_executor(deps_a, [group])(context_for(group))

    runner_b = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    deps_b = build_deps(tmp_path, runner_b)
    state_b = await make_code_executor(deps_b)(context_for(group))

    assert state_a == state_b == GroupState.COMPLETED
    assert runner_a.forks == runner_b.forks
    # Session ids are fresh uuids per run, so compare prompt bodies keyed by
    # fork name rather than the dict's own (session-id-keyed) keys.
    assert list(runner_a.prompts.values()) == list(runner_b.prompts.values())


@pytest.mark.asyncio
async def test_unresolvable_recipe_refuses_before_any_session(tmp_path):
    group = make_group(gid="g9").model_copy(update={"recipe": "research"})
    runner = StubRunner({})
    deps = build_deps(tmp_path, runner)
    with pytest.raises(RecipeDispatchError, match="g9"):
        make_executor(deps, [group])
    assert runner.forks == []


def test_unknown_recipe_groups_reports_by_group():
    group = make_group(gid="g9").model_copy(update={"recipe": "research"})
    unknown = unknown_recipe_groups([group, make_group(gid="g1")])
    assert [g.id for g in unknown] == ["g9"]


def test_recipe_gate_blocks_non_terminal_group_with_disabled_recipe():
    group = make_group(gid="g9").model_copy(update={"recipe": "run"})
    blocked = recipe_gate_violations([group], {}, enabled=[])
    assert [g.id for g in blocked] == ["g9"]


def test_recipe_gate_allows_completed_group_regardless_of_enabled():
    group = make_group(gid="g9").model_copy(update={"recipe": "run"})
    states = {"g9": GroupRunState(state=GroupState.COMPLETED)}
    blocked = recipe_gate_violations([group], states, enabled=[])
    assert blocked == []


def test_recipe_gate_still_blocks_quarantined_group():
    group = make_group(gid="g9").model_copy(update={"recipe": "run"})
    states = {"g9": GroupRunState(state=GroupState.INTERRUPTED, quarantined=True)}
    blocked = recipe_gate_violations([group], states, enabled=[])
    assert [g.id for g in blocked] == ["g9"]


def test_recipe_gate_allows_enabled_recipe():
    group = make_group(gid="g9").model_copy(update={"recipe": "run"})
    blocked = recipe_gate_violations([group], {}, enabled=["run"])
    assert blocked == []


@pytest.mark.parametrize("name", sorted(registered_names()))
def test_every_registered_recipe_executor_resolves(name):
    # The registry spells executors ``module:attr``; a dispatcher that splits on
    # the last ``.`` resolved ``code`` (special-cased) but never ``run`` — found by
    # the driver's live g7-7 probe on r20260924-134934.
    assert callable(_resolve_factory(name))
