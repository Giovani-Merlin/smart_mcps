"""The ``optimize`` recipe (plan U10): the code loop with a settle step that
scores every committed candidate against a KPI Contract and keeps only the
ones that clear it — one candidate, one evaluation, one decision per round.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.config import EstimatorConfig
from orchestrator.execution.kpi import KpiContract
from orchestrator.model import CoderReport, SessionRole
from orchestrator.recipes.registry import MergePolicy, RecipePrice, UnitRecipe
from orchestrator.recipes.run import TRIAGE_TOKEN_ALLOWANCE, RunCommand

#: The confirmation evaluation after a `promising` candidate spends a quarter
#: of a triage call's token allowance per evaluation — cheap relative to the
#: coder round itself, but non-zero (R18).
OPTIMIZE_EVAL_TOKEN_FRACTION = TRIAGE_TOKEN_ALLOWANCE / 4


class OptimizeArgs(BaseModel):
    """``recipe_args`` for an ``optimize`` unit."""

    model_config = ConfigDict(extra="forbid")

    commands: list[RunCommand] = Field(min_length=1)
    measurements: str = Field(min_length=1)
    kpi: KpiContract
    evaluations: int = Field(ge=1, le=100)
    allow_write: list[str] = Field(default_factory=list)


class OptimizeReport(CoderReport):
    """Completion contract of one optimize round: a coder's report plus a
    one-line description of the candidate it just committed."""

    model_config = ConfigDict(extra="forbid")

    candidate: str = ""

    @classmethod
    def extra_fields_example(cls) -> dict[str, object]:
        return {"candidate": "one line: what this candidate changes"}


def price_optimize(
    args: BaseModel | None, metadata: Mapping[str, object], config: EstimatorConfig
) -> RecipePrice:
    # Lazy, like price_code: grouping.estimator imports back into grouping.
    from orchestrator.grouping.estimator import code_node_work

    base_tokens = code_node_work(metadata, config)
    if isinstance(args, OptimizeArgs):
        evaluations = args.evaluations
        minutes = sum(c.wall_clock_min for c in args.commands)
    else:
        evaluations, minutes = 1, 0.0
    return RecipePrice(
        tokens=base_tokens + evaluations * OPTIMIZE_EVAL_TOKEN_FRACTION,
        wall_clock_s=evaluations * minutes * 60.0,
    )


def merge_optimize(args: BaseModel | None) -> MergePolicy:
    # The optimize loop enforces its own mutable region round by round
    # (harness paths, group.files) — the merge itself is the ordinary,
    # unrestricted code ladder.
    return MergePolicy(commit_globs=None)


OPTIMIZE_RECIPE = UnitRecipe(
    name="optimize",
    args_model=OptimizeArgs,
    price=price_optimize,
    contract=OptimizeReport,
    worker_prompt="optimize",
    worker_role=SessionRole.CODER,
    merge=merge_optimize,
    reviewer_prompt="optimize_reviewer",
    handoff_prompt="optimize_handoff",
    executor="orchestrator.execution.optimize_executor:make_executor",
)
