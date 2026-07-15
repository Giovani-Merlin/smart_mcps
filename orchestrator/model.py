"""Typed contracts shared by every stage: groups, manifest, reports, verdicts.

These models are the joinable structure the analyzer contract depends on (origin
R6, R17, R19): groups.json is the grouping engine's output, RunManifest is the
only cross-session join, and the report/verdict schemas are the structured final
messages coder and reviewer sessions must emit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Downstream session titles derived from summaries cap at 120 chars
# (docs/research/infinity-skills-analysis.md); validated here, never truncated later.
SUMMARY_MAX_CHARS = 120


class ReviewIntensity(StrEnum):
    """Review tiers picked from the difficulty score (origin R15)."""

    SELF_VERIFY = "self_verify"
    PAIRED = "paired"
    PAIRED_PLUS = "paired_plus"


class VerificationItem(BaseModel):
    id: str
    description: str
    required: bool = True


class Group(BaseModel):
    """One execution group (origin R6 field contract)."""

    model_config = ConfigDict(validate_assignment=True)

    id: str
    name: str
    summary: str = Field(max_length=SUMMARY_MAX_CHARS)
    spec: str
    difficulty: float = Field(ge=0.0, le=1.0)
    intensity: ReviewIntensity
    dependencies: list[str] = Field(default_factory=list)
    verification: list[VerificationItem] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0


class GroupingResult(BaseModel):
    """The grouping engine's full output — the content of groups.json (plan U4)."""

    plan_path: str
    groups: list[Group]
    flags: list[str] = Field(default_factory=list)  # dropped mappings, budget warnings


class SessionRole(StrEnum):
    BASE = "base"
    CODER = "coder"
    REVIEWER = "reviewer"


class SessionEntry(BaseModel):
    """One claude CLI session; a group accumulates one entry per generation/role."""

    session_id: str
    role: SessionRole
    generation: int = 1
    name: str = ""  # display-name convention: <run_id>-<group_id>-<role>-g<generation>
    retirement_reason: str | None = None
    transcript_path: str | None = None


class GroupManifestEntry(BaseModel):
    group_id: str
    group_name: str
    summary: str = Field(max_length=SUMMARY_MAX_CHARS)
    sessions: list[SessionEntry] = Field(default_factory=list)


class RunManifest(BaseModel):
    """The analyzer's only cross-session join (origin R17): run → groups → sessions."""

    run_id: str
    plan_path: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    base_session_id: str | None = None
    groups: dict[str, GroupManifestEntry] = Field(default_factory=dict)


class Surprise(BaseModel):
    """A finding that may invalidate other groups' specs (origin R12)."""

    kind: Literal["interface_mismatch", "missing_dependency", "merge_conflict", "other"]
    description: str
    affected_groups: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    item_id: str
    status: Literal["pass", "fail", "skipped"]
    notes: str = ""


class CoderReport(BaseModel):
    """Structured final message of every coder round (origin R11, R19)."""

    status: Literal["completed", "blocked", "failed"]
    summary: str = ""
    verification_results: list[VerificationResult] = Field(default_factory=list)
    surprises: list[Surprise] = Field(default_factory=list)


class ReviewerVerdict(BaseModel):
    """Structured final message of every reviewer round (origin R13, R16)."""

    status: Literal["approved", "changes_required", "too_hard", "structural"]
    required_changes: list[str] = Field(default_factory=list)
    surprises: list[Surprise] = Field(default_factory=list)
    notes: str = ""
