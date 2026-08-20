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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.config import EscalationConfig, UsageLimitConfig

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
    # Latest-round context size, persisted every round (R5): in-memory usage dies
    # with the process, and re-entry needs a pre-check against the breaker limit
    # before warm-resuming an interrupted coder.
    last_context_tokens: int = 0
    # Cumulative spend, persisted alongside the context size on the same saves.
    # Distinct from last_context_tokens, which is occupancy of the latest round:
    # these sum every round of the session, so a group's actual cost can be
    # compared against the grouper's per-group `estimated_tokens` prediction and
    # split per role. All default to 0 — runs recorded before this field existed
    # load unchanged and read as "actuals not recorded".
    rounds_completed: int = 0
    total_input_tokens: int = 0  # uncached input only
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


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
    grouping: str | None = None  # named grouping this run snapshotted (plan U10)
    # Persisted at run time so `resume` restores the original run's HITL tier
    # instead of silently reverting to EscalationConfig()'s on_stuck default
    # (plan U2). None on manifests written before this field existed.
    escalation: EscalationConfig | None = None
    # Persisted for the same reason as `escalation`, and against the same trap:
    # an omitted `--auto-resume` on `resume` must restore what the run started
    # with, not silently revert to the library default. None on manifests
    # written before this field existed.
    usage_limit: UsageLimitConfig | None = None
    # The branch `run` was launched from, resolved once at run start (plan U8,
    # R29): `IntegrationMerger`'s `launch_ref` defaults to `HEAD`, a commit, so
    # `finish`'s PR base has to be persisted separately rather than re-derived
    # from a moving HEAD. None for a detached-HEAD launch — `finish` then still
    # pushes the integration branch but skips opening a PR.
    launch_branch: str | None = None
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


#: Cap on the verbatim error a coder may quote in `denial_error`. Generous enough
#: for a stack trace or a build tail, small enough that a runaway log cannot bloat
#: every report artifact on disk.
DENIAL_ERROR_MAX_CHARS = 2000


class CoderReport(BaseModel):
    """Structured final message of every coder round (origin R11, R19).

    ``needs_input`` is the coder-question channel (plan Phase D): a coder that
    cannot proceed without a human decision ends its turn with this status and a
    non-empty ``question``; the orchestrator escalates and resumes it with the
    answer. Headless ``claude -p`` workers cannot pause mid-turn to ask, so the
    only supported channel is report-then-resume (docs/research/design-deviations.md).

    ``permission_denied`` is the typed denial channel (plan U3): a coder that hit
    a permission denial after exhausting its identical-retry budget ends its turn
    with this status and the verbatim ``denied_command``. This is deliberately not
    a ``blocked`` report discriminated by field emptiness — that would make an
    unrelated blocked report's envelope classification depend on whether a coder
    happened to leave a field blank — and deliberately not a ``Surprise``, since a
    denial names no other group.

    ``denial_error``/``denial_source`` (plan P2) are what make one status
    *attributable*. Three unrelated causes used to produce the same opaque
    report — the operator's allowlist lacking a rule, Landlock denying a write, and
    a genuinely forbidden command — and the last validation misdiagnosed one of
    them with the source open. The status stays single by decision; the
    orchestrator classifies a `DenialKind` from these fields
    (``execution/denial.py``). ``denial_source`` earns its place by encoding the
    one thing the model knows for free and the orchestrator cannot recover: whether
    the *harness refused the call* or the *command ran and hit EACCES*.
    """

    status: Literal["completed", "blocked", "failed", "needs_input", "permission_denied"]
    summary: str = ""
    question: str = ""  # required when status == "needs_input"
    denied_command: str = ""  # required when status == "permission_denied"
    # The observed error, verbatim. Optional and *truncating*, never raising: a
    # raising validator here would cost a re-nudge round (`nudge_until_report`)
    # precisely when the worker is already blocked, and `denied_command`'s existing
    # validator is left untouched so every `report-g*-r*.json` already on disk
    # stays parseable.
    denial_error: str = ""
    denial_source: Literal["", "tool_refused", "command_error"] = ""
    verification_results: list[VerificationResult] = Field(default_factory=list)
    surprises: list[Surprise] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_input_requires_question(self) -> CoderReport:
        if self.status == "needs_input" and not self.question.strip():
            raise ValueError("status 'needs_input' requires a non-empty 'question'")
        return self

    @model_validator(mode="after")
    def _permission_denied_requires_command(self) -> CoderReport:
        if self.status == "permission_denied" and not self.denied_command.strip():
            raise ValueError("status 'permission_denied' requires a non-empty 'denied_command'")
        return self

    @model_validator(mode="after")
    def _truncate_denial_error(self) -> CoderReport:
        """Truncate, never reject.

        A model quoting a build log verbatim can produce a very long field, and the
        remedy for that is not to fail its report: rejecting costs a re-nudge round
        exactly when the worker is already blocked, and the classifier only needs
        the first lines. The head is where errno signatures and refusal wording
        appear.
        """
        if len(self.denial_error) > DENIAL_ERROR_MAX_CHARS:
            head = self.denial_error[:DENIAL_ERROR_MAX_CHARS].rstrip()
            object.__setattr__(self, "denial_error", f"{head}… [truncated]")
        return self


