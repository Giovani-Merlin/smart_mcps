"""Configuration surface: thresholds, weights, and defaults for every stage.

All values are config-overridable (origin R5: thresholds are configuration, never
hardcoded policy). Defaults must load without any config file present; U9 layers
CLI-flag > config-file > default resolution on top of `load_config`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class EdgeWeightsConfig(BaseModel):
    """Affinity weights for the codegraph signals (plan R3) and the prose fallback.

    ``prose_neighbor`` is not a codegraph signal: it is the affinity a region-less
    task gets toward its plan-order neighbor so unmappable tasks cluster near the
    work they were written next to.

    ``semantic`` weights one matched task-map route-tag edge
    (``implements``/``consumes``, docs/orchestrator-task-map.md); the layer is then
    scaled by ``clamp(Σw_struct / Σw_sem, semantic_floor, semantic_ceil)`` so
    semantics dominate only when the structural layer is near-empty (greenfield)
    and never override real reference edges on edit-heavy plans.
    """

    shared_file: float = 1.0
    call: float = 2.0
    impact: float = 1.5
    prose_neighbor: float = 0.5
    semantic: float = 1.5
    semantic_floor: float = 0.5
    semantic_ceil: float = 3.0


class PartitionConfig(BaseModel):
    hub_threshold: float = 0.4  # CoCoder's live ROLE_THRESHOLD
    louvain_resolution: float = 1.0
    # R5/plan U6: a declared slice whose own summed work exceeds the budget cap
    # is a hard GrouperError by default; this (and --allow-oversized-slice,
    # exactly equivalent) keeps it whole as one flagged group instead.
    allow_oversized_slice: bool = False


class EstimatorConfig(BaseModel):
    """Token-budget estimator knobs (plan U3). Directional; tuned on real plans."""

    token_budget: int = 100_000
    bytes_per_token: float = 4.0
    slack_multiplier: float = 1.3
    per_file_tool_allowance: int = 2_000
    spec_tokens_allowance: int = 3_000  # partition-time stand-in before specs exist
    # Plan U7: a prospective file with a declared size_hints class is priced here
    # instead of per_file_tool_allowance; medium equals today's flat rate by
    # design, so an unhinted prospective file is priced exactly as before.
    size_hint_small: int = 500
    size_hint_medium: int = 2_000
    size_hint_large: int = 5_000


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
    # Serial by default: each group's worktree is cut from the integration tip at
    # its ready→running transition, so one-at-a-time stacks each group on the
    # prior's merged work — no cross-group merge conflicts, and a usage-limit hit
    # costs at most one in-flight group. Raise via `--concurrency N` for throughput
    # when rate-limit pressure is low.
    concurrency: int = 1
    sequential: bool = False  # R25: deterministic one-at-a-time first-debug mode
    permission_mode: str = "acceptEdits"
    # Spec rewrites per group before it fails. The plan bounds respawns via the
    # generation cap but leaves the rewrite loop bound to implementation (U7).
    max_rewrites: int = 2


class SessionConfig(BaseModel):
    """How the run command shells the claude CLI (plan U9).

    ``claude_bin`` accepts a list so tests point it at the stub interpreter
    (``["python", "tests/fake_claude.py"]``); ``transcript_root`` overrides the
    ``~/.claude/projects`` default for the same reason.
    """

    claude_bin: str | list[str] = "claude"
    model: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    transcript_root: str | None = None


class EscalationConfig(BaseModel):
    """Human-in-the-loop escalation surface (plan Phase D).

    ``enabled`` is off by default: an unattended ``run`` stays fully autonomous
    (all 190 pre-Phase-D tests unchanged). When on, the ``intensity`` tier decides
    which hard moments pause for the operator (``autonomous`` < ``on_failure`` <
    ``on_stuck`` < ``interactive``) and ``source`` decides whether a coder's
    ``needs_input`` question reaches the operator (``workers_via_orchestrator``)
    or is downgraded to a blocked-style rewrite (``orchestrator_only``).

    ``timeout_s = None`` blocks indefinitely (the HITL default — a live operator
    is expected); when set, an unanswered escalation falls back per ``on_timeout``.
    """

    enabled: bool = False
    intensity: Literal["autonomous", "on_failure", "on_stuck", "interactive"] = "on_stuck"
    source: Literal["orchestrator_only", "workers_via_orchestrator"] = "workers_via_orchestrator"
    timeout_s: float | None = None
    on_timeout: Literal["autonomous", "skip", "abort"] = "autonomous"
    poll_interval_s: float = 1.0


class OrchestratorConfig(BaseModel):
    edge_weights: EdgeWeightsConfig = Field(default_factory=EdgeWeightsConfig)
    partition: PartitionConfig = Field(default_factory=PartitionConfig)
    estimator: EstimatorConfig = Field(default_factory=EstimatorConfig)
    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    escalation: EscalationConfig = Field(default_factory=EscalationConfig)


def load_config(path: Path | None = None) -> OrchestratorConfig:
    """Load config from a TOML file; every field falls back to its default.

    ``path=None`` or a missing default file yields pure defaults. U9 resolves the
    conventional location (``.orchestrator/config.toml`` in the target repo).
    """
    if path is None or not path.is_file():
        return OrchestratorConfig()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # Raw-TOML detection (R7): pydantic v2 silently ignores unknown keys, so a
    # config still carrying the removed per-round timeout would be dropped
    # without a trace — warn explicitly before validation.
    session = data.get("session")
    if isinstance(session, dict) and "timeout_s" in session:
        print(
            f"warning: {path}: [session] timeout_s is deprecated and ignored — "
            "the per-round timeout was removed (R7)",
            file=sys.stderr,
        )
    return OrchestratorConfig.model_validate(data)
