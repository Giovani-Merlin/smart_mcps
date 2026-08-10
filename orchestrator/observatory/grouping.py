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

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.execution.manifest import GroupingNameError, RunPaths, grouping_dir
from orchestrator.observatory.artifacts import load_json, load_text
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
LLM_DIRNAME = "llm"
CALLS_INDEX_FILENAME = "calls.json"

# The order the partitioner runs its stages in, and therefore the order the
# stepper scrubs through them. The trace records stages as they execute, so a
# trace written by this orchestrator already arrives in this order — the client
# is promised the order regardless, so it never has to know that.
PIPELINE_STAGE_ORDER = ("louvain", "lift", "split", "merge", "repair", "renumber")

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
    # The group-level DAG the partition produced: group id → the groups it must
    # follow. ``input_graph`` above is its task-level counterpart, and the tab
    # draws both, so both are served.
    dag: dict[str, Any] | None = None
    # Always in ``pipeline_order``; see ``ordered_stages``.
    stages: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_order: list[str] = Field(default_factory=lambda: list(PIPELINE_STAGE_ORDER))
    # True only for a trace whose recorded stages were out of pipeline order —
    # nothing this orchestrator writes. Surfaced rather than hidden: a trace that
    # needed reordering is itself a finding.
    stages_reordered: bool = False
    louvain: list[dict[str, Any]] = Field(default_factory=list)
    splits: list[dict[str, Any]] = Field(default_factory=list)
    merges: list[dict[str, Any]] = Field(default_factory=list)
    repairs: list[dict[str, Any]] = Field(default_factory=list)
    group_difficulty: list[dict[str, Any]] = Field(default_factory=list)
    scorecard: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    # The correlation id joining this trace to the grouper's own LLM calls. Only
    # present once the trace's provenance records one; a trace that predates the
    # call recorder leaves it null and the two artifacts simply do not join.
    grouping_run_id: str | None = None
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
    loaded, _error = load_json(path)
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


