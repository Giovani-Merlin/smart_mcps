"""Review loop: warm coder↔reviewer ferry, circuit breaker, adaptation (plan U7).

The orchestrator ferries control, not content: round triggers carry statuses and
artifact pointers; reports and verdicts persist in the run directory, and the
reviewer computes the diff itself from the shared worktree (plan Key Technical
Decisions). Intensity tiers route the loop: self-verify skips the reviewer
entirely, paired adds one, paired-plus adds a mandatory extra verification pass
(origin R15). The breaker retires a session on token or round thresholds and
respawns a fresh generation from base with a condensed handoff; the generation
cap fails the group to the operator instead of respawning forever (origin R14).
Surprises fan out through the SurpriseBoard: unfinished groups named by a
surprise are rewritten before launch — or instead of merging, when already in
review (origin R12, R16). Completed groups are never rewritten.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.config import (
    BreakerConfig,
    ExecutionConfig,
    LivenessConfig,
    RecipesConfig,
    WorkspaceConfig,
)
from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.escalation import EscalationBroker, EscalationPolicy
from orchestrator.execution.heartbeat import RoundHeartbeat
from orchestrator.execution.liveness import (
    ActivityRegistry,
    ChildActivity,
    LivenessProbe,
    read_suspend_facts,
)
from orchestrator.execution.manifest import (
    ManifestStore,
    log_event,
)
from orchestrator.execution.preflight import (
    PreflightBaseline,
)
from orchestrator.execution.scheduler import (
    Executor,
    GroupContext,
    GroupState,
)
from orchestrator.execution.sessions import SessionRunner
from orchestrator.execution.escalating import EscalationHandlers
from orchestrator.execution.generation import GenerationLoop
from orchestrator.execution.merge_ladder import MergeLadder
from orchestrator.execution.records import SessionRecords
from orchestrator.execution.reviewer import ReviewerRound
from orchestrator.execution.surprises import SurpriseHandling
from orchestrator.model import (
    CoderReport,
    Group,
    RunManifest,
    SessionEntry,
    Surprise,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only, no runtime binding
    from orchestrator.execution.surprises import SurpriseBoard


@dataclass
class ReviewDeps:
    """Everything a group's review loop needs; seams injected so tests script
    sessions in-process and U8/U9 wire worktrees, merges, and the speccer."""

    run_id: str
    runner: SessionRunner
    store: ManifestStore
    manifest: RunManifest
    # The run's compiled base context, prepended to every worker's first prompt
    # (ADR 0007). ``base_session_id`` is the legacy fork path's parent session
    # and is ``None`` unless ``fork_base_session`` is on — with no fork there is
    # no base session to have an id.
    base_context: str
    base_session_id: str | None
    fork_base_session: bool
    breaker: BreakerConfig
    execution: ExecutionConfig
    board: SurpriseBoard
    workspace_for: Callable[[Group], Path]
    merge_group: Callable[[Group, Path], str]  # raises MergeConflict; returns the merge commit sha
    rewrite_spec: Callable[[Group, list[Surprise]], Group]
    base_ref_for: Callable[[Group], str]
    # HITL seam (plan Phase D): both None ⇒ no escalations are ever raised; the
    # lifecycle log stays on regardless (R10).
    broker: EscalationBroker | None = None
    policy: EscalationPolicy | None = None
    # What was already red on the launch branch (plan U2/U3), read back by the
    # merge gate to tell a new failure from a pre-existing one. ``None`` (no
    # baseline captured, or a resumed run with no run directory to read it
    # from) degrades to "no_baseline", which never attributes a failure as
    # new — the same conservative default `compare_to_baseline` documents.
    preflight_baseline: PreflightBaseline | None = None
    # `provision_on_failure = "warn"`: the sync failure text for a group whose
    # worktree launched without a working environment, folded into its first
    # coder prompt. None (or a None result) means the environment is fine.
    provisioning_failure_for: Callable[[Group], str | None] | None = None
    # Liveness (plan U1/U2/U5): the run's shared activity registry — the same
    # instance the runner stamps — and the `[liveness]` config. Both None ⇒ no
    # probe is installed and heartbeats carry no Sign of Life facts.
    activity: ActivityRegistry | None = None
    liveness: LivenessConfig | None = None
    # Seams for non-`code` recipe executors (the `run` recipe); all None keeps
    # every `code` construction site unchanged. ``triage`` is a one-shot LLM
    # call: prompt -> validated JSON payload.
    triage: Callable[[str], dict] | None = None
    workspace_config: WorkspaceConfig | None = None
    recipes_config: RecipesConfig | None = None
    # The run's Artifact Manifest (plan U6): None keeps every construction
    # site that predates it byte-identical — no entry is ever registered and
    # no downstream prompt is ever changed.
    artifacts: ArtifactManifestStore | None = None
    # Every group in the run, by id — how a downstream group's direct
    # upstreams are checked for a non-`code` recipe before their Artifact
    # Manifest entries are folded into its prompt. Empty for every
    # construction site that predates it, same as `artifacts`.
    groups_by_id: dict[str, Group] = field(default_factory=dict)


def make_executor(deps: ReviewDeps) -> Executor:
    """The scheduler-facing executor: runs one group to a terminal state."""

    async def executor(ctx: GroupContext) -> GroupState:
        return await _GroupExecution(deps, ctx).run()

    return executor


class _GroupExecution(
    GenerationLoop, ReviewerRound, SessionRecords, MergeLadder, EscalationHandlers, SurpriseHandling
):
    """One group's journey: generations, rounds, verdicts, rewrites, merge."""

    def __init__(self, deps: ReviewDeps, ctx: GroupContext):
        self.deps = deps
        self.ctx = ctx
        self.group = ctx.group
        self.gid = ctx.group.id
        self.generation = ctx.generation
        self.rewrites = 0
        self.sessions_spawned = 0
        self.extra_pass_done = False
        self.handoff_prompt: str | None = None
        self.workspace: Path | None = None
        self.coder_sid = ""
        self.coder_entry: SessionEntry | None = None
        self.reviewer_sid: str | None = None
        self._questions = 0  # needs_input rounds this generation (uncounted vs the breaker)
        self._grant_notes: list[str] = []  # operator guidance for an over-cap generation
        # Informational surprises consumed at a checkpoint (plan U13): folded into
        # the next prompt this generation builds, then cleared — never spent as a
        # rewrite, never sent to the speccer.
        self._briefing_notes: list[str] = []
        # An operator `retry` note: what they fixed outside the worker. Folded
        # into the next coder prompt as an "Operator note", one-shot — never
        # spent as a rewrite, never sent to the speccer.
        self._operator_notes: list[str] = []
        self._env_failure: str | None = None
        # The last CoderReport this generation produced (plan U6): set just
        # before `_merge()` is called, read there to build the code group's
        # Artifact Manifest entry summary. Cross-mixin (GenerationLoop writes,
        # MergeLadder reads), so it lives on the host, not on either mixin.
        self._last_report: CoderReport | None = None
        # The merge gate's untracked ladder: a first untracked-only failure is a
        # cheap same-spec relaunch with a note; a second, consecutive one has
        # the leftovers archived out of the tree and the merge proceeds. Counts
        # per group across generations (a relaunch advances the generation).
        self._untracked_strikes = 0
        # One automatic gate re-run per generation on an attributable
        # regression in autonomous mode, before a rewrite is spent on a flake.
        self._flake_reruns = 0
        # Re-entry discovery (R4): a live coder entry at the persisted generation
        # can only pre-exist the executor on a resumed run — fresh runs start with
        # an empty group entry. One-shot: consumed by the first generation.
        self._reentry_entry: SessionEntry | None = self._find_reentry_session()
        # Evidence, not a state (plan P3): the loop only ever tells it when a
        # round started; the writing happens on its own daemon thread and nothing
        # here reads it back.
        self._heartbeat = RoundHeartbeat(
            deps.store.paths,
            self.gid,
            log=self._log,
            activity_provider=self._current_child if deps.activity is not None else None,
        )
        # Suspend Cure counts for this group, per generation (plan U5). Seeded
        # from nothing: `Scheduler.record_cure` persists the real count, and
        # `on_cure` mirrors the value it returns.
        self._cures: dict[int, int] = {}
        if deps.activity is not None and deps.liveness is not None:
            probe = LivenessProbe(
                self._heartbeat,
                deps.liveness,
                self._current_child,
                log=self._log,
                suspend_facts_provider=partial(read_suspend_facts, deps.store.paths),
                cures_provider=lambda: self._cures.get(self._heartbeat.generation_no, 0),
                on_cure=self._on_cure,
                activity=deps.activity,
            )
            self._heartbeat.add_tick_hook(probe.tick)
        # The round number `_round_tag` most recently rendered (plan U10):
        # `_on_content_filtered` needs the number of the round that was in
        # flight when `ContentFiltered` hit, and every site that starts or
        # ends a round already renders its tag through `_round_tag` — so
        # recording it there, as a side effect, is the one place that number
        # is always current with no separate bookkeeping to keep in sync.
        self._current_round_no = 0

    def _current_child(self) -> ChildActivity | None:
        """This group's live worker child, matched on its worktree — the cwd
        every worker call of the group is spawned in."""
        if self.deps.activity is None or self.workspace is None:
            return None
        return self.deps.activity.current(str(self.workspace))

    def _on_cure(self, pid: int, generation: int, session_id: str) -> None:
        self._cures[generation] = self.ctx.record_cure()

    def _log(self, text: str) -> None:
        """Append to the run's always-on lifecycle log (R10): control-plane events
        land in ``run.log`` in every run mode, HITL or autonomous."""
        log_event(self.deps.store.paths, text)
