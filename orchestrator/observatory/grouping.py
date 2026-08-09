"""How this run's plan became groups — the Grouping tab's read model.

Everything served here already exists on disk. ``grouping-trace.json`` has
recorded stage-by-stage partitions, Louvain communities, merge/split/repair
decisions, hub roles, slice atoms and the quality scorecard since the trace
schema shipped, and until now nothing rendered any of it. So this router is
almost entirely a *reader*: it locates the artifacts, reports honestly which
ones are absent, and adds the one derived thing the operator actually asked
for — the per-stage diff that answers "why is task X in group Y".

Two rules shape the shapes below.

**Degradation is data, not an error.** A run with no trace, a run whose grouping
predates named groupings, a trace at a schema version this code does not know,
and the missing ``edge-provenance.json`` (which is *every* run on disk today)
all return 200 with a populated ``missing`` list naming the artifact and the
path it was looked for at. The operator's next move is to go read that path, so
the path has to be in the response; a 404 would give them nothing to act on.

**Staleness is not redefined.** ``dag_source`` reports *where* the DAG came
from, and that is all it does. ``stale_dag`` keeps its original meaning
verbatim — the run has no frozen ``groups.json`` of its own — even when the
source resolution manages to find a better DAG than the shared file. Adding a
source and silently changing what "stale" means would break the one flag the
board already teaches operators to distrust.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.execution.manifest import GroupingNameError, RunPaths, grouping_dir
from orchestrator.observatory.runs import (
    RUN_PREFIX,
    load_manifest,
    resolve_run,
    run_groups_path,
)

router = APIRouter(tags=["grouping"], prefix=RUN_PREFIX)

# The trace schema this reader was written against. A newer trace still renders
# — every section is read defensively — but the tab says so rather than
# implying the sections it cannot find were empty.
KNOWN_TRACE_SCHEMA = 1

TRACE_FILENAME = "grouping-trace.json"
EDGE_PROVENANCE_FILENAME = "edge-provenance.json"
BASE_CONTEXT_FILENAME = "base-context.md"

DagSourceKind = Literal["run_snapshot", "named_grouping", "shared_fallback", "missing"]


class DagSource(BaseModel):
    """Where this run's DAG and grouping artifacts were resolved from.

    ``stale_dag`` is duplicated here deliberately: the board and this tab must
    never disagree about it, so both read the same computation rather than each
    deciding for themselves.
    """

    kind: DagSourceKind
    directory: str | None = None  # display-only; the artifacts' parent
    groups_path: str | None = None
    grouping_name: str | None = None  # the manifest's named grouping, if any
    stale_dag: bool = True
    # Why this source and not the one above it — shown next to the source chip
    # so "shared_fallback" never looks like a choice someone made.
    reason: str = ""


class MissingArtifact(BaseModel):
    """An artifact the tab wanted and did not find.

    ``expected_path`` is the whole point: the operator's next move is to go look
    on disk, and a degradation that does not say where it looked is a dead end.
    """

    artifact: str
    expected_path: str
    explanation: str


class StageDiff(BaseModel):
    """What changed between two consecutive pipeline stages.

    ``moved`` is computed from co-membership, not from group ids: the
    ``renumber`` stage rewrites every id without moving a single task, and an
    id-based diff would light the whole graph up as if it had.
    """

    stage: str
    previous_stage: str | None = None
    moved: list[str] = Field(default_factory=list)
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    group_count: int = 0


class GroupingView(BaseModel):
    """One body with everything the Grouping tab renders."""

    project: str
    run_id: str
    plan_path: str = ""
    dag_source: DagSource
    missing: list[MissingArtifact] = Field(default_factory=list)
    trace_path: str | None = None
    trace_schema_version: int | None = None
    trace_schema_known: bool = True
    # The trace's own sections, passed through unchanged. Read defensively so a
    # section this reader has never heard of is simply carried, and one that has
    # been renamed away degrades to empty rather than raising.
    input_graph: dict[str, Any] | None = None
    node_work: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    hub_roles: list[dict[str, Any]] = Field(default_factory=list)
    slice_atoms: list[dict[str, Any]] = Field(default_factory=list)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    louvain: list[dict[str, Any]] = Field(default_factory=list)
    splits: list[dict[str, Any]] = Field(default_factory=list)
    merges: list[dict[str, Any]] = Field(default_factory=list)
    repairs: list[dict[str, Any]] = Field(default_factory=list)
    group_difficulty: list[dict[str, Any]] = Field(default_factory=list)
    scorecard: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    last_stage: str | None = None
    flags: list[str] = Field(default_factory=list)
    mapper_flags: list[str] = Field(default_factory=list)
    partition_flags: list[str] = Field(default_factory=list)
    failure: dict[str, Any] | None = None
    # Derived, not stored: the stepper's recolour sets.
    stage_diffs: list[StageDiff] = Field(default_factory=list)
    # Edge provenance is not written by any orchestrator on disk yet; the field
    # exists so the tab reads it the moment it does, and lands in ``missing``
    # until then.
    edge_provenance: dict[str, Any] | None = None
    paths: dict[str, str] = Field(default_factory=dict)


# ------------------------------------------------------------- dag resolution


def resolve_dag_source(paths: RunPaths, grouping_name: str | None) -> DagSource:
    """Where to read this run's grouping artifacts from.

    Three tiers, most trustworthy first:

    1. the run's own frozen snapshot in the run directory (``run`` copies the
       whole grouping directory in, so the trace sits beside ``groups.json``);
    2. the named grouping the manifest recorded, if it still exists — a run that
       crashed before its snapshot was taken still has this;
    3. the shared ``.orchestrator/groups.json``, which every planning cycle
       overwrites and which therefore may describe a different plan entirely.

    ``stale_dag`` is computed exactly as it always was — whether tier 1 exists —
    and nothing about tiers 2 and 3 changes it. Finding a *better* fallback than
    the shared file does not make a run's DAG any less unfrozen.
    """
    snapshot = run_groups_path(paths)
    # The one and only definition of stale, unchanged from load_dag's original.
    stale = not snapshot.is_file()

    if snapshot.is_file():
        return DagSource(
            kind="run_snapshot",
            directory=str(paths.run_dir),
            groups_path=str(snapshot),
            grouping_name=grouping_name,
            stale_dag=stale,
            reason="the run's own frozen copy, taken when it started",
        )

    if grouping_name:
        try:
            named = grouping_dir(paths.repo_root, grouping_name)
        except GroupingNameError:
            named = None
        if named is not None and (named / "groups.json").is_file():
            return DagSource(
                kind="named_grouping",
                directory=str(named),
                groups_path=str(named / "groups.json"),
                grouping_name=grouping_name,
                stale_dag=stale,
                reason=(
                    f"this run has no frozen snapshot; read from the named grouping "
                    f"{grouping_name!r} it recorded, which may have been regrouped since"
                ),
            )

    shared = paths.repo_root / ".orchestrator" / "groups.json"
    if shared.is_file():
        return DagSource(
            kind="shared_fallback",
            directory=str(shared.parent),
            groups_path=str(shared),
            grouping_name=grouping_name,
            stale_dag=stale,
            reason=(
                "this run has no frozen snapshot and named no grouping; the shared "
                "groups.json is rewritten by every planning cycle and may describe "
                "a different plan"
            ),
        )

    return DagSource(
        kind="missing",
        grouping_name=grouping_name,
        stale_dag=stale,
        reason="no DAG could be found for this run",
    )


# ------------------------------------------------------------------- reading


def _read_json(path: Path) -> dict[str, Any] | None:
    """A malformed artifact reads as absent. The tab's degradation path already
    says where it looked, which is more use than a 500."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _as_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def stage_diffs(stages: list[dict[str, Any]]) -> list[StageDiff]:
    """The stepper's recolour sets, derived by diffing consecutive partitions.

    A task counts as *moved* when the set of tasks it shares a group with
    changes. That is the question the operator is asking — "who did this end up
    with, and when did that happen" — and it is the only formulation that
    survives ``renumber``, which relabels every group without moving anything.
    """
    diffs: list[StageDiff] = []
    previous_mates: dict[str, frozenset[str]] | None = None
    previous_name: str | None = None

    for snapshot in stages:
        name = str(snapshot.get("stage") or "")
        partition = snapshot.get("partition")
        if not isinstance(partition, dict):
            continue
        mates = _co_membership(partition)

        if previous_mates is None:
            diffs.append(
                StageDiff(
                    stage=name,
                    previous_stage=None,
                    added=sorted(mates),
                    group_count=len(set(partition.values())),
                )
            )
        else:
            shared = set(mates) & set(previous_mates)
            diffs.append(
                StageDiff(
                    stage=name,
                    previous_stage=previous_name,
                    moved=sorted(n for n in shared if mates[n] != previous_mates[n]),
                    added=sorted(set(mates) - set(previous_mates)),
                    removed=sorted(set(previous_mates) - set(mates)),
                    group_count=len(set(partition.values())),
                )
            )
        previous_mates, previous_name = mates, name
    return diffs


