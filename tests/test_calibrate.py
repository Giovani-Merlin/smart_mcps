"""Estimator calibration: predicted vs actual, and the multiplier it implies."""

from __future__ import annotations

import json

import pytest

from orchestrator.config import EstimatorConfig
from orchestrator.execution.calibrate import (
    RunCalibration,
    calibrate_run,
    format_calibration,
)
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.grouping.estimator import (
    estimate_group_tokens,
    node_work,
    partition_budget_cap,
)
from orchestrator.model import (
    GroupManifestEntry,
    RunManifest,
    SessionEntry,
    SessionRole,
)


# ------------------------------------------------------- the multiplier itself


def test_coder_multiplier_scales_the_group_estimate() -> None:
    """The read-cost formula is unchanged; the coder figure is it, scaled."""
    read_only = EstimatorConfig(coder_slack_multiplier=1.0)
    scaled = EstimatorConfig(coder_slack_multiplier=2.5)
    args = dict(source_bytes=40_000, file_count=5, spec_tokens=3_000, base_tokens=10_000)
    assert estimate_group_tokens(**args, config=scaled) == pytest.approx(
        estimate_group_tokens(**args, config=read_only) * 2.5, rel=1e-6
    )


def test_coder_multiplier_shrinks_the_partition_cap() -> None:
    """Direction check: a larger multiplier must make groups SMALLER.

    Both sides move — node work scales up and the cap shrinks, because the head
    is scaled too — so the number of nodes that fit falls twice over. This is
    the whole point of the knob and the easiest thing to get backwards.
    """
    read_only = EstimatorConfig(token_budget=200_000, coder_slack_multiplier=1.0)
    scaled = EstimatorConfig(token_budget=200_000, coder_slack_multiplier=2.5)
    metadata = {"source_bytes": 20_000, "files": ("a.py", "b.py")}

    assert node_work(metadata, scaled) > node_work(metadata, read_only)
    assert partition_budget_cap(10_000, scaled) < partition_budget_cap(10_000, read_only)

    fits_read = partition_budget_cap(10_000, read_only) / node_work(metadata, read_only)
    fits_scaled = partition_budget_cap(10_000, scaled) / node_work(metadata, scaled)
    assert fits_scaled < fits_read


def test_default_multiplier_is_the_measured_one() -> None:
    """Guards the constant the write-up justifies; changing it is deliberate."""
    assert EstimatorConfig().coder_slack_multiplier == 2.5


# ------------------------------------------------------------- reading a run


def _seed_run(
    tmp_path,
    *,
    estimates: dict[str, int],
    sessions: dict[str, list[tuple]],
    grouped_at: float | None = None,
) -> RunPaths:
    paths = RunPaths(tmp_path, "r-test")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.state_path.write_text("{}")
    if grouped_at is not None:
        (paths.run_dir / "grouping-trace.json").write_text(
            json.dumps({"config": {"estimator": {"coder_slack_multiplier": grouped_at}}})
        )
    (paths.run_dir / "groups.json").write_text(
        json.dumps(
            {"groups": [{"id": gid, "estimated_tokens": est} for gid, est in estimates.items()]}
        )
    )
    manifest = RunManifest(run_id="r-test", plan_path="plan.md")
    for gid, entries in sessions.items():
        manifest.groups[gid] = GroupManifestEntry(
            group_id=gid,
            group_name=gid,
            summary="",
            sessions=[
                SessionEntry(session_id=f"{gid}-{i}", role=role, last_context_tokens=tokens)
                for i, (role, tokens) in enumerate(entries)
            ],
        )
    ManifestStore(paths).save(manifest)
    return paths


def test_calibrate_reports_ratios_per_group(tmp_path) -> None:
    paths = _seed_run(
        tmp_path,
        estimates={"g1": 100_000},
        sessions={"g1": [(SessionRole.CODER, 250_000), (SessionRole.REVIEWER, 110_000)]},
    )
    row = calibrate_run(paths, 2.5).groups[0]
    assert row.coder_ratio == pytest.approx(2.5)
    assert row.reviewer_ratio == pytest.approx(1.1)


