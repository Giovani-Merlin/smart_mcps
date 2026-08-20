"""Estimator calibration: what a run predicted vs what it actually cost.

The grouper's token estimate is a *read*-cost model. Reviewers, which read a
group's material roughly once, land close to it; coders iterate and cost far
more. `EstimatorConfig.coder_slack_multiplier` bridges the two, and this module
is how that constant stops being a guess: every finished run already records
both halves — `estimated_tokens` per group in the run's `groups.json`, and
`last_context_tokens` per session in `manifest.json` — but nothing compared them,
so the multiplier could only ever be set from a hand-measured sample.

Reports observed ratios and the multiplier they imply. It never edits config:
one run is a small sample, and the operator decides when enough have accumulated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from statistics import median

from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.model import SessionRole


@dataclass(frozen=True)
class GroupCalibration:
    """One group's prediction against its outcome.

    ``coder_tokens`` is the *peak* across the group's coder sessions rather than
    the last one: a retired generation is exactly the observation that matters,
    and a fresh generation that replaced it would otherwise hide the overshoot.
    """

    group_id: str
    estimated_tokens: int
    coder_tokens: int
    reviewer_tokens: int

    @property
    def coder_ratio(self) -> float | None:
        if not self.estimated_tokens or not self.coder_tokens:
            return None
        return self.coder_tokens / self.estimated_tokens

    @property
    def reviewer_ratio(self) -> float | None:
        if not self.estimated_tokens or not self.reviewer_tokens:
            return None
        return self.reviewer_tokens / self.estimated_tokens


@dataclass(frozen=True)
class RunCalibration:
    """``grouping_multiplier`` is the multiplier that was in effect when this run
    was *grouped*, not the one configured now. They are different numbers as soon
    as anyone tunes the config, and only the grouping-time one explains the
    estimates being compared — a run grouped before the knob existed has raw
    read-cost estimates, and scaling its ratio by today's setting would compound
    two eras and recommend a wildly inflated multiplier.
    """

    groups: list[GroupCalibration]
    configured_multiplier: float
    grouping_multiplier: float = 1.0

    @property
    def coder_ratios(self) -> list[float]:
        return [r for g in self.groups if (r := g.coder_ratio) is not None]

    @property
    def observed_multiplier(self) -> float | None:
        """Median observed coder ratio — the multiplier this run alone implies.

        Median, not mean: one runaway group (a coder that thrashed on a flaky
        test) should not drag the constant that sizes every future group.
        """
        ratios = self.coder_ratios
        return median(ratios) if ratios else None

    @property
    def implied_multiplier(self) -> float | None:
        """What this run says the multiplier should have been.

        The estimates were already scaled by ``grouping_multiplier``, so a run
        that still came in ``observed`` times high needed the product of the two.
        """
        observed = self.observed_multiplier
        return None if observed is None else self.grouping_multiplier * observed


def calibrate_run(paths: RunPaths, configured_multiplier: float) -> RunCalibration:
    """Read a finished run's estimates and actuals. Missing halves are skipped,
    not defaulted — a group with no recorded coder usage is absent from the
    ratios rather than counted as a perfect prediction."""
    # The grouping trace records the whole config as it stood at grouping time.
    # Absent (or pre-dating the knob) means 1.0: raw read-cost estimates.
    grouping_multiplier = 1.0
    trace_path = paths.run_dir / "grouping-trace.json"
    if trace_path.is_file():
        try:
            trace = json.loads(trace_path.read_text())
        except json.JSONDecodeError:
            trace = {}
        estimator_cfg = (trace.get("config") or {}).get("estimator") or {}
        grouping_multiplier = float(estimator_cfg.get("coder_slack_multiplier") or 1.0)

    groups_path = paths.run_dir / "groups.json"
    estimates: dict[str, int] = {}
    if groups_path.is_file():
        payload = json.loads(groups_path.read_text())
        for entry in payload.get("groups") or []:
            gid = entry.get("id")
            if gid:
                estimates[gid] = int(entry.get("estimated_tokens") or 0)

    store = ManifestStore(paths)
    manifest = store.load() if store.exists() else None
    rows: list[GroupCalibration] = []
    for gid in sorted(estimates):
        coder = reviewer = 0
        group_entry = manifest.groups.get(gid) if manifest else None
        for session in (group_entry.sessions if group_entry else ()) or ():
            tokens = session.last_context_tokens or 0
            if session.role == SessionRole.CODER:
                coder = max(coder, tokens)
            elif session.role == SessionRole.REVIEWER:
                reviewer = max(reviewer, tokens)
        rows.append(
            GroupCalibration(
                group_id=gid,
                estimated_tokens=estimates[gid],
                coder_tokens=coder,
                reviewer_tokens=reviewer,
            )
        )
    return RunCalibration(
        groups=rows,
        configured_multiplier=configured_multiplier,
        grouping_multiplier=grouping_multiplier,
    )


def format_calibration(run_id: str, calibration: RunCalibration) -> str:
    """Human-readable report. Ratios are against the estimate as recorded, which
    already includes whatever multiplier was configured when the run was grouped
    — so a ratio near 1.0 means that multiplier was right, not that the raw
    read-cost model was."""
    lines = [f"calibration for run {run_id}", ""]
    lines.append(
        f"{'group':8}{'estimate':>12}{'coder':>12}{'ratio':>8}{'reviewer':>12}{'ratio':>8}"
    )
    for row in calibration.groups:
        coder_ratio = f"{row.coder_ratio:.2f}x" if row.coder_ratio else "-"
        rev_ratio = f"{row.reviewer_ratio:.2f}x" if row.reviewer_ratio else "-"
        lines.append(
            f"{row.group_id:8}{row.estimated_tokens:>12,}{row.coder_tokens:>12,}"
            f"{coder_ratio:>8}{row.reviewer_tokens:>12,}{rev_ratio:>8}"
        )

    observed = calibration.observed_multiplier
    lines.append("")
    if observed is None:
        lines.append("no coder usage recorded — nothing to calibrate against")
        return "\n".join(lines)

    configured = calibration.configured_multiplier
    grouping = calibration.grouping_multiplier
    implied = calibration.implied_multiplier
    assert implied is not None  # observed is not None, so neither is implied
    lines.append(f"coder_slack_multiplier when grouped: {grouping:.2f}")
    lines.append(f"observed (median coder ratio):       {observed:.2f}")
    lines.append(f"implied by this run:                 {implied:.2f}")
    if abs(configured - grouping) > 1e-9:
        lines.append(
            f"  note: config now says {configured:.2f}. This run was grouped at "
            f"{grouping:.2f}, so its estimates predate the current setting and "
            "the implied figure above is the one to compare."
        )
    if observed > 1.15:
        lines.append(
            f"  -> groups ran {observed:.2f}x over their estimates; "
            f"coder_slack_multiplier wants to be nearer {implied:.2f} (smaller groups)"
        )
    elif observed < 0.85:
        lines.append(
            f"  -> groups ran well under estimate; {implied:.2f} would size them "
            "larger and reuse warm context better"
        )
    else:
        lines.append("  -> within 15% of prediction; leave it alone")
    lines.append("")
    lines.append("One run is a small sample. Compare several before changing config.")
    return "\n".join(lines)
