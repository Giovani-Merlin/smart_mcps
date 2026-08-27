"""Run discovery and the composed run snapshot — the Observatory's read core.

Everything here is pure disk reading. A run is a directory, not a process: the
snapshot resolves group states, the groups→sessions join and the DAG entirely
from files, so a finished run, a failed run and a run whose orchestrator crashed
mid-flight all read identically (R9). Nothing consults ``live_pids`` liveness —
a dead pid recorded in ``state.json`` is data, not a reason to error.

``state.json`` and ``manifest.json`` are written with write-then-rename
(``manifest.atomic_write_text``), so a reader never sees a torn file and no
locking is needed on this side.

This module also owns the project/run resolution helpers the slice routers
(events, escalations, transcripts, artifacts) share, which is what lets them
register routes without touching ``app.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from orchestrator.execution.heartbeat import read_heartbeat
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.model import GroupingResult, RunManifest
from orchestrator.observatory.registry import Project, find_project, load_registry


# Every run-scoped endpoint hangs off this prefix, so the SPA's client can build
# one URL from (project, run_id) and append the resource.
RUN_PREFIX = "/api/projects/{project}/runs/{run_id}"


class ObservatoryContext(BaseModel):
    """What ``create_app`` was configured with; lives on ``app.state``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    registry_path: Path | None = None
    fallback_repo: Path | None = None


class RunInfo(BaseModel):
    """One entry of the run list. ``updated_at`` is what "newest first" sorts on —
    run ids are only chronological when they are the generated ``r<timestamp>``
    form, and operators pass ``--run-id smoke1`` all the time."""

    run_id: str
    updated_at: datetime | None = None


class SnapshotSession(BaseModel):
    """A manifest session, flattened for the UI. ``transcript_path`` is absolute
    and already resolved on disk — the transcript endpoint reads it straight."""

    session_id: str
    role: str
    generation: int = 1
    name: str = ""
    retirement_reason: str | None = None
    transcript_path: str | None = None
    # Cost accounting. ``last_context_tokens`` is occupancy of the latest round —
    # the quantity the grouper's ``estimated_tokens`` predicts, and the only
    # honest thing to compare it against. The four cumulative counters are a
    # different quantity entirely (spend summed over every round), so the UI
    # keeps them in a separate panel. All read 0 for runs recorded before the
    # split shipped, which the client renders as "actuals not recorded".
    last_context_tokens: int = 0
    rounds_completed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    # When the transcript file was last appended to — the cheapest evidence that
    # a session is still producing anything. Recorded for free by the runner and
    # until now read by nobody. None when the path is unset or already gone.
    transcript_mtime: datetime | None = None


class GroupHeartbeat(BaseModel):
    """The group's ``heartbeat.json``, passed through unchanged.

    Deliberately facts only: when this round started, which round it is, and when
    the writer last ran. There is no "stalled" field here and there must never be
    one — persisting the inference would make it a state that future code
    branches on. The UI computes staleness from these numbers itself.
    """

    started_at: str | None = None
    generation: int = 0
    round: int = 0
    round_started_at: str | None = None
    updated_at: str | None = None
    # What the group is doing between round boundaries — "forking the base
    # session", "resuming the interrupted coder", "running". Rounds alone cannot
    # explain a silence that happens *before* round 1 exists, which is where the
    # longest one lives: forking the base session took 21 minutes on a real run.
    # Still a fact, not a verdict: it says what, not whether it is too long.
    phase: str | None = None
    phase_elapsed_s: float | None = None
    # Total paused time within the current round (rate-limit gate overlays,
    # summed) and how long the current round has been open. Both already live in
    # ``heartbeat.json``; absence means "not recorded", never "zero" — a heartbeat
    # from before these fields shipped must not be read as "no pause happened".
    paused_s: float | None = None
    round_elapsed_s: float | None = None


