"""The ``evaluate`` recipe (plan U8): a ``run`` unit plus a KPI Contract, a
hash-checked harness and an optional smoke gate."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from orchestrator.execution.kpi import KpiContract
from orchestrator.model import SessionRole
from orchestrator.recipes.registry import UnitRecipe
from orchestrator.recipes.run import RunArgs, RunRecord, merge_run, price_run


class EvaluateArgs(RunArgs):
    """``recipe_args`` for an ``evaluate`` unit — ``run``'s args plus a
    required KPI Contract and required ``measurements`` (unlike ``run``,
    where measurements are optional)."""

    model_config = ConfigDict(extra="forbid")

    kpi: KpiContract
    measurements: str = Field(min_length=1)


class EvaluationRecord(RunRecord):
    """Completion contract of an ``evaluate`` unit: ``run``'s contract plus
    the KPI, guards and harness hash read off the measurements JSON.
    ``threshold_cleared`` is informational — it never gates the unit."""

    model_config = ConfigDict(extra="forbid")

    kpi_key: str
    kpi_value: float | None
    guards: dict[str, float] = Field(default_factory=dict)
    harness_hash: str
    threshold_cleared: bool | None = None


#: ``evaluate`` prices exactly like ``run`` — same command wall-clock sum,
#: same triage token allowance.
price_evaluate = price_run

#: ``evaluate`` merges exactly like ``run`` — its own declared ``commit_paths``.
merge_evaluate = merge_run

EVALUATE_RECIPE = UnitRecipe(
    name="evaluate",
    args_model=EvaluateArgs,
    price=price_evaluate,
    contract=EvaluationRecord,
    worker_prompt="run_triage",
    worker_role=SessionRole.RUNNER,
    merge=merge_evaluate,
    reviewer_prompt=None,
    handoff_prompt=None,
    executor="orchestrator.execution.evaluate_executor:make_executor",
)