def _co_membership(partition: dict[str, Any]) -> dict[str, frozenset[str]]:
    """node → the other nodes sharing its group. Id-independent by construction."""
    members: dict[Any, set[str]] = {}
    for node, group in partition.items():
        members.setdefault(group, set()).add(str(node))
    return {str(node): frozenset(members[group] - {str(node)}) for node, group in partition.items()}


def build_grouping_view(paths: RunPaths, project: str) -> GroupingView:
    """Compose the tab's whole body from disk, naming whatever is absent."""
    manifest = load_manifest(paths)
    grouping_name = manifest.grouping if manifest else None
    source = resolve_dag_source(paths, grouping_name)

    directory = Path(source.directory) if source.directory else paths.run_dir
    trace_path = directory / TRACE_FILENAME
    provenance_path = directory / EDGE_PROVENANCE_FILENAME

    view = GroupingView(
        project=project,
        run_id=paths.run_id,
        plan_path=(manifest.plan_path if manifest else ""),
        dag_source=source,
        trace_path=str(trace_path),
        paths={
            "run_dir": str(paths.run_dir),
            "manifest": str(paths.manifest_path),
            "groups": source.groups_path or str(run_groups_path(paths)),
            "trace": str(trace_path),
            "edge_provenance": str(provenance_path),
            "base_context": str(directory / BASE_CONTEXT_FILENAME),
        },
    )

    trace = _read_json(trace_path)
    if trace is None:
        view.missing.append(
            MissingArtifact(
                artifact=TRACE_FILENAME,
                expected_path=str(trace_path),
                explanation=(
                    "this run has no grouping trace — either it predates the trace "
                    "schema, or `group` was run without recording one. Stages, "
                    "communities and the scorecard cannot be shown without it."
                ),
            )
        )
    else:
        _fill_from_trace(view, trace)

    provenance = _read_json(provenance_path)
    if provenance is None:
        view.missing.append(
            MissingArtifact(
                artifact=EDGE_PROVENANCE_FILENAME,
                expected_path=str(provenance_path),
                explanation=(
                    "edge provenance is not recorded yet (plan A2), so an edge's "
                    "weight cannot be traced back to the signals that produced it. "
                    "Edge weights below are the summed totals only."
                ),
            )
        )
    else:
        view.edge_provenance = provenance

    return view