class SnapshotGroup(BaseModel):
    """One board card: scheduler state joined to the manifest's group entry."""

    group_id: str
    name: str = ""
    summary: str = ""
    state: str = "pending"
    generation: int = 1
    failure: str | None = None
    # ``state.json``'s GroupRunState is last-writer-wins and single-valued, so a
    # group that failed and was then resolved keeps its old ``failure`` string
    # attached to a successful state. That is not a second failure and must not
    # render as one — the UI shows a "stale failure text" chip instead.
    stale_failure: bool = False
    depends_on: list[str] = Field(default_factory=list)
    sessions: list[SnapshotSession] = Field(default_factory=list)
    # From the DAG, so estimate-vs-actual has its prediction side and the board
    # can say how many reviewer sessions to expect (intensity drives that).
    difficulty: float | None = None
    intensity: str | None = None
    estimated_tokens: int | None = None
    # None for every run written before the heartbeat shipped, and for any group
    # that never started a round. Absence is normal, so it is a null field and
    # never an error.
    heartbeat: GroupHeartbeat | None = None


class DagEdge(BaseModel):
    """A dependency edge, in execution order: ``from`` must complete before ``to``."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class UsageLimitView(BaseModel):
    """The run's rate-limit gate, as ``usage-limit.json`` recorded it.

    Present as soon as a pause has ever been armed; ``released_at`` set means it
    is over and the banner should clear. Kept a passthrough of the gate's own
    record — no "is it still paused" boolean — for the same reason the heartbeat
    serves facts and not a ``stalled`` verdict: the client can compare
    ``released_at`` and ``reset_at`` to the clock itself.
    """

    armed_at: datetime | None = None
    detail: str = ""
    attempt: int = 1
    reset_at: datetime | None = None
    wake_at: datetime | None = None
    released_at: datetime | None = None


class RunSnapshot(BaseModel):
    """One body with everything the board needs — states, the sessions join, and
    the DAG — so the SPA renders a run from a single request."""

    project: str
    run_id: str
    plan_path: str = ""
    base_session_id: str | None = None
    created_at: datetime | None = None
    groups: list[SnapshotGroup] = Field(default_factory=list)
    edges: list[DagEdge] = Field(default_factory=list)
    stale_dag: bool = False
    live_pids: dict[int, str] = Field(default_factory=dict)
    # The named grouping this run snapshotted (plan U10), or None for a run that
    # predates named groupings.
    grouping: str | None = None
    # The run's HITL configuration as it was persisted. Rated the operator's
    # worst blind spot: without it there is no way to tell a run with escalation
    # switched off from one that simply never escalated, and those look
    # identical on the board.
    escalation: dict | None = None
    # None until the run has ever hit a usage limit, which is the normal case
    # and never an error. A pause in progress is what makes the difference
    # between "this run is wedged" and "this run is waiting", and that question
    # was previously unanswerable from the UI at all.
    usage_limit: UsageLimitView | None = None


# ------------------------------------------------------------------ resolution


def context_of(request: Request) -> ObservatoryContext:
    return request.app.state.observatory


def list_projects(request: Request) -> list[Project]:
    """Re-read the registry per request: adding a project should not need a
    restart, which is half of what R19 asks for."""
    ctx = context_of(request)
    return load_registry(ctx.registry_path, ctx.fallback_repo)


def resolve_repo(request: Request, project: str) -> Path:
    entry = find_project(list_projects(request), project)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown project {project!r}")
    if not entry.usable:
        raise HTTPException(status_code=404, detail=f"project {project!r}: {entry.error}")
    return entry.repo_path


def resolve_run(request: Request, project: str, run_id: str) -> RunPaths:
    """The run's paths, or 404. Every run-scoped endpoint enters through here."""
    paths = RunPaths(resolve_repo(request, project), run_id)
    if not paths.run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} in project {project!r}")
    return paths


# -------------------------------------------------------------------- listing


def runs_dir_of(repo: Path) -> Path:
    return repo / ".orchestrator" / "runs"


