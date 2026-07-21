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

from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.scheduler import RunState
from orchestrator.model import GroupingResult, RunManifest
from orchestrator.observatory.registry import Project, find_project, load_registry


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


class SnapshotGroup(BaseModel):
    """One board card: scheduler state joined to the manifest's group entry."""

    group_id: str
    name: str = ""
    summary: str = ""
    state: str = "pending"
    generation: int = 1
    failure: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    sessions: list[SnapshotSession] = Field(default_factory=list)


class DagEdge(BaseModel):
    """A dependency edge, in execution order: ``from`` must complete before ``to``."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


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


def _load_manifest(paths: RunPaths) -> RunManifest | None:
    store = ManifestStore(paths)
    return store.load() if store.exists() else None


def load_dag(paths: RunPaths) -> tuple[GroupingResult | None, bool]:
    """The run's DAG and whether it is stale.

    The per-run snapshot is preferred; ``.orchestrator/groups.json`` is shared and
    rewritten by every planning cycle, so falling back to it can render a DAG
    that never belonged to this run — hence the flag rather than a silent read
    (ADR 0002). Runs that predate the snapshot always take the fallback.
    """
    if paths.groups_path.is_file():
        return GroupingResult.model_validate_json(paths.groups_path.read_text()), False
    shared = paths.repo_root / ".orchestrator" / "groups.json"
    if shared.is_file():
        return GroupingResult.model_validate_json(shared.read_text()), True
    return None, True


def build_snapshot(paths: RunPaths, project: str) -> RunSnapshot:
    """Compose state + manifest + DAG into the one body the board renders."""
    state = _load_state(paths)
    manifest = _load_manifest(paths)
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
                depends_on=list(group.dependencies) if group else [],
                sessions=[
                    SnapshotSession(
                        session_id=session.session_id,
                        role=session.role.value,
                        generation=session.generation,
                        name=session.name,
                        retirement_reason=session.retirement_reason,
                        transcript_path=session.transcript_path,
                    )
                    for session in (entry.sessions if entry else [])
                ],
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
        # Recorded for display only — the read path never checks whether these
        # pids are alive, which is what lets a crashed run render (R9).
        live_pids=dict(state.live_pids) if state else {},
    )
