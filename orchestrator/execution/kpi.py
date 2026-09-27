"""The pure KPI contract, noise floor, decision function and Attempt Ledger
(plan U9). Nothing here touches git, a subprocess or an LLM — the ``evaluate``
and ``optimize`` executors are the only callers that do.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.execution.manifest import atomic_write_text

Outcome = Literal["keep", "promising", "inconclusive", "discard", "crash"]
Direction = Literal["min", "max"]

#: newest-last table capped by `render_ledger_table`; the trailer line added
#: when older rows are dropped to make room for it.
_TRAILER_TEMPLATE = "+{n} earlier rows — see ledger.json"


class Guard(BaseModel):
    """A metric that must not regress past ``max_regression`` in its own
    ``direction``, independent of the KPI's own gain."""

    model_config = ConfigDict(extra="forbid")

    key: str
    direction: Direction
    max_regression: float = Field(ge=0.0)


class KpiContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    direction: Direction
    min_effect: float = 0.0
    guards: list[Guard] = Field(default_factory=list)
    harness_paths: list[str] = Field(min_length=1)
    smoke: str | None = None
    good_enough: float | None = None


def signed_delta(candidate: float, champion: float, direction: Direction) -> float:
    """``candidate - champion`` flipped so a positive result is always
    "better", regardless of whether the KPI is minimised or maximised."""
    raw = candidate - champion
    return raw if direction == "max" else -raw


def noise_floor(deltas: Sequence[float]) -> float:
    """Median absolute deviation over the last five deltas; ``0.0`` with
    fewer than two — there is no spread to measure from a single point."""
    window = list(deltas)[-5:]
    if len(window) < 2:
        return 0.0
    center = median(window)
    return median(abs(value - center) for value in window)


def decide(
    delta: float,
    guard_deltas: Sequence[float],
    floor: float,
    contract: KpiContract,
    *,
    crashed: bool = False,
) -> Outcome:
    """Keep-or-revert, per R17.

    ``guard_deltas`` are signed the same way as ``delta`` (positive is
    better) and line up with ``contract.guards`` in order; any guard whose
    delta regresses past its own ``max_regression`` discards the candidate
    even when the KPI itself improved by a wide margin.
    """
    if crashed:
        return "crash"
    for guard, guard_delta in zip(contract.guards, guard_deltas, strict=True):
        if guard_delta < -guard.max_regression:
            return "discard"
    if delta < 0:
        return "discard"
    threshold = max(contract.min_effect, 2 * floor)
    if delta >= threshold:
        return "keep" if floor == 0 else "promising"
    return "inconclusive"


class Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_no: int
    candidate_commit: str
    kpi_value: float | None
    guard_values: dict[str, float] = Field(default_factory=dict)
    delta: float | None
    noise_floor: float
    outcome: Outcome
    harness_hash: str
    why: str
    at: str


class Ledger(BaseModel):
    """Append-only attempt history for one ``optimize`` group, persisted at
    ``<group_dir>/ledger.json``."""

    model_config = ConfigDict(extra="forbid")

    attempts: list[Attempt] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.is_file():
            return cls()
        return cls.model_validate_json(path.read_text())

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, self.model_dump_json(indent=2) + "\n")

    def append(self, path: Path, attempt: Attempt) -> None:
        self.attempts.append(attempt)
        self.save(path)

    def deltas(self) -> list[float]:
        return [a.delta for a in self.attempts if a.delta is not None]

    def champion(self) -> Attempt | None:
        kept = [a for a in self.attempts if a.outcome == "keep"]
        return kept[-1] if kept else None


def _row(attempt: Attempt) -> str:
    kpi = "—" if attempt.kpi_value is None else f"{attempt.kpi_value:g}"
    delta = "—" if attempt.delta is None else f"{attempt.delta:+g}"
    return (
        f"| {attempt.round_no} | {attempt.candidate_commit[:8]} | "
        f"{kpi} | {delta} | {attempt.outcome} |"
    )


def render_ledger_table(ledger: Ledger, max_chars: int = 6000) -> str:
    """Newest-last rows, capped at ``max_chars``; the newest row is always
    kept even if the cap can't be honoured with only it present."""
    header = ["| round | commit | kpi | delta | outcome |", "|---|---|---|---|---|"]
    rows = [_row(a) for a in ledger.attempts]
    dropped = 0
    while True:
        lines = [*header, *rows]
        if dropped:
            lines.append(_TRAILER_TEMPLATE.format(n=dropped))
        text = "\n".join(lines)
        if len(text) <= max_chars or len(rows) <= 1:
            return text
        rows = rows[1:]
        dropped += 1
