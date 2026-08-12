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
    # A partition whose cycle repair left a group over the cap is degenerate: the
    # repair collapsed an SCC it could not re-split, so the "groups" are one blob.
    # Hard error by default (this used to be a flag nobody blocked on, so `group`
    # exited 0 with a single 3.8x-over-cap group); this accepts it instead.
    allow_degenerate_partition: bool = False
    # Plan U4: the granularity dial. "independent" enforces both merge_small_groups
    # guards and reproduces today's default partition byte-for-byte; "balanced"
    # drops chain_compatible but still rejects a merge that regresses the
    # simulated makespan; "monolithic" also drops the makespan check. The budget
    # cap, slice must-link and cycle checks stay hard at every level. CLI
    # `--granularity` wins over this when both are set.
    granularity: Literal["independent", "balanced", "monolithic"] = "independent"


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
    """Circuit-breaker thresholds (origin R14; plan Key Technical Decisions).

    ``context_token_limit`` default matches measured reality (plan U7): the
    120k default retired healthy coders whose real occupancy was nowhere near
    it, once the RoundUsage fix (plan context-token P0) made the signal
    accurate.
    """

    context_token_limit: int = 200_000
    max_rounds_per_generation: int = 3
    max_generations: int = 3
    # Plan U3: staged in-round prompts at 70%/90%/100% of context_token_limit,
    # riding the per-turn observer the streaming channel (plan U1) provides —
    # bounds *cost* inside a round, not stuck-ness (that's R7's wall-clock
    # rejection; a token ceiling is a proxy for the former, never the latter).
    # Off by default so an existing run/test is unaffected until it opts in.
    context_ladder_enabled: bool = False


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
    # Warm-resume attempts at the group's own coder session to resolve a merge
    # conflict in place, before falling back to a full spec rewrite (plan U1).
    # Serial-by-default (concurrency=1) makes cross-group conflicts rare, so one
    # attempt from the session that just built the work is the right cost/benefit
    # ahead of the proven (but expensive) rewrite path.
    max_conflict_resolve_attempts: int = 1


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
    # Thinking budget per worker turn. Left unset the CLI picks its own default,
    # which is neither pinned nor visible in any run artifact — and thinking counts
    # as *output* tokens, so it lands squarely on the cost driver measured on run
    # r20260729-correctness (588k output tokens across 342 turns in one round).
    # The CLI's level names map to budgets by the documented convention:
    # medium 4000 / high 10000 / xhigh 31999. Default to medium; raise per-run in
    # config.toml when a group genuinely needs deeper reasoning.
    # NB: `--max-thinking-tokens` is hidden from `claude --help`, so it must never
    # go in REQUIRED_CLI_FLAGS or preflight would reject every real CLI.
    max_thinking_tokens: int | None = 4000
    # Orthogonal to the budget above: `--thinking` gates *whether* a turn thinks
    # (enabled = always, adaptive = the model decides, disabled = never), while
    # max_thinking_tokens caps how far it may go when it does. Measured on one probe
    # (sonnet, same prompt): adaptive 62 output tokens vs enabled 140 vs disabled 253
    # — adaptive was both cheapest and correct, so it is the default. Pairing it with
    # the medium ceiling means "think only when it helps, never more than medium".
    thinking: str | None = "adaptive"
    # Plan U2: kernel-enforced confinement via Landlock, layered under the
    # deny-rules below rather than instead of them (deny-rules give a clearer
    # error for the accidental case; Landlock is the actual boundary).
    #
    # On by default. It shipped off, on the reasoning that a run should opt in —
    # but the CLI then never passed it at all, so the whole mechanism sat dead
    # for a release while the P0 it closes (workers editing the operator's
    # auto-memory) stayed open. An opt-in boundary that nothing opts into is not
    # a boundary. Defaulting on is safe because absence degrades to a warning
    # and deny-rules rather than failing a group.
    confine: bool = True
    # --disallowedTools patterns (e.g. the denied git subcommands from
    # worktrees.denied_git_tool_patterns()) and an optional --settings path or
    # inline JSON string. Empty/None means the flag is omitted entirely.
    disallowed_tools: list[str] = Field(default_factory=list)
    settings: str | None = None


class EscalationConfig(BaseModel):
    """Human-in-the-loop escalation surface (plan Phase D).

    ``enabled`` is on by default (plan U2): a group ending failed or interrupted
    must never let an overlapping successor start silently, and that gate needs
    an operator channel to be meaningful by default. When on, the ``intensity``
    tier decides which hard moments pause for the operator (``autonomous`` <
    ``on_failure`` < ``on_stuck`` < ``interactive``) and ``source`` decides
    whether a coder's ``needs_input`` question reaches the operator
    (``workers_via_orchestrator``) or is downgraded to a blocked-style rewrite
    (``orchestrator_only``). ``--intensity autonomous`` (or ``[escalation]
    intensity = "autonomous"``) forces this back off for an unattended run.

    ``timeout_s = None`` blocks indefinitely (the HITL default — a live operator
    is expected); when set, an unanswered escalation falls back per ``on_timeout``.
    """

    enabled: bool = True
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
