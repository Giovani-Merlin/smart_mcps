"""Configuration surface: thresholds, weights, and defaults for every stage.

All values are config-overridable (origin R5: thresholds are configuration, never
hardcoded policy). Defaults must load without any config file present; U9 layers
CLI-flag > config-file > default resolution on top of `load_config`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class EdgeWeightsConfig(BaseModel):
    """Affinity weights for the codegraph signals (plan R3) and the prose fallback.

    ``prose_neighbor`` is not a codegraph signal: it is the affinity a region-less
    task gets toward its plan-order neighbor so unmappable tasks cluster near the
    work they were written next to.
    """

    shared_file: float = 1.0
    call: float = 2.0
    impact: float = 1.5
    prose_neighbor: float = 0.5


class PartitionConfig(BaseModel):
    hub_threshold: float = 0.4  # CoCoder's live ROLE_THRESHOLD
    louvain_resolution: float = 1.0


class EstimatorConfig(BaseModel):
    """Token-budget estimator knobs (plan U3). Directional; tuned on real plans."""

    token_budget: int = 100_000
    bytes_per_token: float = 4.0
    slack_multiplier: float = 1.3
    per_file_tool_allowance: int = 2_000
    spec_tokens_allowance: int = 3_000  # partition-time stand-in before specs exist


class DifficultyConfig(BaseModel):
    """Difficulty = weighted sum of saturating-normalized signals, in [0, 1).

    Each signal x is normalized as x / (x + scale): the scale is the raw value at
    which that signal contributes half its weight. Tier thresholds pick the review
    intensity (origin R15): < d_review → self-verify, < d_hard → paired reviewer,
    else paired plus one mandatory extra round.
    """

    weight_files_touched: float = 1.0
    weight_max_fan: float = 1.5
    weight_hub_touches: float = 2.0
    weight_cross_group_edges: float = 1.5
    weight_verification_items: float = 1.0

    scale_files_touched: float = 6.0
    scale_max_fan: float = 10.0
    scale_hub_touches: float = 1.0
    scale_cross_group_edges: float = 3.0
    scale_verification_items: float = 5.0

    d_review: float = 0.35
    d_hard: float = 0.65


class BreakerConfig(BaseModel):
    """Circuit-breaker thresholds (origin R14; plan Key Technical Decisions)."""

    context_token_limit: int = 120_000
    max_rounds_per_generation: int = 3
    max_generations: int = 3


class ExecutionConfig(BaseModel):
    concurrency: int = 3
    sequential: bool = False  # R25: deterministic one-at-a-time first-debug mode
    permission_mode: str = "acceptEdits"
    # Spec rewrites per group before it fails. The plan bounds respawns via the
    # generation cap but leaves the rewrite loop bound to implementation (U7).
    max_rewrites: int = 2


class OrchestratorConfig(BaseModel):
    edge_weights: EdgeWeightsConfig = Field(default_factory=EdgeWeightsConfig)
    partition: PartitionConfig = Field(default_factory=PartitionConfig)
    estimator: EstimatorConfig = Field(default_factory=EstimatorConfig)
    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


def load_config(path: Path | None = None) -> OrchestratorConfig:
    """Load config from a TOML file; every field falls back to its default.

    ``path=None`` or a missing default file yields pure defaults. U9 resolves the
    conventional location (``.orchestrator/config.toml`` in the target repo).
    """
    if path is None or not path.is_file():
        return OrchestratorConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return OrchestratorConfig.model_validate(data)
