"""Token-budget estimator and difficulty score (plan U3).

Formulas are directional (plan: "defaults tuned during implementation against real
plans"): the estimator answers "does this group's context fit the budget?", the
difficulty score answers "how much review does it deserve?" (origin R4, R5, R15).
Both read only plain numbers, so the partition strategy can consume them as
injected hooks without importing this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator.config import DifficultyConfig, EstimatorConfig
from orchestrator.model import ReviewIntensity


def estimate_group_tokens(
    source_bytes: int,
    file_count: int,
    spec_tokens: int,
    base_tokens: int,
    config: EstimatorConfig,
) -> int:
    """Plan U3 formula: (base head + spec + source bytes/token ratio) × slack,
    plus a flat per-file tool-output allowance — then scaled to a coder.

    The formula above is a *read*-cost model, validated against reviewers
    (0.90x-1.35x of estimate). A coder iterates, so its real context runs
    1.56x-3.83x higher; ``coder_slack_multiplier`` converts read cost into the
    predicted coder peak that ``token_budget`` is actually guarding.
    """
    core = base_tokens + spec_tokens + source_bytes / config.bytes_per_token
    read_cost = core * config.slack_multiplier + file_count * config.per_file_tool_allowance
    return int(read_cost * config.coder_slack_multiplier)


def is_over_budget(estimated_tokens: int, config: EstimatorConfig) -> bool:
    return estimated_tokens > config.token_budget


# size_hints class name -> the EstimatorConfig field pricing it (plan U7).
_SIZE_HINT_FIELDS = {
    "small": "size_hint_small",
    "medium": "size_hint_medium",
    "large": "size_hint_large",
}


def node_work(metadata: Mapping[str, object], config: EstimatorConfig) -> float:
    """Per-task work in tokens — the hook injected into the partition strategy.

    Uses the metadata shape the codegraph adapter emits (source_bytes, files).
    Prospective files contribute zero source bytes but count in the per-file
    allowance — they will exist by the time the group runs, and pricing them at
    zero would let merge_small_groups over-merge tiny greenfield groups. A
    prospective file named in ``size_hints`` (path -> small/medium/large) is
    priced by that class instead of the flat allowance; existing-file pricing
    is untouched.
    """
    source_bytes = int(metadata.get("source_bytes", 0) or 0)
    existing_files = metadata.get("files", ()) or ()
    prospective_files = metadata.get("prospective_files", ()) or ()
    size_hints = metadata.get("size_hints") or {}
    tokens = source_bytes / config.bytes_per_token * config.slack_multiplier
    tokens += len(existing_files) * config.per_file_tool_allowance
    for file in prospective_files:
        hint = size_hints.get(file)
        if hint:
            tokens += getattr(config, _SIZE_HINT_FIELDS[hint])
        else:
            tokens += config.per_file_tool_allowance
    # Scaled to a coder for the same reason as ``estimate_group_tokens``, and by
    # the same factor, so partition-time sizing and the final estimate agree.
    return tokens * config.coder_slack_multiplier


def partition_budget_cap(base_tokens: int, config: EstimatorConfig) -> float:
    """Budget available to a group's summed node work, after the fixed head.

    The base head and spec allowance are per-group constants, so the cap the
    partitioner enforces on summed node work is the budget minus that slacked head.

    The head is scaled to a coder alongside ``node_work``: both sides of the
    comparison must be in coder tokens, or the cap would be enforced against
    read-cost work using a coder-cost budget.
    """
    head = (
        (base_tokens + config.spec_tokens_allowance)
        * config.slack_multiplier
        * config.coder_slack_multiplier
    )
    return max(config.token_budget - head, 0.0)


@dataclass(frozen=True)
class DifficultySignals:
    """Raw signals per candidate group (plan U3; sources: codegraph metadata)."""

    files_touched: int = 0
    max_fan_in: int = 0
    max_fan_out: int = 0
    hub_touches: int = 0
    cross_group_edges: int = 0
    verification_items: int = 0


def _saturating(value: float, scale: float) -> float:
    """x / (x + scale): 0 at 0, 0.5 at the scale point, asymptotically 1."""
    if value <= 0:
        return 0.0
    return value / (value + scale)


def difficulty_score(signals: DifficultySignals, config: DifficultyConfig) -> float:
    """Normalized weighted sum in [0, 1)."""
    weighted = [
        (
            config.weight_files_touched,
            _saturating(signals.files_touched, config.scale_files_touched),
        ),
        (
            config.weight_max_fan,
            _saturating(max(signals.max_fan_in, signals.max_fan_out), config.scale_max_fan),
        ),
        (config.weight_hub_touches, _saturating(signals.hub_touches, config.scale_hub_touches)),
        (
            config.weight_cross_group_edges,
            _saturating(signals.cross_group_edges, config.scale_cross_group_edges),
        ),
        (
            config.weight_verification_items,
            _saturating(signals.verification_items, config.scale_verification_items),
        ),
    ]
    total_weight = sum(weight for weight, _ in weighted)
    if total_weight == 0:
        return 0.0
    return sum(weight * norm for weight, norm in weighted) / total_weight


def intensity_for(difficulty: float, config: DifficultyConfig) -> ReviewIntensity:
    """Difficulty → review tier (origin R15, AE7): the dial lives in data."""
    if difficulty < config.d_review:
        return ReviewIntensity.SELF_VERIFY
    if difficulty < config.d_hard:
        return ReviewIntensity.PAIRED
    return ReviewIntensity.PAIRED_PLUS
