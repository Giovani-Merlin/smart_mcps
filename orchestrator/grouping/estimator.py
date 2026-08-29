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
from pathlib import Path

from orchestrator.config import DifficultyConfig, EstimatorConfig, OrchestratorConfig
from orchestrator.grouping.base_context import compile_base_context
from orchestrator.grouping.graphing import TaskMapping, source_bytes_of
from orchestrator.grouping.plan_reader import TaskMapError, parse_task_map_for_pricing
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
class TaskPrice:
    """One task's priced work (plan U7), both scales named per U8's vocabulary."""

    task_id: str
    slice: str | None
    node_work: float
    coder_work: float


@dataclass(frozen=True)
class SlicePrice:
    """A declared slice's (or an unlabeled task's own singleton atom's) summed
    work against the cap. ``group --price`` has no graph, so it cannot detect
    hub-isolated atoms the way the real partitioner's ``slice_atoms`` does —
    only the plan's own ``slice:`` labels are priced as groups; every other
    task prices alone."""

    label: str
    tasks: tuple[str, ...]
    node_work: float
    coder_work: float
    over_cap: bool


@dataclass(frozen=True)
class PriceReport:
    """``group --price`` output (plan U7/C3): every task's node work, every
    slice's summed work against the cap, and the resolved budget parameters
    that produced it — all without a graph build or a codegraph client.
    """

    tasks: tuple[TaskPrice, ...]
    slices: tuple[SlicePrice, ...]
    token_budget: int
    bytes_per_token: float
    slack_multiplier: float
    coder_slack_multiplier: float
    per_file_tool_allowance: int
    base_tokens: int
    head: float
    budget_cap: float

    @property
    def over_cap(self) -> bool:
        return any(slice_price.over_cap for slice_price in self.slices)


# --price compiles its cap estimate with an empty codegraph summary (plan
# decision, 2026-08-28): compile_base_context's "Codebase architecture" section
# is the only piece that needs a live index, and skipping it keeps --price
# sub-second at the cost of a cap that is only approximate — stated here so
# every caller repeats the same wording rather than inventing their own.
PRICE_CAP_APPROXIMATION_NOTE = (
    "cap is approximate: compiled with an empty codegraph summary (no codegraph "
    "client, no graph build) to stay sub-second — the real cap from `group` "
    "typically lands within a few thousand tokens of this figure"
)


def price_task_mappings(
    mappings: list[TaskMapping],
    repo_root: Path,
    base_tokens: int,
    config: EstimatorConfig,
) -> PriceReport:
    """Price every task mapping from working-tree byte counts (plan U7), with no
    graph and no codegraph client — ``source_bytes_of`` and ``node_work`` both
    read only the filesystem and plain config numbers.
    """
    task_prices: dict[str, TaskPrice] = {}
    for mapping in mappings:
        metadata = {
            "source_bytes": source_bytes_of(repo_root, mapping.files),
            "files": mapping.files,
            "prospective_files": mapping.prospective_files,
            "size_hints": dict(mapping.size_hints),
        }
        coder_work = node_work(metadata, config)
        task_prices[mapping.task_id] = TaskPrice(
            task_id=mapping.task_id,
            slice=mapping.slice,
            node_work=coder_work / config.coder_slack_multiplier,
            coder_work=coder_work,
        )

    budget_cap = partition_budget_cap(base_tokens, config)
    atoms: dict[str, list[str]] = {}
    for mapping in mappings:
        label = mapping.slice or mapping.task_id
        atoms.setdefault(label, []).append(mapping.task_id)

    slice_prices = []
    for label, members in sorted(atoms.items()):
        coder_work = sum(task_prices[m].coder_work for m in members)
        node_work_sum = sum(task_prices[m].node_work for m in members)
        slice_prices.append(
            SlicePrice(
                label=label,
                tasks=tuple(sorted(members)),
                node_work=node_work_sum,
                coder_work=coder_work,
                over_cap=coder_work > budget_cap,
            )
        )

    head = (
        (base_tokens + config.spec_tokens_allowance)
        * config.slack_multiplier
        * config.coder_slack_multiplier
    )
    return PriceReport(
        tasks=tuple(task_prices[m.task_id] for m in sorted(mappings, key=lambda m: m.task_id)),
        slices=tuple(slice_prices),
        token_budget=config.token_budget,
        bytes_per_token=config.bytes_per_token,
        slack_multiplier=config.slack_multiplier,
        coder_slack_multiplier=config.coder_slack_multiplier,
        per_file_tool_allowance=config.per_file_tool_allowance,
        base_tokens=base_tokens,
        head=head,
        budget_cap=budget_cap,
    )


def price_plan(plan_path: Path, repo_root: Path, config: OrchestratorConfig) -> PriceReport:
    """``group --price <plan>`` (plan U7/C3): parses the task map and prices it
    sub-second, with zero graph build and zero codegraph client — the whole
    point of this mode. Raises ``TaskMapError`` if the plan carries no
    embedded task map (this mode has no LLM-mapper fallback to fall back to).
    """
    plan_text = plan_path.read_text()
    mappings = parse_task_map_for_pricing(plan_text, repo_root)
    if mappings is None:
        raise TaskMapError(
            "plan has no embedded orchestrator-task-map block — --price requires a "
            "task-mapped plan (see docs/orchestrator-task-map.md); the LLM mapper "
            "has no sub-second, zero-codegraph equivalent"
        )
    base_context = compile_base_context(repo_root, plan_path, "")
    base_tokens = int(len(base_context) / config.estimator.bytes_per_token)
    return price_task_mappings(mappings, repo_root, base_tokens, config.estimator)


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