def run_groups_path(paths: RunPaths) -> Path:
    """This run's frozen DAG snapshot — the one place the Observatory names it.

    ``RunPaths.groups_path`` is the orchestrator's own spelling and stays
    authoritative while it exists. It has been proposed for removal more than
    once, and it was referenced directly from two modules, so both would have
    started raising ``AttributeError`` mid-request the moment it went. Routing
    both through here means a removal degrades to the literal layout instead of
    a 500, and ``test_observatory_drift.py``'s attribute audit fails at test
    time so the drift is still reported rather than papered over.
    """
    own = getattr(paths, "groups_path", None)
    if isinstance(own, Path):
        return own
    return paths.run_dir / "groups.json"


def list_runs(repo: Path) -> list[RunInfo]:
    """Newest first. An absent ``runs/`` dir is the normal state of a repo that
    has planned but never run, so it lists as empty rather than erroring."""
    directory = runs_dir_of(repo)
    if not directory.is_dir():
        return []
    runs = [
        RunInfo(run_id=entry.name, updated_at=_updated_at(entry))
        for entry in directory.iterdir()
        if entry.is_dir()
    ]
    runs.sort(key=lambda run: (run.updated_at or datetime.min.replace(tzinfo=UTC), run.run_id))
    return list(reversed(runs))


def _updated_at(run_dir: Path) -> datetime | None:
    """The run's last write: state.json moves on every transition; the directory
    itself is the fallback for a run that never got that far."""
    candidates = [run_dir / "state.json", run_dir / "manifest.json", run_dir]
    stamps = [path.stat().st_mtime for path in candidates if path.exists()]
    if not stamps:
        return None
    return datetime.fromtimestamp(max(stamps), tz=UTC)


# ------------------------------------------------------------------- snapshot


def _load_state(paths: RunPaths) -> RunState | None:
    if not paths.state_path.is_file():
        return None
    return RunState.model_validate_json(paths.state_path.read_text())


def load_manifest(paths: RunPaths) -> RunManifest | None:
    store = ManifestStore(paths)
    return store.load() if store.exists() else None


def load_dag(paths: RunPaths) -> tuple[GroupingResult | None, bool]:
    """The run's DAG and whether it is stale.

    The per-run snapshot is preferred; ``.orchestrator/groups.json`` is shared and
    rewritten by every planning cycle, so falling back to it can render a DAG
    that never belonged to this run — hence the flag rather than a silent read
    (ADR 0002). Runs that predate the snapshot always take the fallback.
    """
    snapshot = run_groups_path(paths)
    if snapshot.is_file():
        return GroupingResult.model_validate_json(snapshot.read_text()), False
    shared = paths.repo_root / ".orchestrator" / "groups.json"
    if shared.is_file():
        return GroupingResult.model_validate_json(shared.read_text()), True
    return None, True


def _transcript_mtime(transcript_path: str | None) -> datetime | None:
    """Last write to a session's transcript, or None if there is nothing to stat."""
    if not transcript_path:
        return None
    try:
        stat = Path(transcript_path).stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime, UTC)


def _group_heartbeat(paths: RunPaths, group_id: str) -> GroupHeartbeat | None:
    """Pass the group's heartbeat facts through, dropping anything malformed.

    The inference an operator wants — "nothing has moved for 23 minutes" — is
    computed by the reader from ``updated_at`` / ``round_started_at`` and the
    session's ``transcript_mtime``. It is deliberately not computed here: a
    persisted or served "stalled" flag becomes a state, and this run's plan says
    no such state exists.
    """
    payload = read_heartbeat(paths, group_id)
    if payload is None:
        return None
    try:
        return GroupHeartbeat.model_validate(payload)
    except ValidationError:
        return None