def _fill_from_trace(view: GroupingView, trace: dict[str, Any]) -> None:
    schema = trace.get("schema_version")
    view.trace_schema_version = schema if isinstance(schema, int) else None
    view.trace_schema_known = view.trace_schema_version == KNOWN_TRACE_SCHEMA
    if not view.trace_schema_known:
        view.missing.append(
            MissingArtifact(
                artifact=f"{TRACE_FILENAME} (schema v{KNOWN_TRACE_SCHEMA})",
                expected_path=str(view.trace_path),
                explanation=(
                    f"this trace reports schema version {view.trace_schema_version!r}; "
                    f"the Observatory reads v{KNOWN_TRACE_SCHEMA}. Sections it does not "
                    "recognise are shown as empty rather than guessed at."
                ),
            )
        )

    view.input_graph = _as_dict(trace.get("input_graph"))
    view.node_work = _as_list(trace.get("node_work"))
    view.budget = _as_dict(trace.get("budget"))
    view.config = _as_dict(trace.get("config")) or {}
    view.hub_roles = _as_list(trace.get("hub_roles"))
    view.slice_atoms = _as_list(trace.get("slice_atoms"))
    view.stages = _as_list(trace.get("stages"))
    view.louvain = _as_list(trace.get("louvain"))
    view.splits = _as_list(trace.get("splits"))
    view.merges = _as_list(trace.get("merges"))
    view.repairs = _as_list(trace.get("repairs"))
    view.group_difficulty = _as_list(trace.get("groups"))
    view.scorecard = _as_dict(trace.get("scorecard"))
    view.provenance = _as_dict(trace.get("provenance"))
    view.failure = _as_dict(trace.get("failure"))
    last_stage = trace.get("last_stage")
    view.last_stage = last_stage if isinstance(last_stage, str) else None
    view.flags = _as_str_list(trace.get("flags"))
    view.mapper_flags = _as_str_list(trace.get("mapper_flags"))
    view.partition_flags = _as_str_list(trace.get("partition_flags"))
    view.stage_diffs = stage_diffs(view.stages)


@router.get("/grouping", response_model=GroupingView)
def get_grouping(request: Request, project: str, run_id: str) -> GroupingView:
    """Everything the Grouping tab renders, in one request.

    Never 404s on a missing artifact — an absent trace is the normal state of
    every run recorded before the trace schema shipped, and the tab has a real
    thing to show for it.
    """
    return build_grouping_view(resolve_run(request, project, run_id), project)


@router.get("/grouping/base-context", response_model=str)
def get_base_context(request: Request, project: str, run_id: str) -> str:
    """The shared context every worker in this run was given, verbatim."""
    paths = resolve_run(request, project, run_id)
    manifest = load_manifest(paths)
    source = resolve_dag_source(paths, manifest.grouping if manifest else None)
    directory = Path(source.directory) if source.directory else paths.run_dir
    path = directory / BASE_CONTEXT_FILENAME
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no base context at {path}")
    return path.read_text(encoding="utf-8", errors="replace")
