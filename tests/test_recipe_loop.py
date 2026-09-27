"""U5 tests: the code loop reads its prompt/contract/reviewer/handoff/role
from the group's registry entry, rather than hard-coding the ``code`` ones.

``code``'s own prompts, reports and session records must come out
byte-identical; a stub recipe entry proves the loop actually reads the
registry rather than the ``code`` values happening to match by coincidence.
"""

from __future__ import annotations

import orchestrator.recipes as recipes_pkg
import pytest

from orchestrator.execution import prompting
from orchestrator.execution.prompting import render_coder_prompt, render_worker_prompt
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import CoderReport, ReviewIntensity, SessionRole
from orchestrator.recipes.registry import MergePolicy, RecipePrice, UnitRecipe

from tests.test_review_loop import Harness, StubRunner, coder_report, make_group


def test_code_recipe_worker_prompt_matches_render_coder_prompt_byte_for_byte():
    group = make_group()
    recipe = recipes_pkg.get_recipe("code")
    via_registry = render_worker_prompt(
        recipe.worker_prompt, "r1", group, decisions="some decisions"
    )
    direct = render_coder_prompt("r1", group, decisions="some decisions")
    assert via_registry == direct


@pytest.mark.asyncio
async def test_stub_recipe_uses_its_own_worker_prompt_and_role(monkeypatch, tmp_path):
    real_load_template = prompting.load_template

    def fake_load_template(name: str) -> str:
        if name == "stub_worker":
            return "STUB PROMPT for $group_name\n$verification\n$report_contract$decisions"
        return real_load_template(name)

    monkeypatch.setattr(prompting, "load_template", fake_load_template)

    stub_recipe = UnitRecipe(
        name="stub",
        args_model=None,
        price=lambda args, metadata, config: RecipePrice(tokens=0.0),
        contract=CoderReport,
        worker_prompt="stub_worker",
        worker_role=SessionRole.RESEARCHER,
        merge=lambda args: MergePolicy(commit_globs=None),
        reviewer_prompt=None,
        handoff_prompt=None,
        executor="orchestrator.execution.review:make_executor",
    )
    real_get_recipe = recipes_pkg.get_recipe

    def fake_get_recipe(name: str):
        if name == "stub":
            return stub_recipe
        return real_get_recipe(name)

    monkeypatch.setattr(recipes_pkg, "get_recipe", fake_get_recipe)

    group = make_group(recipe="stub", intensity=ReviewIntensity.SELF_VERIFY)
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    harness = Harness(tmp_path, runner)
    state = await harness.run(group)

    assert state == GroupState.COMPLETED
    session_id = runner.session_ids["r1-g1-coder-g1"]
    prompt = runner.prompts[session_id][0]
    assert "STUB PROMPT for group g1" in prompt
    roles = [s.role.value for s in harness.manifest.groups["g1"].sessions]
    assert roles == ["researcher"]
