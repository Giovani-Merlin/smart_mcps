"""The recipe dispatcher: one ``Executor`` that routes each group to its recipe's executor.

The scheduler still sees a single ``Executor``; only this module knows recipes
exist. Every recipe present in the run is resolved eagerly, so an unknown or
unimportable one refuses the run before any worktree is created.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable

from orchestrator.execution.review import ReviewDeps
from orchestrator.execution.review import make_executor as make_code_executor
from orchestrator.execution.scheduler import (
    TERMINAL_STATES,
    Executor,
    GroupContext,
    GroupRunState,
)
from orchestrator.model import Group
from orchestrator.recipes import get_recipe, registered_names


class RecipeDispatchError(Exception):
    """A group names a recipe the run cannot execute."""


ExecutorFactory = Callable[[ReviewDeps], Executor]


def _resolve_factory(recipe_name: str) -> ExecutorFactory:
    if recipe_name == "code":
        return make_code_executor
    try:
        recipe = get_recipe(recipe_name)
    except KeyError:
        raise RecipeDispatchError(
            f"unknown recipe {recipe_name!r}; registered recipes: {list(registered_names())}"
        ) from None
    module_path, _, attr = recipe.executor.rpartition(".")
    try:
        return getattr(importlib.import_module(module_path), attr)
    except (ImportError, AttributeError) as exc:
        raise RecipeDispatchError(
            f"recipe {recipe_name!r} executor {recipe.executor!r} cannot be imported: {exc}"
        ) from exc


def make_executor(
    deps: ReviewDeps,
    groups: Iterable[Group] = (),
    states: dict[str, GroupRunState] | None = None,
) -> Executor:
    """Executor that delegates on ``ctx.group.recipe``. ``groups`` are the run's
    groups; the recipe of every group that could still execute is resolved now
    (a group already terminal — ``COMPLETED``/``FAILED``/``RESOLVED`` and not
    quarantined — never reaches the executor again, so a resume does not pay
    for importing a recipe none of its remaining work uses). ``code`` is
    always available."""
    groups = list(groups)
    states = states or {}

    def still_active(group: Group) -> bool:
        entry = states.get(group.id)
        return entry is None or entry.state not in TERMINAL_STATES or entry.quarantined

    active = [g for g in groups if still_active(g)]
    executors: dict[str, Executor] = {}
    for name in {"code", *(g.recipe for g in active)}:
        try:
            executors[name] = _resolve_factory(name)(deps)
        except RecipeDispatchError as exc:
            offenders = sorted(g.id for g in active if g.recipe == name)
            raise RecipeDispatchError(f"group(s) {', '.join(offenders)}: {exc}") from exc

    async def executor(ctx: GroupContext):
        recipe = ctx.group.recipe
        if recipe not in executors:  # a group introduced after startup
            executors[recipe] = _resolve_factory(recipe)(deps)
        return await executors[recipe](ctx)

    return executor


def recipe_gate_violations(
    groups: Iterable[Group],
    states: dict[str, GroupRunState],
    enabled: Iterable[str],
) -> list[Group]:
    """Non-terminal (or quarantined) groups whose recipe is not in ``enabled``
    (``code`` is always allowed). Finished groups never block a resume."""
    allowed = {"code", *enabled}
    bad = []
    for group in groups:
        if group.recipe in allowed:
            continue
        entry = states.get(group.id)
        if entry is not None and entry.state in TERMINAL_STATES and not entry.quarantined:
            continue
        bad.append(group)
    return bad


def unknown_recipe_groups(groups: Iterable[Group]) -> list[Group]:
    known = set(registered_names())
    return [g for g in groups if g.recipe not in known]
