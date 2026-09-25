"""Cross-group surprise registry, its residue reporting, and the per-group
prompt-note channels that consume it (plan U1 of the review-loop split).

Surprises fan out through the `SurpriseBoard`: unfinished groups named by a
surprise are rewritten before launch — or instead of merging, when already in
review (origin R12, R16). Completed groups are never rewritten. `surprise_residue`
and `format_residue_report` report on whatever the board never delivered by
the time a run ends.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from orchestrator.execution.manifest import RunPaths, atomic_write_text, log_event
from orchestrator.execution.scheduler import GroupState, RunState
from orchestrator.model import Group, Surprise, SurpriseResidueEntry

if TYPE_CHECKING:
    from orchestrator.execution.review import ReviewDeps

_logger = logging.getLogger(__name__)

#: Above this many named groups a mark is logged as a wide fan-out warning
#: (plan U11 Decisions) — delivered in full regardless, never truncated: the
#: informational kind (plan U13) is the real fix for a broad note draining
#: rewrite budgets, so capping fan-out here would only bite a genuinely
#: rewrite-worthy broadcast.
WIDE_FANOUT_THRESHOLD = 5

#: One id-like token inside a decorated ``affected_groups`` entry.
_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class SurpriseBoard:
    """Cross-group surprise registry. A mark is consumed by the named group's
    executor at its next checkpoint (before launch, or before accepting an
    approval); marks for completed/failed groups are simply never read.

    Persisted to the run directory when constructed with ``paths`` (plan U7):
    a plain in-memory dict dies with the process, silently dropping a surprise
    marked for a group that has not yet run. ``paths=None`` keeps every
    existing in-process test byte-identical.

    ``groups`` (plan U11) is the run's real group list, used to validate every
    id in a surprise's ``affected_groups``: a task id is resolved to its owning
    group, and an id naming neither a group nor a task falls through to the
    run-level bucket (key ``RUN_LEVEL``) instead of a dead bucket nothing ever
    reads. ``groups=None`` (every existing caller before this unit) skips
    validation entirely, so ids like the tests' bare "g0"/"g3" keep working
    unchanged.
    """

    #: Bucket key for a surprise whose id names neither a real group nor a task
    #: owned by one — otherwise silently dropped, per plan U11.
    RUN_LEVEL = "__run__"

    def __init__(
        self,
        paths: RunPaths | None = None,
        *,
        groups: list[Group] | None = None,
        is_settled: Callable[[str], bool] | None = None,
    ) -> None:
        self._paths = paths
        self._lock = threading.Lock()
        # r20260924: a surprise about a group that has already merged (or one
        # naming no group at all) is delivered to nobody — until this it was
        # only ever reported in the end-of-run residue, hours after the moment
        # a driver could have acted on it. `is_settled` (the scheduler's live
        # view) decides; without it the board reads `state.json` lazily.
        self._is_settled = is_settled
        self._pending: dict[str, list[Surprise]] = {}
        self._group_ids: frozenset[str] | None = (
            frozenset(g.id for g in groups) if groups is not None else None
        )
        self._task_owner: dict[str, str] = {}
        # Case-folded full task ids and their leading token ("u8" for
        # "u8-structure-fix-budget"): workers name tasks the way the plan does
        # ("U8"), not by the slugged id the grouper assigned.
        self._task_alias_owner: dict[str, str] = {}
        # A surprise aimed at a non-`code` group has no coder to read it: the
        # run executor consumes it at group start and notes it in the artifact
        # summary. The anchor line says so at mark time (r20260925-101742: one
        # died in the residue as "never delivered — group already completed").
        self._recipe_by_group: dict[str, str] = {}
        if groups is not None:
            for group in groups:
                self._recipe_by_group[group.id] = group.recipe
                for task in group.tasks:
                    self._task_owner.setdefault(task, group.id)
                    self._task_alias_owner.setdefault(task.lower(), group.id)
                    head = task.split("-", 1)[0].lower()
                    if any(ch.isdigit() for ch in head):
                        self._task_alias_owner.setdefault(head, group.id)
        if paths is not None and paths.surprises_path.is_file():
            raw = json.loads(paths.surprises_path.read_text())
            self._pending = {
                gid: [Surprise.model_validate(item) for item in items] for gid, items in raw.items()
            }

    def mark(self, surprise: Surprise, *, source_group: str | None = None) -> None:
        with self._lock:
            targets = [gid for gid in surprise.affected_groups if gid != source_group]
            if len(targets) > WIDE_FANOUT_THRESHOLD:
                _logger.warning(
                    "surprise from %s names %d groups (wide fan-out): %s",
                    source_group,
                    len(targets),
                    surprise.description,
                )
            keys: list[str] = []
            for gid in targets:
                for key in self._resolve(gid, surprise, source_group):
                    if key not in keys:
                        keys.append(key)
            if not keys and self._group_ids is not None:
                # No other group named (an empty list, or only the source
                # itself): a finding about future work, which must reach the
                # run's residue report rather than vanish.
                keys.append(self.RUN_LEVEL)
            for key in keys:
                self._append(key, surprise)
                self._log_late_surprise(key, surprise, source_group)
            self._persist()

    def _log_late_surprise(self, key: str, surprise: Surprise, source_group: str | None) -> None:
        """One anchor line in run.log for a surprise nobody will consume —
        non-blocking, no escalation: the driver greps `SURPRISE` and decides.
        The bucket is still appended as before, so the residue report and
        every existing reader see exactly what they saw."""
        if self._paths is None:
            return
        src = source_group or "?"
        desc = " ".join(surprise.description.split())
        if len(desc) > 200:
            desc = desc[:199] + "…"
        if key == self.RUN_LEVEL:
            line = f"SURPRISE [{surprise.kind}] group {src} → (no target group): {desc}"
        elif self._settled(key):
            line = f"SURPRISE [{surprise.kind}] group {src} → {key} (already merged): {desc}"
        elif self._recipe_by_group.get(key, "code") != "code":
            recipe = self._recipe_by_group[key]
            line = (
                f"SURPRISE [{surprise.kind}] group {src} → {key} "
                f"({recipe} recipe, no coder): {desc}"
            )
        else:
            return
        try:
            log_event(self._paths, line)
        except OSError:  # evidence is never worth a surprise
            pass

    def _settled(self, gid: str) -> bool:
        if self._is_settled is not None:
            return self._is_settled(gid)
        if self._paths is None:
            return False
        try:
            state = RunState.model_validate_json(self._paths.state_path.read_text())
        except (OSError, ValueError):
            return False
        entry = state.groups.get(gid)
        return entry is not None and entry.state in (GroupState.COMPLETED, GroupState.RESOLVED)

    def _resolve(self, gid: str, surprise: Surprise, source_group: str | None) -> list[str]:
        """Map a raw ``affected_groups`` id to the buckets it should land in."""
        if self._group_ids is None:
            return [gid]  # no group list configured — legacy, unvalidated behavior
        if gid in self._group_ids:
            return [gid]
        owner = self._task_owner.get(gid)
        if owner is not None:
            return [owner]
        # A decorated id — "g3 (structure-fix-budget/U8)" in r20260908. Plan
        # task ids are what a worker actually knows, so a task token wins over
        # a group token that may be a guess.
        tokens = [t.lower() for t in _ID_TOKEN.findall(gid)]
        task_owners = [self._task_alias_owner[t] for t in tokens if t in self._task_alias_owner]
        group_hits = [t for t in tokens if t in self._group_ids]
        resolved = list(dict.fromkeys(task_owners or group_hits))
        if resolved:
            _logger.info(
                "surprise from %s names %r — resolved to %s", source_group, gid, ", ".join(resolved)
            )
            return resolved
        _logger.warning(
            "surprise from %s names unknown id %s (no matching group or task): %s",
            source_group,
            gid,
            surprise.description,
        )
        return [self.RUN_LEVEL]

    def _append(self, key: str, surprise: Surprise) -> None:
        """Append, deduplicating an identical surprise already pending in this
        bucket (plan U11): successive rounds/generations re-emit the same
        finding, and a dead-simple in-list check is enough since a bucket
        rarely holds more than a handful of entries."""
        bucket = self._pending.setdefault(key, [])
        if surprise not in bucket:
            bucket.append(surprise)

    def pending_for(self, group_id: str) -> list[Surprise]:
        with self._lock:
            return list(self._pending.get(group_id, []))

    def consume(self, group_id: str) -> list[Surprise]:
        with self._lock:
            surprises = self._pending.pop(group_id, [])
            if surprises:
                self._persist()
            return surprises

    def _persist(self) -> None:
        if self._paths is None:
            return
        payload = {
            gid: [surprise.model_dump() for surprise in surprises]
            for gid, surprises in self._pending.items()
        }
        atomic_write_text(self._paths.surprises_path, json.dumps(payload, indent=2) + "\n")


#: Reason strings for a residue entry (plan U12) — a bucket resolved to a real
#: group whose state is terminal-completed never gets another checkpoint to
#: consume it at; ``RUN_LEVEL`` never named a group at all; anything else was
#: still reachable when the run stopped.
REASON_GROUP_COMPLETED = "never delivered — group already completed"
REASON_UNKNOWN_GROUP = "unknown group id"
REASON_RUN_ENDED = "run ended before delivery"


def surprise_residue(paths: RunPaths, state: RunState | None) -> list[SurpriseResidueEntry]:
    """Every bucket still on the board when the run ended, each labelled with
    why (plan U12). Reads ``surprises.json`` directly rather than constructing
    a ``SurpriseBoard`` — a residue report has no group list to validate
    against and must never mutate the file it is reporting on. Sorted by
    bucket for a stable, readable listing.
    """
    if not paths.surprises_path.is_file():
        return []
    try:
        raw = json.loads(paths.surprises_path.read_text())
    except json.JSONDecodeError:
        return []
    entries: list[SurpriseResidueEntry] = []
    for bucket, items in raw.items():
        surprises = [Surprise.model_validate(item) for item in items]
        if not surprises:
            continue
        entries.append(
            SurpriseResidueEntry(
                bucket=bucket,
                count=len(surprises),
                reason=_residue_reason(bucket, state),
                surprises=surprises,
            )
        )
    entries.sort(key=lambda entry: entry.bucket)
    return entries


def _residue_reason(bucket: str, state: RunState | None) -> str:
    if bucket == SurpriseBoard.RUN_LEVEL:
        return REASON_UNKNOWN_GROUP
    group_state = state.groups.get(bucket) if state is not None else None
    if group_state is not None and group_state.state in (
        GroupState.COMPLETED,
        GroupState.RESOLVED,
    ):
        return REASON_GROUP_COMPLETED
    return REASON_RUN_ENDED


def format_residue_report(entries: list[SurpriseResidueEntry]) -> str:
    """Human-readable rendering shared by the CLI end-of-run summary and
    `finish` (plan U12). An empty board renders an explicit "none pending"
    rather than a bare heading — the whole point is that the operator should
    never have to open ``surprises.json`` to learn the board is empty."""
    lines = ["surprises pending at end of run:"]
    if not entries:
        lines.append("  none pending")
        return "\n".join(lines)
    for entry in entries:
        lines.append(f"  {entry.bucket}: {entry.count} pending — {entry.reason}")
        for surprise in entry.surprises:
            lines.append(f"    - [{surprise.kind}] {surprise.description}")
    return "\n".join(lines)


class SurpriseHandling:
    """The surprise board's consumer side and the three prompt-note channels
    a group's coder prompt is built from: informational surprise briefings,
    an environment-provisioning notice, and an operator's `retry` note. Mixed
    onto `_GroupExecution`, whose `__init__` is the only place the attributes
    below are born (see `ExecutionHost`)."""

    gid: str
    group: Group
    deps: ReviewDeps
    _briefing_notes: list[str]
    _operator_notes: list[str]
    _env_failure: str | None
    _log: Callable[[str], None]
    _rewrite: Callable[..., Awaitable[None]]

    def _spread(self, surprises: list[Surprise]) -> None:
        """Fan surprises out to the groups they name — never back at the source."""
        for surprise in surprises:
            self.deps.board.mark(surprise, source_group=self.gid)

    async def _handle_pending_surprises(self, reason: str) -> bool:
        """Consume this group's pending surprises at a checkpoint (before
        launch, or before an approval is accepted). A surprise whose kind is
        anything but ``informational`` is rewrite-worthy: any such surprise
        pending triggers a full rewrite (spec + speccer call, one rewrite
        spent), which also consumes every other pending surprise including any
        informational ones mixed in. When only informational surprises are
        pending, they cost nothing: consumed here and folded into the next
        prompt this generation builds (plan U13), never touching
        ``rewrite_spec`` or the rewrite counter.

        Returns True when a rewrite happened, so the caller can bail out of its
        current path (merge/approval) the way it always has.
        """
        pending = self.deps.board.pending_for(self.gid)
        if not pending:
            return False
        if any(surprise.kind != "informational" for surprise in pending):
            await self._rewrite(reason)
            return True
        informational = self.deps.board.consume(self.gid)
        self._briefing_notes.extend(surprise.description for surprise in informational)
        self._log(
            f"group {self.gid}: {len(informational)} informational surprise(s) "
            "folded into the next briefing (no rewrite spent)"
        )
        return False

    def _apply_briefing(self, prompt: str) -> str:
        """Fold any consumed informational surprises into a coder prompt about
        to be sent (plan U13), then clear them — a one-shot addition to
        whichever prompt this generation happens to build next (a fresh
        launch, or a breaker handoff)."""
        if not self._briefing_notes:
            return prompt
        notes = "\n".join(f"- {note}" for note in self._briefing_notes)
        self._briefing_notes = []
        return f"{prompt}\n\n## Informational updates from other groups\n{notes}\n"

    def _apply_env_notice(self, prompt: str) -> str:
        """Tell a coder whose worktree failed to provision (warn mode) exactly
        what failed, once. Silent completion with a green self-verification
        against a missing environment is the worst of the three outcomes;
        this is the least the worker must know to avoid it."""
        if not self._env_failure:
            return prompt
        notice = self._env_failure
        self._env_failure = None
        return (
            f"{prompt}\n\n## Environment warning\n"
            "Provisioning this worktree's environment FAILED before you started, "
            "so nothing here has a working venv yet:\n\n```\n"
            f"{notice}\n```\n\n"
            "Fix the dependency spec if the cause is in this repo, re-run `uv sync` "
            "in the worktree, and do not report any verification item as passed "
            "unless it ran in a working environment. If you cannot repair the "
            "environment, report status `blocked` and say so.\n"
        )

    def _apply_operator_note(self, prompt: str) -> str:
        """Fold an operator's ``retry`` note into the coder prompt about to be
        sent, once: what was fixed outside the worker and why it should go again
        on the unchanged spec."""
        if not self._operator_notes:
            return prompt
        notes = "\n".join(f"- {note}" for note in self._operator_notes)
        self._operator_notes = []
        return (
            f"{prompt}\n\n## Operator note\n"
            "The previous attempt at this spec stopped; the operator fixed the cause "
            "outside your session and relaunched you on the same spec:\n"
            f"{notes}\n"
        )
