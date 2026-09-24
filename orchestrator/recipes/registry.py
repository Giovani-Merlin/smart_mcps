"""The single enumeration of Unit Recipes (R2)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from orchestrator.config import EstimatorConfig


@dataclass(frozen=True)
class RecipePrice:
    """A unit's price: tokens, optional wall-clock seconds, and whether a recipe
    default stood in for something the plan did not declare."""

    tokens: float
    wall_clock_s: float | None = None
    defaulted: bool = False


PriceFn = Callable[[BaseModel | None, Mapping[str, object], EstimatorConfig], RecipePrice]


@dataclass(frozen=True)
class UnitRecipe:
    """One entry in the registry. Owns everything specific to its kind of work:
    args schema, pricing, completion contract, reviewer/handoff prompts, merge
    policy, and the dotted path to its executor. ``custom`` is a free mapping the
    core never reads — a recipe's own escape hatch."""

    name: str
    args_model: type[BaseModel] | None
    price: PriceFn
    contract: type[BaseModel]
    reviewer_prompt: str | None
    handoff_prompt: str | None
    merge: Literal["code_ladder", "run_commit_paths"]
    executor: str  # dotted path, resolved lazily by the dispatcher
    custom: Mapping[str, object] = field(default_factory=dict)


def _entries() -> dict[str, UnitRecipe]:
    # Imported lazily, not at module load: both point back into this package,
    # and code.py's price_code lazily imports grouping.estimator in turn.
    from orchestrator.recipes.code import CODE_RECIPE
    from orchestrator.recipes.run import RUN_RECIPE

    return {r.name: r for r in (CODE_RECIPE, RUN_RECIPE)}


def registered_names() -> tuple[str, ...]:
    return tuple(_entries())


def get_recipe(name: str) -> UnitRecipe:
    entries = _entries()
    if name not in entries:
        raise KeyError(f"unknown recipe {name!r}; registered recipes: {list(entries)}")
    return entries[name]