def ordered_stages(stages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Stages in pipeline order, and whether reordering them changed anything.

    The client stepper scrubs this list front to back, so the order it is served
    in *is* the story it tells; making it re-sort would mean teaching the
    frontend the pipeline's shape as well. A trace written by this orchestrator
    already records stages as they ran, so on real data this is a no-op — which
    is exactly the property worth keeping, because reordering a well-formed
    trace could only falsify it.

    Two stages get their position from context rather than from their name: one
    that ran twice (``lift`` runs on both sides of contraction, so its name says
    nothing about which occurrence this is) and one this reader has never heard
    of. Both sort with whatever preceded them, so an unknown stage stays put
    instead of being flung to one end.
    """
    ranks = {name: index for index, name in enumerate(PIPELINE_STAGE_ORDER)}
    names = [str(snapshot.get("stage") or "") for snapshot in stages]
    repeated = {name for name in names if names.count(name) > 1}

    keyed: list[tuple[tuple[int, int], dict[str, Any]]] = []
    anchor = -1
    for index, (name, snapshot) in enumerate(zip(names, stages, strict=True)):
        rank = ranks.get(name)
        if rank is None or name in repeated:
            rank = anchor
        else:
            anchor = rank
        keyed.append(((rank, index), snapshot))

    result = [snapshot for _key, snapshot in sorted(keyed, key=lambda pair: pair[0])]
    return result, result != stages


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
                    "this grouping has no edge-provenance sidecar — it was produced "
                    "before the ledgers were recorded. An edge's weight cannot be "
                    "traced back to the signals that produced it, so the weights "
                    "below are the summed totals only. Regrouping this plan writes one."
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
    view.dag = _as_dict(trace.get("dag"))
    view.stages, view.stages_reordered = ordered_stages(_as_list(trace.get("stages")))
    view.louvain = _as_list(trace.get("louvain"))
    view.splits = _as_list(trace.get("splits"))
    view.merges = _as_list(trace.get("merges"))
    view.repairs = _as_list(trace.get("repairs"))
    view.group_difficulty = _as_list(trace.get("groups"))
    view.scorecard = _as_dict(trace.get("scorecard"))
    view.provenance = _as_dict(trace.get("provenance"))
    run_id = (view.provenance or {}).get("grouping_run_id")
    view.grouping_run_id = run_id if isinstance(run_id, str) else None
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


def resolved_grouping_dir(paths: RunPaths) -> Path:
    """This run's grouping directory, resolved entirely server-side.

    Every artifact path in this router is built from here, and the only inputs
    are the run the URL named and the grouping name the *manifest* recorded — a
    client never supplies a path component. The named-grouping tier additionally
    goes through ``grouping_dir``, which rejects a separator or ``..`` in the
    name before it is joined to anything, so a manifest carrying a traversal
    string falls through to the next tier rather than escaping the repo.
    """
    manifest = load_manifest(paths)
    source = resolve_dag_source(paths, manifest.grouping if manifest else None)
    return Path(source.directory) if source.directory else paths.run_dir


@router.get("/grouping/base-context", response_model=str)
def get_base_context(request: Request, project: str, run_id: str) -> str:
    """The shared context every worker in this run was given, verbatim."""
    paths = resolve_run(request, project, run_id)
    path = resolved_grouping_dir(paths) / BASE_CONTEXT_FILENAME
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no base context at {path}")
    return path.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------- the grouper's own calls


class LlmCallsView(BaseModel):
    """The grouper's LLM call index — what the mapper and the speccer were asked
    and what they answered.

    ``calls`` entries are passed through **unchanged**, dotted keys and all.
    ``gen_ai.operation.name`` is how the client tells a mapper call from a
    speccer one and it is an OpenTelemetry GenAI convention name, so renaming it
    into something more Pythonic here would cost the label and the convention at
    once.

    Failed and repaired attempts are included, deliberately: a call that got the
    schema wrong and succeeded on retry is the single most informative record in
    the directory, and it is the one the old ``last_raw`` overwrite destroyed.
    """

    run_id: str
    directory: str  # where the records were looked for
    index_path: str
    present: bool = False
    schema_version: int | None = None
    # The join to ``grouping-trace.json``'s ``provenance``.
    grouping_run_id: str | None = None
    # The join to ``groups.json``, which carries no run id of its own — a
    # timestamped field there would break ``serialize_grouping``'s determinism.
    produced_group_ids: list[str] = Field(default_factory=list)
    produced_task_ids: list[str] = Field(default_factory=list)
    calls: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[MissingArtifact] = Field(default_factory=list)


class LlmCallDetail(BaseModel):
    """One attempt, with the prompt it sent and the raw text it got back.

    Both files are named by the *record*, never by the client, and are resolved
    back under the grouping directory before being read.
    """

    seq: int
    call: dict[str, Any]
    request_path: str | None = None
    request_text: str | None = None
    raw_path: str | None = None
    raw_text: str | None = None
    missing: list[MissingArtifact] = Field(default_factory=list)


def _sibling(directory: Path, name: Any) -> Path | None:
    """``directory/name`` when ``name`` is a plain filename directly inside it.

    The filenames come out of ``calls.json``, which is written by the recorder
    and is not attacker input in any normal sense — but it is still a file, and a
    reader that would follow whatever string a file contains is a reader that
    escapes its directory the day that file is wrong. Rejecting separators up
    front and confirming containment after ``resolve()`` is what actually stops
    a symlink pointing out of the run.
    """
    if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name:
        return None
    if "/" in name or name in (".", ".."):
        return None
    candidate = (directory / name).resolve()
    if not candidate.is_file() or not candidate.is_relative_to(directory.resolve()):
        return None
    return candidate


def _absent_llm(view: LlmCallsView, directory: Path) -> LlmCallsView:
    view.missing.append(
        MissingArtifact(
            artifact=f"{LLM_DIRNAME}/{CALLS_INDEX_FILENAME}",
            expected_path=view.index_path,
            explanation=(
                "this grouping recorded no LLM calls. Every run made before the "
                "call recorder shipped is in this state, as is any grouping "
                "produced from a plan whose task map was read verbatim — that "
                f"path calls no model at all. Looked in {directory}."
            ),
        )
    )
    return view


def build_llm_calls_view(paths: RunPaths) -> LlmCallsView:
    """The call index for this run's grouping, or an honest account of its absence."""
    directory = resolved_grouping_dir(paths) / LLM_DIRNAME
    index_path = directory / CALLS_INDEX_FILENAME
    view = LlmCallsView(run_id=paths.run_id, directory=str(directory), index_path=str(index_path))

    index = _read_json(index_path)
    if index is None:
        return _absent_llm(view, directory)

    view.present = True
    schema = index.get("schema_version")
    view.schema_version = schema if isinstance(schema, int) else None
    run_id = index.get("grouping_run_id")
    view.grouping_run_id = run_id if isinstance(run_id, str) else None
    produced = _as_dict(index.get("produced")) or {}
    view.produced_group_ids = _as_str_list(produced.get("group_ids"))
    view.produced_task_ids = _as_str_list(produced.get("task_ids"))
    view.calls = _as_list(index.get("calls"))
    return view


def build_llm_call_detail(paths: RunPaths, seq: int) -> LlmCallDetail | None:
    """One attempt's record plus its two text files. ``None`` when no such seq."""
    view = build_llm_calls_view(paths)
    directory = Path(view.directory)
    record = next((call for call in view.calls if call.get("seq") == seq), None)
    if record is None:
        return None

    detail = LlmCallDetail(seq=seq, call=record)
    for kind, path_field, text_field in (
        ("request_file", "request_path", "request_text"),
        ("raw_file", "raw_path", "raw_text"),
    ):
        named = record.get(kind)
        path = _sibling(directory, named)
        if path is None:
            detail.missing.append(
                MissingArtifact(
                    artifact=str(named or kind),
                    expected_path=str(directory / str(named)) if named else str(directory),
                    explanation=(
                        f"the record names no readable {kind} inside the grouping's "
                        "llm directory. The index survives a lost or unwritable "
                        "side file, so the record itself is still shown."
                    ),
                )
            )
            continue
        text, error = load_text(path)
        setattr(detail, path_field, str(path))
        setattr(detail, text_field, text)
        if error is not None:
            detail.missing.append(
                MissingArtifact(artifact=str(named), expected_path=str(path), explanation=error)
            )
    return detail


@router.get("/grouping/llm", response_model=LlmCallsView)
def get_llm_calls(request: Request, project: str, run_id: str) -> LlmCallsView:
    """The grouper's call index. 200 with ``present: false`` when there is none —
    a 404 here would be indistinguishable from a mistyped run id, and "this run
    predates call recording" is a thing the tab has to render, not an error."""
    return build_llm_calls_view(resolve_run(request, project, run_id))


@router.get("/grouping/llm/calls/{seq}", response_model=LlmCallDetail)
def get_llm_call(request: Request, project: str, run_id: str, seq: int) -> LlmCallDetail:
    """One attempt in full.

    ``seq`` is an integer from the index — the client selects a record, not a
    file. A seq that is not in the index is a genuine 404: unlike a missing
    artifact, it names something that was never claimed to exist.
    """
    detail = build_llm_call_detail(resolve_run(request, project, run_id), seq)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no grouper LLM call with seq {seq}")
    return detail
