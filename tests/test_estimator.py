"""Tests for orchestrator/grouping/estimator.py — token budget and difficulty."""

from orchestrator.config import DifficultyConfig, EstimatorConfig
from orchestrator.grouping.estimator import (
    DifficultySignals,
    difficulty_score,
    estimate_group_tokens,
    intensity_for,
    is_over_budget,
    node_work,
    partition_budget_cap,
)
from orchestrator.model import ReviewIntensity


class TestTokenEstimate:
    def test_over_budget_group_is_flagged(self):
        """Plan U3 scenario: estimator flags a group over budget."""
        config = EstimatorConfig()
        estimate = estimate_group_tokens(
            source_bytes=4_000_000,
            file_count=30,
            spec_tokens=5_000,
            base_tokens=10_000,
            config=config,
        )
        assert is_over_budget(estimate, config)

    def test_under_budget_group_passes(self):
        config = EstimatorConfig()
        estimate = estimate_group_tokens(
            source_bytes=40_000,
            file_count=3,
            spec_tokens=2_000,
            base_tokens=5_000,
            config=config,
        )
        assert not is_over_budget(estimate, config)

    def test_formula_components(self):
        """(base + spec + bytes/ratio) × slack + files × allowance."""
        config = EstimatorConfig(
            bytes_per_token=4.0, slack_multiplier=2.0, per_file_tool_allowance=100
        )
        estimate = estimate_group_tokens(
            source_bytes=400, file_count=2, spec_tokens=50, base_tokens=50, config=config
        )
        assert estimate == int((50 + 50 + 100) * 2.0 + 200)

    def test_node_work_reads_adapter_metadata_shape(self):
        config = EstimatorConfig(
            bytes_per_token=4.0, slack_multiplier=1.0, per_file_tool_allowance=100
        )
        meta = {"source_bytes": 400, "files": ["a.py", "b.py"]}
        assert node_work(meta, config) == 100 + 200

    def test_node_work_counts_prospective_files_in_allowance(self):
        """Prospective files bring zero bytes but full per-file allowance —
        pricing them at zero would let merging over-merge greenfield groups."""
        config = EstimatorConfig(
            bytes_per_token=4.0, slack_multiplier=1.0, per_file_tool_allowance=100
        )
        meta = {"source_bytes": 0, "files": [], "prospective_files": ["new1.py", "new2.py"]}
        assert node_work(meta, config) == 200

    def test_node_work_prices_hinted_prospective_file_by_class(self):
        """Plan U7: a hinted prospective file is priced by its class, not the
        flat per-file allowance; other files still use their own allowances."""
        config = EstimatorConfig(
            bytes_per_token=4.0,
            slack_multiplier=1.0,
            per_file_tool_allowance=100,
            size_hint_small=500,
            size_hint_medium=2_000,
            size_hint_large=5_000,
        )
        meta = {
            "source_bytes": 0,
            "files": ["existing.py"],
            "prospective_files": ["app/big.py", "app/unhinted.py"],
            "size_hints": {"app/big.py": "large"},
        }
        assert node_work(meta, config) == 100 + 5_000 + 100

    def test_node_work_with_no_size_hints_key_matches_today(self):
        """A metadata dict carrying no ``size_hints`` key produces exactly the
        same node_work as before this change (backward compatibility)."""
        config = EstimatorConfig(
            bytes_per_token=4.0, slack_multiplier=1.0, per_file_tool_allowance=100
        )
        meta = {
            "source_bytes": 400,
            "files": ["a.py", "b.py"],
            "prospective_files": ["new1.py", "new2.py"],
        }
        assert node_work(meta, config) == 100 + 400

    def test_node_work_with_empty_size_hints_matches_no_hints(self):
        config = EstimatorConfig(
            bytes_per_token=4.0, slack_multiplier=1.0, per_file_tool_allowance=100
        )
        meta = {
            "source_bytes": 400,
            "files": ["a.py", "b.py"],
            "prospective_files": ["new1.py", "new2.py"],
            "size_hints": {},
        }
        assert node_work(meta, config) == 100 + 400

    def test_estimate_group_tokens_unchanged_for_group_with_no_hinted_files(self):
        """size_hints only affects node_work; estimate_group_tokens (the
        post-partition group estimate) keeps its pre-existing flat formula."""
        config = EstimatorConfig(
            bytes_per_token=4.0, slack_multiplier=2.0, per_file_tool_allowance=100
        )
        estimate = estimate_group_tokens(
            source_bytes=400, file_count=2, spec_tokens=50, base_tokens=50, config=config
        )
        assert estimate == int((50 + 50 + 100) * 2.0 + 200)

    def test_partition_budget_cap_subtracts_slacked_head(self):
        config = EstimatorConfig(
            token_budget=100_000, slack_multiplier=1.0, spec_tokens_allowance=3_000
        )
        assert partition_budget_cap(base_tokens=7_000, config=config) == 90_000

    def test_partition_budget_cap_never_negative(self):
        config = EstimatorConfig(token_budget=1_000)
        assert partition_budget_cap(base_tokens=10_000_000, config=config) == 0.0


class TestDifficulty:
    def test_hard_group_scores_above_easy_group(self):
        """Plan U3 scenario: many hubs and wide impact order above a trivial group."""
        config = DifficultyConfig()
        easy = DifficultySignals(files_touched=1, verification_items=1)
        hard = DifficultySignals(
            files_touched=15,
            max_fan_in=40,
            max_fan_out=25,
            hub_touches=3,
            cross_group_edges=6,
            verification_items=12,
        )
        assert difficulty_score(hard, config) > difficulty_score(easy, config)

    def test_score_stays_in_unit_interval(self):
        config = DifficultyConfig()
        extreme = DifficultySignals(
            files_touched=10_000,
            max_fan_in=10_000,
            max_fan_out=10_000,
            hub_touches=10_000,
            cross_group_edges=10_000,
            verification_items=10_000,
        )
        assert 0.0 <= difficulty_score(DifficultySignals(), config) < 1.0
        assert 0.0 <= difficulty_score(extreme, config) < 1.0

    def test_ae7_low_difficulty_maps_to_self_verify(self):
        """AE7 (mapping half): below d_review → self-verify, no reviewer."""
        config = DifficultyConfig(d_review=0.35, d_hard=0.65)
        assert intensity_for(0.1, config) is ReviewIntensity.SELF_VERIFY

    def test_ae7_high_difficulty_maps_to_paired_plus(self):
        config = DifficultyConfig(d_review=0.35, d_hard=0.65)
        assert intensity_for(0.9, config) is ReviewIntensity.PAIRED_PLUS

    def test_between_thresholds_maps_to_paired(self):
        config = DifficultyConfig(d_review=0.35, d_hard=0.65)
        assert intensity_for(0.5, config) is ReviewIntensity.PAIRED

    def test_thresholds_come_from_config(self):
        strict = DifficultyConfig(d_review=0.01, d_hard=0.02)
        assert intensity_for(0.1, strict) is ReviewIntensity.PAIRED_PLUS
