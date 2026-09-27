"""The ``code`` recipe: today's coder/reviewer machinery, unchanged."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from orchestrator.config import EstimatorConfig
from orchestrator.model import CoderReport, SessionRole
from orchestrator.recipes.registry import MergePolicy, RecipePrice, UnitRecipe


def price_code(
    args: BaseModel | None, metadata: Mapping[str, object], config: EstimatorConfig
) -> RecipePrice:
    # Lazy: grouping.estimator (via plan_reader) imports back into grouping,
    # and this module must not import grouping at load time.
    from orchestrator.grouping.estimator import code_node_work

    return RecipePrice(tokens=code_node_work(metadata, config))


def merge_code(args: BaseModel | None) -> MergePolicy:
    return MergePolicy(commit_globs=None)


CODE_RECIPE = UnitRecipe(
    name="code",
    args_model=None,
    price=price_code,
    contract=CoderReport,
    worker_prompt="coder",
    worker_role=SessionRole.CODER,
    merge=merge_code,
    reviewer_prompt="reviewer",
    handoff_prompt="handoff",
    executor="orchestrator.execution.review:make_executor",
)