class ReviewerVerdict(BaseModel):
    """Structured final message of every reviewer round (origin R13, R16)."""

    status: Literal["approved", "changes_required", "too_hard", "structural"]
    required_changes: list[str] = Field(default_factory=list)
    surprises: list[Surprise] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------- escalation (Phase D)


class EscalationKind(StrEnum):
    """Every hard moment a run can escalate to the operator (plan Phase D).

    The first six are *decision* points (a rewrite/respawn/fail would otherwise
    happen autonomously); the last three are *approval gates* only the
    ``interactive`` tier raises before an otherwise-automatic step.
    """

    CODER_QUESTION = "coder_question"  # coder emitted needs_input + a question
    CODER_BLOCKED = "coder_blocked"  # coder reported blocked/failed
    REVIEWER_TOO_HARD = "reviewer_too_hard"
    REVIEWER_STRUCTURAL = "reviewer_structural"
    MERGE_CONFLICT = "merge_conflict"
    CAPS_EXHAUSTED = "caps_exhausted"  # generation/rewrite cap about to FAIL the group
    GROUP_RESOLVE = "group_resolve"  # FAILED group's stranded work needs resolving (plan U2)
    GROUP_START = "group_start"  # interactive: approve before launch
    RESPAWN = "respawn"  # interactive: approve a breaker respawn
    MERGE_APPROVE = "merge_approve"  # interactive: approve before merge


class HumanAction(StrEnum):
    """What the operator decides for an escalation (plan Phase D v1 action set)."""

    ANSWER = "answer"  # free-text guidance: resume/rewrite guided by it, or "proceed"
    SKIP = "skip"  # fail this group; dependents strand, the run continues
    ABORT = "abort"  # stop the whole run cleanly; state stays resumable


class EscalationContext(BaseModel):
    """Pointers (not payloads) the operator opens to decide — the orchestrator's
    ferry-control-not-content rule extended to the human channel."""

    report_path: str | None = None
    verdict_path: str | None = None
    diff_summary: str = ""
    surprises: list[Surprise] = Field(default_factory=list)


class EscalationRequest(BaseModel):
    """One curated question on the run's single human channel (plan Phase D:
    ``workers_via_orchestrator``). Written as ``escalations/request-<id>.json``."""

    id: str
    run_id: str
    group_id: str
    generation: int
    kind: EscalationKind
    prompt: str  # human-readable summary of the decision to make
    context: EscalationContext = Field(default_factory=EscalationContext)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EscalationResponse(BaseModel):
    """The operator's answer, written as ``escalations/response-<id>.json``; the
    blocked group's coroutine picks it up by correlation ``id`` and resumes."""

    id: str
    action: HumanAction
    answer: str = ""
    answered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PermissionDenied(Exception):
    """A coder reported ``permission_denied`` (plan U3): the harness is healthy,
    but a sandboxed command was refused after the coder's identical-retry budget.
    Routes the group to INTERRUPTED, never FAILED — the work is unfinished, not
    wrong, and a plain ``resume`` re-enters the same worktree. Raised directly by
    the review loop (never through its rewrite path), so this costs no rewrite.
    Lives here, not in ``execution/review.py``, so ``execution/scheduler.py`` can
    catch it without importing from ``review.py`` (which imports from
    ``scheduler.py``, and a cycle isn't worth it for one exception type).

    The attributed kind travels in ``str(exc)`` as well as on the instance (plan
    P2), which is what gets it all the way to the Observatory for free: the
    scheduler already writes ``f"{type(exc).__name__}: {exc}"`` into
    ``state.json``, so `status` and every UI reading that field gain the kind with
    **zero** schema churn. The keyword attributes are for callers that want the
    parts rather than the sentence.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "",
        denied_command: str = "",
        denial_error: str = "",
        denial_source: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.denied_command = denied_command
        self.denial_error = denial_error
        self.denial_source = denial_source