def test_peak_coder_session_wins_over_the_replacement(tmp_path) -> None:
    """A retired generation is the observation that matters — a smaller,
    fresher generation must not hide it."""
    paths = _seed_run(
        tmp_path,
        estimates={"g1": 100_000},
        sessions={"g1": [(SessionRole.CODER, 320_000), (SessionRole.CODER, 108_000)]},
    )
    assert calibrate_run(paths, 2.5).groups[0].coder_tokens == 320_000


def test_group_without_recorded_usage_is_skipped_not_counted(tmp_path) -> None:
    """Absent usage must not read as a perfect prediction and drag the median."""
    paths = _seed_run(
        tmp_path,
        estimates={"g1": 100_000, "g2": 100_000},
        sessions={"g1": [(SessionRole.CODER, 300_000)]},
    )
    calibration = calibrate_run(paths, 2.5)
    assert calibration.coder_ratios == [pytest.approx(3.0)]
    assert calibration.observed_multiplier == pytest.approx(3.0)


def test_observed_multiplier_is_a_median_not_a_mean() -> None:
    """One thrashing group must not set the constant sizing every future group."""
    from orchestrator.execution.calibrate import GroupCalibration

    rows = [
        GroupCalibration(f"g{i}", 100_000, coder, 0)
        for i, coder in enumerate([100_000, 110_000, 900_000])
    ]
    assert RunCalibration(rows, 2.5).observed_multiplier == pytest.approx(1.1)


def test_missing_manifest_yields_no_multiplier(tmp_path) -> None:
    paths = _seed_run(tmp_path, estimates={"g1": 100_000}, sessions={})
    calibration = calibrate_run(paths, 2.5)
    assert calibration.observed_multiplier is None
    assert "nothing to calibrate" in format_calibration("r-test", calibration)


# ------------------------------------------------ which multiplier applies


def test_implied_uses_the_grouping_time_multiplier_not_the_current_one(tmp_path) -> None:
    """A run grouped before the knob existed has raw read-cost estimates.

    Scaling its ratio by today's config compounds two eras: a 3.26x overshoot on
    an unscaled estimate implies 3.26, not 2.5 x 3.26 = 8.15. Getting this wrong
    recommends a wildly inflated multiplier from perfectly ordinary data.
    """
    paths = _seed_run(
        tmp_path,
        estimates={"g1": 100_000},
        sessions={"g1": [(SessionRole.CODER, 326_000)]},
    )  # no grouping trace at all -> pre-multiplier run
    calibration = calibrate_run(paths, configured_multiplier=2.5)
    assert calibration.grouping_multiplier == 1.0
    assert calibration.implied_multiplier == pytest.approx(3.26)

    report = format_calibration("r-test", calibration)
    assert "8.15" not in report
    assert "predate the current setting" in report


def test_implied_compounds_when_the_run_was_already_scaled(tmp_path) -> None:
    """Grouped at 2.0 and still 1.5x over means it needed 3.0."""
    paths = _seed_run(
        tmp_path,
        estimates={"g1": 100_000},
        sessions={"g1": [(SessionRole.CODER, 150_000)]},
        grouped_at=2.0,
    )
    calibration = calibrate_run(paths, configured_multiplier=2.0)
    assert calibration.implied_multiplier == pytest.approx(3.0)
    # Config matches grouping, so there is no era mismatch to warn about.
    assert "predate the current setting" not in format_calibration("r-test", calibration)


# --------------------------------------------------------------- the report


@pytest.mark.parametrize(
    ("coder_tokens", "expected"),
    [
        (300_000, "wants to be nearer"),
        (100_000, "leave it alone"),
        (50_000, "size them larger"),
    ],
)
def test_report_recommends_by_direction(tmp_path, coder_tokens: int, expected: str) -> None:
    paths = _seed_run(
        tmp_path,
        estimates={"g1": 100_000},
        sessions={"g1": [(SessionRole.CODER, coder_tokens)]},
    )
    assert expected in format_calibration("r-test", calibrate_run(paths, 2.5))
