"""Tests for orchestrator/model.py and orchestrator/config.py — the shared contracts."""

import pytest
from pydantic import ValidationError

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.model import (
    CoderReport,
    Group,
    GroupingResult,
    GroupManifestEntry,
    ReviewerVerdict,
    ReviewIntensity,
    RunManifest,
    SessionEntry,
    SessionRole,
    Surprise,
    VerificationItem,
)


def make_group(**overrides):
    defaults = dict(
        id="g1",
        name="auth-service",
        summary="Add token refresh to the auth service",
        spec="Full worker-facing spec text.",
        difficulty=0.4,
        intensity=ReviewIntensity.PAIRED,
    )
    return Group(**(defaults | overrides))


class TestGroup:
    def test_round_trips_losslessly(self):
        group = make_group(
            dependencies=["g0"],
            verification=[VerificationItem(id="v1", description="tests pass")],
            tasks=["t1", "t2"],
            files=["a.py"],
            estimated_tokens=42_000,
        )
        assert Group.model_validate_json(group.model_dump_json()) == group

    def test_summary_over_length_bound_is_rejected(self):
        """Downstream session titles cap at 120 chars — reject, never truncate."""
        with pytest.raises(ValidationError, match="summary"):
            make_group(summary="x" * 121)

    def test_difficulty_bounds_enforced(self):
        with pytest.raises(ValidationError):
            make_group(difficulty=1.5)


class TestManifest:
    def test_round_trips_with_generations_and_retirement(self):
        """Plan U3 scenario: multiple generations per group, retirement reasons intact."""
        manifest = RunManifest(
            run_id="run-1",
            plan_path="docs/plans/example.md",
            base_session_id="00000000-0000-0000-0000-000000000000",
            groups={
                "g1": GroupManifestEntry(
                    group_id="g1",
                    group_name="auth-service",
                    summary="Add token refresh",
                    sessions=[
                        SessionEntry(
                            session_id="s-gen1",
                            role=SessionRole.CODER,
                            generation=1,
                            name="run-1-g1-coder-g1",
                            retirement_reason="round threshold exceeded",
                        ),
                        SessionEntry(
                            session_id="s-gen2",
                            role=SessionRole.CODER,
                            generation=2,
                            name="run-1-g1-coder-g2",
                        ),
                        SessionEntry(
                            session_id="s-rev",
                            role=SessionRole.REVIEWER,
                            generation=1,
                            name="run-1-g1-reviewer-g1",
                        ),
                    ],
                )
            },
        )
        restored = RunManifest.model_validate_json(manifest.model_dump_json())
        assert restored == manifest
        sessions = restored.groups["g1"].sessions
        assert [s.generation for s in sessions] == [1, 2, 1]
        assert sessions[0].retirement_reason == "round threshold exceeded"
        assert sessions[1].retirement_reason is None


class TestReportSchemas:
    def test_coder_report_requires_status(self):
        """Plan U3 scenario: schemas reject missing status."""
        with pytest.raises(ValidationError, match="status"):
            CoderReport.model_validate({"summary": "did things"})

    def test_coder_report_rejects_malformed_surprises(self):
        with pytest.raises(ValidationError, match="surprises"):
            CoderReport.model_validate(
                {"status": "completed", "surprises": [{"kind": "not_a_kind"}]}
            )

    def test_verdict_requires_status(self):
        with pytest.raises(ValidationError, match="status"):
            ReviewerVerdict.model_validate({"notes": "looks fine"})

    def test_verdict_rejects_unknown_status(self):
        with pytest.raises(ValidationError, match="status"):
            ReviewerVerdict.model_validate({"status": "maybe"})

    def test_valid_report_round_trips(self):
        report = CoderReport(
            status="completed",
            summary="done",
            surprises=[
                Surprise(
                    kind="interface_mismatch",
                    description="signature changed",
                    affected_groups=["g2"],
                )
            ],
        )
        assert CoderReport.model_validate_json(report.model_dump_json()) == report

    def test_grouping_result_round_trips(self):
        result = GroupingResult(
            plan_path="plan.md", groups=[make_group()], flags=["dropped: ghost_symbol"]
        )
        assert GroupingResult.model_validate_json(result.model_dump_json()) == result


class TestConfig:
    def test_defaults_load_without_config_file(self):
        """Plan U3 verification: config defaults load with no file present."""
        config = load_config(None)
        assert config.estimator.token_budget == 100_000
        assert config.breaker.max_generations == 3
        assert config.execution.concurrency == 3

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        assert load_config(tmp_path / "absent.toml") == OrchestratorConfig()

    def test_toml_overrides_merge_over_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[estimator]\ntoken_budget = 50000\n\n[difficulty]\nd_review = 0.2\n"
        )
        config = load_config(config_file)
        assert config.estimator.token_budget == 50_000
        assert config.difficulty.d_review == 0.2
        # untouched sections keep defaults
        assert config.breaker.context_token_limit == 120_000
