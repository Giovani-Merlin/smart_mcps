"""The ``research`` recipe (plan U6): a worker session that grounds a
declared question in codegraph, queries Perplexity with the web tools as a
recorded fallback, and commits a Findings Artifact whose every finding
carries a source."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.config import EstimatorConfig
from orchestrator.model import SessionRole, WorkerReport
from orchestrator.recipes.registry import MergePolicy, RecipePrice, UnitRecipe

SUMMARY_MAX_CHARS = 2000
REFINEMENT_MAX_CHARS = 2000

#: Coder-token allowance per declared ``size`` class (Goal). ``medium`` is
#: also the default a plan that never declares ``size`` gets, so
#: ``price_research`` reports it ``defaulted``.
_SIZE_TOKENS: dict[str, float] = {
    "small": 30_000,
    "medium": 60_000,
    "large": 120_000,
}


class ResearchArgs(BaseModel):
    """``recipe_args`` for a ``research`` unit — ``extra="forbid"`` so an
    unknown key is a hard parse error naming the field."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    output: str
    focus_paths: list[str] = Field(default_factory=list)
    size: Literal["small", "medium", "large"] = "medium"

    @field_validator("output")
    @classmethod
    def _output_under_docs_research(cls, value: str) -> str:
        if not (value.startswith("docs/research/") and value.endswith(".md")):
            raise ValueError(f"output {value!r} must start with 'docs/research/' and end in '.md'")
        return value


class Finding(BaseModel):
    """One research finding — ``sources`` requires at least one URL or
    repo-relative path so a Findings Artifact never asserts an unsourced
    claim."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]
    freshness: str | None = None


class SpecRefinement(BaseModel):
    """A research group's one-shot refinement of its declared downstream
    consumer's spec (plan U7), carried here so ``FindingsReport`` can validate
    its shape even though the surprise-board wiring is a later unit."""

    model_config = ConfigDict(extra="forbid")

    target_task: str = Field(min_length=1)
    refinement: str = Field(min_length=1, max_length=REFINEMENT_MAX_CHARS)


class FindingsReport(WorkerReport):
    """Completion contract of a ``research`` unit: at least one sourced
    finding on a ``completed`` report, an optional fallback-provider flag, and
    an optional single spec refinement for the group's declared consumer."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=SUMMARY_MAX_CHARS)
    findings: list[Finding] = Field(default_factory=list)
    provider_fallback: bool = False
    spec_refinement: SpecRefinement | None = None

    @model_validator(mode="after")
    def _completed_requires_findings(self) -> "FindingsReport":
        if self.status == "completed" and not self.findings:
            raise ValueError("status 'completed' requires at least one finding")
        return self


def price_research(
    args: BaseModel | None, metadata: Mapping[str, object], config: EstimatorConfig
) -> RecipePrice:
    if isinstance(args, ResearchArgs):
        size = args.size
        defaulted = "size" not in args.model_fields_set
    else:
        size, defaulted = "medium", True
    return RecipePrice(tokens=_SIZE_TOKENS[size], defaulted=defaulted)


def merge_research(args: BaseModel | None) -> MergePolicy:
    output = getattr(args, "output", None)
    return MergePolicy(commit_globs=(output,) if output else ())


RESEARCH_RECIPE = UnitRecipe(
    name="research",
    args_model=ResearchArgs,
    price=price_research,
    contract=FindingsReport,
    worker_prompt="research",
    worker_role=SessionRole.RESEARCHER,
    merge=merge_research,
    reviewer_prompt="research_reviewer",
    handoff_prompt="research_handoff",
    extra_allowed_tools=("Bash(smart-mcps-perplexity *)", "WebSearch", "WebFetch"),
    executor="orchestrator.execution.review:make_executor",
)