def _usage_limit(paths: RunPaths) -> UsageLimitView | None:
    """The gate's record, or None when there is none to read.

    Malformed is treated as absent for the same reason the heartbeat does it: a
    file written from a worker thread while the reader polls must never be able
    to 500 the whole snapshot.
    """
    try:
        payload = json.loads(paths.usage_limit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return UsageLimitView.model_validate(payload)
    except ValidationError:
        return None


def _is_stale_failure(run_state: GroupRunState | None) -> bool:
    """A ``failure`` string left attached to a state that is not a failure.

    ``GroupRunState`` is single-valued and last-writer-wins, so it cannot say
    "failed once, then succeeded" — it says ``completed`` with the old failure
    text still hanging off it, and real runs on disk look exactly like that.
    Rendering that as a failure would be the likeliest wrong thing this whole
    surface could do, so the flag exists and ``manifest.json``'s append-only
    session list stays the ground truth for what attempts happened.
    """
    if run_state is None or not run_state.failure:
        return False
    return run_state.state in (GroupState.COMPLETED, GroupState.RESOLVED)


def build_snapshot(paths: RunPaths, project: str) -> RunSnapshot:
    """Compose state + manifest + DAG into the one body the board renders."""
    state = _load_state(paths)
    manifest = load_manifest(paths)
    grouping, stale_dag = load_dag(paths)

    planned = {group.id: group for group in grouping.groups} if grouping else {}
    # DAG order first (it is the plan's own order), then anything the run knows
    # about that the DAG does not — a group added after the snapshot was taken.
    ordering = list(planned)
    extra = set(state.groups if state else ()) | set(manifest.groups if manifest else ())
    ordering += sorted(extra - set(ordering))

    groups: list[SnapshotGroup] = []
    for gid in ordering:
        run_state = state.groups.get(gid) if state else None
        entry = manifest.groups.get(gid) if manifest else None
        group = planned.get(gid)
        groups.append(
            SnapshotGroup(
                group_id=gid,
                name=(entry.group_name if entry else "") or (group.name if group else ""),
                summary=(entry.summary if entry else "") or (group.summary if group else ""),
                state=run_state.state.value if run_state else "pending",
                generation=run_state.generation if run_state else 1,
                failure=run_state.failure if run_state else None,
                stale_failure=_is_stale_failure(run_state),
                depends_on=list(group.dependencies) if group else [],
                sessions=[
                    SnapshotSession(
                        session_id=session.session_id,
                        role=session.role.value,
                        generation=session.generation,
                        name=session.name,
                        retirement_reason=session.retirement_reason,
                        transcript_path=session.transcript_path,
                        last_context_tokens=session.last_context_tokens,
                        rounds_completed=session.rounds_completed,
                        total_input_tokens=session.total_input_tokens,
                        total_output_tokens=session.total_output_tokens,
                        total_cache_read_tokens=session.total_cache_read_tokens,
                        total_cache_creation_tokens=session.total_cache_creation_tokens,
                        model=session.model,
                        started_at=session.started_at,
                        ended_at=session.ended_at,
                        transcript_mtime=_transcript_mtime(session.transcript_path),
                    )
                    for session in (entry.sessions if entry else [])
                ],
                difficulty=group.difficulty if group else None,
                intensity=group.intensity.value if group else None,
                estimated_tokens=group.estimated_tokens if group else None,
                heartbeat=_group_heartbeat(paths, gid),
            )
        )

    known = {group.group_id for group in groups}
    edges = [
        DagEdge(from_=dep, to=group.id)
        for group in (grouping.groups if grouping else [])
        for dep in group.dependencies
        if dep in known
    ]

    return RunSnapshot(
        project=project,
        run_id=paths.run_id,
        plan_path=(manifest.plan_path if manifest else "")
        or (grouping.plan_path if grouping else ""),
        base_session_id=manifest.base_session_id if manifest else None,
        created_at=manifest.created_at if manifest else None,
        groups=groups,
        edges=edges,
        stale_dag=stale_dag,
        grouping=manifest.grouping if manifest else None,
        escalation=(
            manifest.escalation.model_dump(mode="json")
            if manifest and manifest.escalation
            else None
        ),
        usage_limit=_usage_limit(paths),
        # Recorded for display only — the read path never checks whether these
        # pids are alive, which is what lets a crashed run render (R9).
        live_pids=dict(state.live_pids) if state else {},
    )
