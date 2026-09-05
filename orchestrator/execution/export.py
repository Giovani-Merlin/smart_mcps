"""Export a finished run as a self-contained, framework-agnostic package.

The Observatory stays the live-run surface; *reading* finished runs moves to
external analyzers (Infinity Skills first). The boundary is this file's
contract: ``<run_dir>/ingest/`` — a versioned ``ingest.json`` manifest (the
run -> group -> session join, plan text, assembled specs, rewrite history,
artifact/escalation summaries) plus one ``events/<session_id>.jsonl.gz`` per
session, parsed into harness-neutral events by
``orchestrator.execution.transcript_events``. Consumers never read Claude
Code jsonl directly.

Tolerance rules for old runs, deliberate and load-bearing:

- ``transcript_path`` may be null or stale (usage-limit retries adopt new
  session ids; some manifests predate the field) — re-resolve by globbing
  ``<transcript_root>/*/<session_id>.jsonl`` and mark ``transcript_missing``
  rather than fail. A missing transcript writes no events file.
- a ``failure`` string attached to a completed/resolved state is stale
  (last-writer-wins ``state.json``) — exported as null, flagged, never as a
  failure.
- absent fields on old manifests export as null, never invented zeros; the
  token counters are the exception because 0 already means "not recorded"
  in the manifest itself.
- ``surprises.json`` is consumed state, not history — surprises are mined
  from the report/verdict artifacts instead. A rewrite's
  ``triggering_surprises``/``escalation_ids`` are inferred the same way: the
  surprises and escalations recorded strictly after the previous
  ``spec-gen<N>.json`` (exclusive) up to and including this one's generation
  (inclusive) are attributed to it — there is no field that names the cause
  of a rewrite directly, so this is a best-effort bucketing by generation,
  never an invented value.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, Field

from orchestrator.execution.denial import classify_denial
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.transcript_events import parse_transcript, write_events_gz
from orchestrator.grouping.llm_record import INDEX_NAME as LLM_INDEX_NAME

#: Bump only on a breaking change to the contract; additive fields are free.
SCHEMA_VERSION = 2
FRAMEWORK = "smart-mcps-orchestrator"

_ARTIFACT_RE = re.compile(r"^(report|verdict)-g(\d+)-r(\d+)\.json$")
_SPEC_GEN_RE = re.compile(r"^spec-gen(\d+)\.json$")

#: Artifact filenames that are per-group bookkeeping, not round artifacts.
_ARTIFACT_SKIP = {"heartbeat.json"}


class ExportTokens(BaseModel):
    """Cumulative spend over every round of the session. All-zero means
    "actuals not recorded" for runs that predate the counters."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class ExportSession(BaseModel):
    session_id: str
    role: str  # coder | reviewer
    generation: int = 1
    name: str = ""
    transcript_missing: bool = False
    #: Relative to the package directory (``events/<session_id>.jsonl.gz``);
    #: None when the transcript is missing, so no file was written.
    events_path: str | None = None
    events_count: int = 0
    #: True only when this session's first user message began with the
    #: byte-exact base-context prefix and it was removed from the exported
    #: events; a non-matching first message exports whole with this False.
    base_context_stripped: bool = False
    started_at: str | None = None
    ended_at: str | None = None
    rounds_completed: int = 0
    retirement_reason: str | None = None
    model: str | None = None
    tokens: ExportTokens = Field(default_factory=ExportTokens)


class ExportSurprise(BaseModel):
    kind: str
    description: str = ""
    affected_groups: list[str] = Field(default_factory=list)


class ExportArtifact(BaseModel):
    """One report/verdict summary, with ``path`` kept as the pointer to the
    full artifact. Served tolerant of older schemas: fields absent from the
    file export as null/empty rather than failing the bundle."""

    kind: str  # coder_report | reviewer_verdict | other
    generation: int | None = None
    round: int | None = None
    #: When the orchestrator wrote the artifact, from the file's mtime — these
    #: files carry no timestamp of their own, and the owning session's
    #: ``ended_at`` is null on many runs, so without this a consumer has nothing
    #: to place a round's outcome in time by. Null only if the file vanished
    #: between listing and stat.
    recorded_at: str | None = None
    #: Relative to the run directory.
    path: str
    status: str | None = None
    surprises: list[ExportSurprise] = Field(default_factory=list)
    #: Only for a permission_denied report: which of the unrelated causes it
    #: was, attributed by the same classifier the review loop uses.
    denial_kind: str | None = None
    denied_command: str = ""
    required_changes: list[str] = Field(default_factory=list)
    #: Non-null when the file was unreadable/half-written; the artifact is
    #: still listed so the consumer knows a round happened.
    error: str | None = None


class ExportEscalation(BaseModel):
    id: str
    kind: str = ""
    generation: int | None = None
    prompt: str = ""
    created_at: str | None = None
    #: Relative to the run directory.
    request_path: str
    response_path: str | None = None
    action: str | None = None
    answer: str = ""


class ExportRewrite(BaseModel):
    """One ``spec-gen<N>.json`` snapshot: the group's spec as rewritten,
    plus a best-effort attribution of what triggered it (see module
    docstring — bucketed by generation, not a recorded link)."""

    generation: int
    spec: dict = Field(default_factory=dict)
    triggering_surprises: list[ExportSurprise] = Field(default_factory=list)
    escalation_ids: list[str] = Field(default_factory=list)


class ExportGroup(BaseModel):
    id: str
    name: str = ""
    summary: str = ""
    final_state: str = "pending"
    failure: str | None = None
    #: True when a failure string was recorded but the group's final state is
    #: not a failure (it failed once, then succeeded); ``failure`` is then
    #: exported null so no consumer renders a success as failed.
    stale_failure: bool = False
    depends_on: list[str] = Field(default_factory=list)
    #: The assembled spec from the run's ``groups.json``; null only when the
    #: group id is absent from that snapshot (never observed in a real run).
    spec: dict | None = None
    rewrites: list[ExportRewrite] = Field(default_factory=list)
    sessions: list[ExportSession] = Field(default_factory=list)
    artifacts: list[ExportArtifact] = Field(default_factory=list)
    escalations: list[ExportEscalation] = Field(default_factory=list)


class ExportLlmCall(BaseModel):
    """One call the orchestrator itself made (mapper/speccer), read from
    ``<run_dir>/llm/calls.json``.

    The recorder writes OTel ``gen_ai.*`` names; this flattens them to the
    bundle's own naming. Fields absent from an older ``calls.json`` export as
    null/zero, never invented — and an entry whose ``claude.transcript_path``
    no longer resolves exports with ``transcript_missing`` set, exactly like a
    worker session."""

    seq: int
    recorded_at: str | None = None
    #: ``gen_ai.operation.name`` — the recorder's ``stage`` (``speccer_output``,
    #: ``mapper_output``, ...).
    operation: str = ""
    #: ``gen_ai.request.model``; null when the call failed before a response.
    model: str | None = None
    attempt: int = 0
    #: ``status.code`` — ``ok`` or ``error``.
    status: str = ""
    error: str | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    #: Which groups the call was about — one id for a mid-run rewrite speccer
    #: call, every id for the initial mapper/speccer pass. Empty when the record
    #: predates the ``subject`` field AND the request file could not be read.
    group_ids: list[str] = Field(default_factory=list)
    #: WHY the call happened: the surprises/operator verdicts folded into the
    #: rewrite speccer's ``rewrite_context``, verbatim. Empty for a call with no
    #: recorded cause (every mapper call, and any speccer call whose cause was
    #: never written down).
    rewrite_context: list[str] = Field(default_factory=list)
    #: Pointers into the run directory, so a consumer can always reach the full
    #: prompt and raw response; null when the record names no file.
    request_path: str | None = None
    raw_path: str | None = None
    transcript_missing: bool = False
    #: Relative to the package directory (``events/<session_id>.jsonl.gz``);
    #: None when the transcript is missing, so no file was written. The
    #: base-context strip is deliberately *not* applied — an orchestrator
    #: prompt never carries the workers' base context.
    events_path: str | None = None
    events_count: int = 0
    tokens: ExportTokens = Field(default_factory=ExportTokens)


class ExportBaseContext(BaseModel):
    """The shared prefix every worker's first prompt began with. Null when
    the run predates the file, or wrote it somewhere this export can't find."""

    path: str
    sha256: str
    char_len: int


class ExportPlan(BaseModel):
    path: str = ""
    text: str | None = None


class ExportGrouping(BaseModel):
    name: str | None = None
    granularity: str | None = None


class RunExport(BaseModel):
    """The whole contract — ``ingest.json``, ``schema_version`` first."""

    schema_version: int = SCHEMA_VERSION
    framework: str = FRAMEWORK
    run_id: str
    repo_root: str
    project: str
    plan: ExportPlan = Field(default_factory=ExportPlan)
    grouping: ExportGrouping | None = None
    created_at: str | None = None
    base_context: ExportBaseContext | None = None
    groups: list[ExportGroup] = Field(default_factory=list)
    #: The orchestrator's own LLM calls, in recorded order. Empty for a run
    #: with no ``llm/`` directory (every run before the recorder shipped).
    llm_calls: list[ExportLlmCall] = Field(default_factory=list)


class ExportError(Exception):
    """The run directory cannot be exported at all (missing or empty)."""


# ------------------------------------------------------------------ building


def default_transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _resolve_transcript(
    session_id: str, recorded: str | None, transcript_root: Path
) -> tuple[str | None, bool]:
    """``(path, missing)`` — prefer the recorded path while it exists, else
    re-glob by session id (the id is the stable key; the recorded path goes
    stale when a worktree slug changes or the manifest predates the field)."""
    if recorded and Path(recorded).is_file():
        return recorded, False
    matches = sorted(transcript_root.glob(f"*/{session_id}.jsonl"))
    if matches:
        return str(matches[0]), False
    return recorded, True


def _mtime_iso(path: Path) -> str | None:
    """The file's modification time, UTC ISO 8601 — the fallback when the artifact
    carries no ``recorded_at`` of its own (every run written before
    ``save_group_artifact`` began stamping one). Observed, not invented, but
    weaker than the emitted field: a later rewrite of the file would move it."""
    import datetime

    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.datetime.fromtimestamp(stamp, datetime.UTC).isoformat()


def _artifact(path: Path, run_dir: Path) -> ExportArtifact:
    from orchestrator.observatory.artifacts import load_json

    match = _ARTIFACT_RE.match(path.name)
    if match:
        kind = "coder_report" if match.group(1) == "report" else "reviewer_verdict"
        generation: int | None = int(match.group(2))
        round_no: int | None = int(match.group(3))
    else:
        kind, generation, round_no = "other", None, None

    content, error = load_json(path)
    # The orchestrator stamps `recorded_at` at write time; the mtime stands in for
    # artifacts written before it did.
    recorded_at = content.get("recorded_at") if isinstance(content, dict) else None
    artifact = ExportArtifact(
        kind=kind,
        generation=generation,
        round=round_no,
        recorded_at=str(recorded_at) if recorded_at else _mtime_iso(path),
        path=str(path.relative_to(run_dir)),
        error=error,
    )
    if not isinstance(content, dict):
        return artifact

    surprises = [
        ExportSurprise(
            kind=str(item.get("kind") or "other"),
            description=str(item.get("description") or ""),
            affected_groups=[str(g) for g in item.get("affected_groups") or []],
        )
        for item in content.get("surprises") or []
        if isinstance(item, dict)
    ]
    status = content.get("status")
    denial_kind = None
    if status == "permission_denied":
        # Same attribution the Observatory derives on read: never stored, so
        # every report already on disk gains it retroactively.
        denial_kind = str(
            classify_denial(
                denied_command=str(content.get("denied_command") or ""),
                denial_error=str(content.get("denial_error") or ""),
                denial_source=str(content.get("denial_source") or ""),
            )
        )
    return artifact.model_copy(
        update={
            "status": str(status) if status is not None else None,
            "surprises": surprises,
            "denial_kind": denial_kind,
            "denied_command": str(content.get("denied_command") or ""),
            "required_changes": [str(c) for c in content.get("required_changes") or []],
        }
    )


def _group_artifacts(paths: RunPaths, group_id: str) -> list[ExportArtifact]:
    directory = paths.group_dir(group_id)
    if not directory.is_dir():
        return []
    artifacts = [
        _artifact(path, paths.run_dir)
        for path in sorted(directory.glob("*.json"))
        if path.name not in _ARTIFACT_SKIP and not _SPEC_GEN_RE.match(path.name)
    ]
    # Chronological within the group: by (generation, round); "other" files last.
    # `recorded_at` orders them in real time, but (generation, round) is the
    # authoritative sequence — an "other" file's mtime says nothing about rounds.
    artifacts.sort(key=lambda a: (a.generation is None, a.generation or 0, a.round or 0, a.path))
    return artifacts


def _escalations_by_group(paths: RunPaths) -> dict[str, list[ExportEscalation]]:
    """Request/response pairs off disk, tolerant of malformed files — an
    escalation another schema wrote should still list with what it has.

    Globs both the run-level ``escalations/`` directory and every
    ``groups/<gid>/`` directory (newer runs write escalation pairs there
    too); an id seen in the run-level directory first wins on a collision."""
    from orchestrator.observatory.artifacts import load_json

    directories = [paths.escalations_dir]
    groups_root = paths.run_dir / "groups"
    if groups_root.is_dir():
        directories.extend(sorted(p for p in groups_root.iterdir() if p.is_dir()))

    by_group: dict[str, list[ExportEscalation]] = {}
    seen_ids: set[str] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for request_path in sorted(directory.glob("request-*.json")):
            esc_id = request_path.name[len("request-") : -len(".json")]
            if esc_id in seen_ids:
                continue
            seen_ids.add(esc_id)
            content, _ = load_json(request_path)
            request = content if isinstance(content, dict) else {}
            response_path = directory / f"response-{esc_id}.json"
            action: str | None = None
            answer = ""
            rel_response: str | None = None
            if response_path.is_file():
                rel_response = str(response_path.relative_to(paths.run_dir))
                response, _ = load_json(response_path)
                if isinstance(response, dict):
                    action = str(response.get("action")) if response.get("action") else None
                    answer = str(response.get("answer") or "")
            generation = request.get("generation")
            entry = ExportEscalation(
                id=str(request.get("id") or esc_id),
                kind=str(request.get("kind") or ""),
                generation=int(generation) if isinstance(generation, int) else None,
                prompt=str(request.get("prompt") or ""),
                created_at=str(request.get("created_at")) if request.get("created_at") else None,
                request_path=str(request_path.relative_to(paths.run_dir)),
                response_path=rel_response,
                action=action,
                answer=answer,
            )
            by_group.setdefault(str(request.get("group_id") or ""), []).append(entry)
    return by_group


def _group_spec(paths: RunPaths, group_id: str) -> dict | None:
    from orchestrator.observatory.runs import load_dag

    grouping, _stale = load_dag(paths)
    if grouping is None:
        return None
    for group in grouping.groups:
        if group.id == group_id:
            return group.model_dump(mode="json")
    return None


def _group_rewrites(
    paths: RunPaths,
    group_id: str,
    artifacts: list[ExportArtifact],
    escalations: list[ExportEscalation],
) -> list[ExportRewrite]:
    from orchestrator.observatory.artifacts import load_json

    directory = paths.group_dir(group_id)
    if not directory.is_dir():
        return []
    entries: list[tuple[int, Path]] = []
    for path in directory.glob("spec-gen*.json"):
        match = _SPEC_GEN_RE.match(path.name)
        if match:
            entries.append((int(match.group(1)), path))
    entries.sort(key=lambda pair: pair[0])

    rewrites: list[ExportRewrite] = []
    previous_gen = 0
    for generation, path in entries:
        content, _error = load_json(path)
        spec = content if isinstance(content, dict) else {}
        surprises = [
            surprise
            for artifact in artifacts
            if artifact.generation is not None and previous_gen < artifact.generation <= generation
            for surprise in artifact.surprises
        ]
        escalation_ids = [
            escalation.id
            for escalation in escalations
            if escalation.generation is not None
            and previous_gen < escalation.generation <= generation
        ]
        rewrites.append(
            ExportRewrite(
                generation=generation,
                spec=spec,
                triggering_surprises=surprises,
                escalation_ids=escalation_ids,
            )
        )
        previous_gen = generation
    return rewrites


def _llm_subject(entry: dict, llm_dir: Path) -> tuple[list[str], list[str]]:
    """``(group_ids, rewrite_context)`` for one recorded call.

    Prefer the ``subject`` the recorder writes. Runs recorded before that field
    existed carry the same facts only inside the prompt they sent, so fall back
    to the request file's ``GROUPS_JSON`` block — keyed by group id, with each
    group's ``rewrite_context`` verbatim. That backfill is best-effort by
    design: a prompt that does not match this shape (every mapper call, and any
    future template change) yields empty lists, never a guess and never an
    error. It exists so already-recorded runs are not permanently unattributed.
    """
    import json

    subject = entry.get("subject")
    if isinstance(subject, dict):
        group_ids = [str(g) for g in subject.get("group_ids") or []]
        context = [str(c) for c in subject.get("rewrite_context") or []]
        if group_ids or context:
            return group_ids, context

    request_file = entry.get("request_file")
    if not request_file:
        return [], []
    try:
        text = (llm_dir / str(request_file)).read_text(encoding="utf-8")
        # The prompt names GROUPS_JSON twice — once in the instructions, once as
        # the section header the payload follows; the LAST one starts the data.
        tail = text[text.rindex("GROUPS_JSON") :]
        payload = json.loads(tail[tail.index("{") :])
    except (OSError, ValueError):
        return [], []
    if not isinstance(payload, dict):
        return [], []
    group_ids: list[str] = []
    context: list[str] = []
    for group_id, group in payload.items():
        group_ids.append(str(group_id))
        if isinstance(group, dict):
            context.extend(str(c) for c in group.get("rewrite_context") or [])
    return group_ids, context


def _llm_calls(
    paths: RunPaths,
    *,
    events_dir: Path | None,
    transcript_root: Path,
) -> list[ExportLlmCall]:
    """The orchestrator's own calls off ``<run_dir>/llm/calls.json``.

    Absent, unreadable, or malformed: ``[]`` — the audit trail is inert by
    contract on the writing side too, and losing it must never fail a bundle.
    Each call's transcript is parsed and written to the same ``events/``
    directory as a worker's, so a consumer reads both through one schema.
    """
    from orchestrator.observatory.artifacts import load_json

    index = paths.run_dir / "llm" / LLM_INDEX_NAME
    if not index.is_file():
        return []
    content, _error = load_json(index)
    if not isinstance(content, dict):
        return []
    raw_calls = content.get("calls")
    if not isinstance(raw_calls, list):
        return []

    calls: list[ExportLlmCall] = []
    for entry in raw_calls:
        if not isinstance(entry, dict):
            continue
        seq = entry.get("seq")
        if not isinstance(seq, int):
            continue
        status = entry.get("status")
        session_id = entry.get("claude.session_id")
        session_id = str(session_id) if session_id else None
        duration = entry.get("duration_ms")

        group_ids, rewrite_context = _llm_subject(entry, index.parent)
        transcript_missing = session_id is None
        events_path: str | None = None
        events_count = 0
        if session_id is not None:
            transcript, transcript_missing = _resolve_transcript(
                session_id, entry.get("claude.transcript_path"), transcript_root
            )
            if events_dir is not None and not transcript_missing and transcript:
                # No strip_prefix: a speccer prompt does not begin with the
                # workers' base context, and a partial strip is never correct.
                parsed = parse_transcript(Path(transcript))
                events_count = len(parsed.events)
                write_events_gz(events_dir / f"{session_id}.jsonl.gz", parsed.events)
                events_path = f"events/{session_id}.jsonl.gz"

        calls.append(
            ExportLlmCall(
                seq=seq,
                recorded_at=str(entry["recorded_at"]) if entry.get("recorded_at") else None,
                operation=str(entry.get("gen_ai.operation.name") or ""),
                model=(
                    str(entry["gen_ai.request.model"])
                    if entry.get("gen_ai.request.model")
                    else None
                ),
                attempt=entry.get("attempt") if isinstance(entry.get("attempt"), int) else 0,
                status=str(status.get("code") or "") if isinstance(status, dict) else "",
                error=str(entry["error"]) if entry.get("error") else None,
                duration_ms=duration if isinstance(duration, int) else None,
                session_id=session_id,
                group_ids=group_ids,
                rewrite_context=rewrite_context,
                request_path=_llm_rel(entry.get("request_file"), index.parent, paths.run_dir),
                raw_path=_llm_rel(entry.get("raw_file"), index.parent, paths.run_dir),
                transcript_missing=transcript_missing,
                events_path=events_path,
                events_count=events_count,
                tokens=ExportTokens(
                    input=_int(entry.get("gen_ai.usage.input_tokens")),
                    output=_int(entry.get("gen_ai.usage.output_tokens")),
                    cache_read=_int(entry.get("claude.usage.cache_read_tokens")),
                    cache_creation=_int(entry.get("claude.usage.cache_creation_tokens")),
                ),
            )
        )
    calls.sort(key=lambda call: call.seq)
    return calls


def _llm_rel(name: object, llm_dir: Path, run_dir: Path) -> str | None:
    """A recorded file's path relative to the run directory — a pointer, kept
    even when the file itself is gone (the record is the evidence it existed)."""
    if not name:
        return None
    return str((llm_dir / str(name)).relative_to(run_dir))


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _grouping_granularity(paths: RunPaths) -> str | None:
    from orchestrator.observatory.artifacts import load_json

    trace_path = paths.run_dir / "grouping-trace.json"
    if not trace_path.is_file():
        return None
    content, _error = load_json(trace_path)
    if not isinstance(content, dict):
        return None
    config = content.get("config")
    if not isinstance(config, dict):
        return None
    partition = config.get("partition")
    if not isinstance(partition, dict):
        return None
    granularity = partition.get("granularity")
    return str(granularity) if granularity else None


def _session_sort_key(session: ExportSession) -> tuple[bool, str]:
    # None started_at sorts last, original order preserved among themselves.
    return (session.started_at is None, session.started_at or "")


def _group_sort_key(index: int, group: ExportGroup) -> tuple[bool, str, int]:
    starts = [s.started_at for s in group.sessions if s.started_at]
    # Groups that never started keep their snapshot (DAG) order, after the rest.
    return (not starts, min(starts) if starts else "", index)


def build_export(
    paths: RunPaths,
    *,
    project: str,
    events_dir: Path | None = None,
    transcript_root: Path | None = None,
) -> RunExport:
    """Compose the contract from the run directory, writing each session's
    parsed transcript to ``events_dir/<session_id>.jsonl.gz`` as it goes.

    ``events_dir`` is optional: a caller that only wants the run/group/session
    join (``report.facts.build_facts``, which never reads events) can omit it
    and skip the transcript parsing entirely — every session then exports
    with ``events_path`` null and ``base_context_stripped`` false, same as a
    missing transcript. ``export_run`` always passes it, since the package it
    writes is the events files' only home.

    Raises ``ExportError`` only when there is no run to export; everything
    else degrades to null fields."""
    # Local import: observatory.runs pulls fastapi, which the rest of the
    # execution package never needs.
    from orchestrator.observatory.runs import build_snapshot

    if not paths.run_dir.is_dir():
        raise ExportError(f"no run directory at {paths.run_dir}")
    snapshot = build_snapshot(paths, project)
    if not snapshot.groups:
        raise ExportError(f"run {paths.run_id} has no manifest, state, or DAG to export")

    root = transcript_root or default_transcript_root()
    escalations = _escalations_by_group(paths)

    base_context_path = paths.run_dir / "base-context.md"
    base_context_text: str | None = None
    base_context: ExportBaseContext | None = None
    if base_context_path.is_file():
        base_context_bytes = base_context_path.read_bytes()
        base_context_text = base_context_bytes.decode("utf-8")
        base_context = ExportBaseContext(
            path="base-context.md",
            sha256=hashlib.sha256(base_context_bytes).hexdigest(),
            char_len=len(base_context_text),
        )

    groups: list[ExportGroup] = []
    for group in snapshot.groups:
        sessions: list[ExportSession] = []
        for session in group.sessions:
            if session.role == "orchestrator":
                # Synthetic rewrite/base rows the snapshot injects for board
                # display (`_rewrite_sessions`/`_base_session`) — no real
                # transcript, and `ExportGroup.rewrites` already carries the
                # rewrite's spec, so nothing here would go unrepresented.
                continue
            transcript, missing = _resolve_transcript(
                session.session_id, session.transcript_path, root
            )
            events_path: str | None = None
            events_count = 0
            stripped = False
            if events_dir is not None and not missing and transcript:
                parsed = parse_transcript(Path(transcript), strip_prefix=base_context_text)
                stripped = parsed.strip.applied
                events_count = len(parsed.events)
                write_events_gz(events_dir / f"{session.session_id}.jsonl.gz", parsed.events)
                events_path = f"events/{session.session_id}.jsonl.gz"
            sessions.append(
                ExportSession(
                    session_id=session.session_id,
                    role=session.role,
                    generation=session.generation,
                    name=session.name,
                    transcript_missing=missing,
                    events_path=events_path,
                    events_count=events_count,
                    base_context_stripped=stripped,
                    started_at=session.started_at,
                    ended_at=session.ended_at,
                    rounds_completed=session.rounds_completed,
                    retirement_reason=session.retirement_reason,
                    model=session.model,
                    tokens=ExportTokens(
                        input=session.total_input_tokens,
                        output=session.total_output_tokens,
                        cache_read=session.total_cache_read_tokens,
                        cache_creation=session.total_cache_creation_tokens,
                    ),
                )
            )
        sessions.sort(key=_session_sort_key)
        artifacts = _group_artifacts(paths, group.group_id)
        group_escalations = escalations.get(group.group_id, [])
        groups.append(
            ExportGroup(
                id=group.group_id,
                name=group.name,
                summary=group.summary,
                final_state=group.state,
                # A stale failure is not this group's outcome — export null,
                # keep the flag so nothing is silently dropped.
                failure=None if group.stale_failure else group.failure,
                stale_failure=group.stale_failure,
                depends_on=list(group.depends_on),
                spec=_group_spec(paths, group.group_id),
                rewrites=_group_rewrites(paths, group.group_id, artifacts, group_escalations),
                sessions=sessions,
                artifacts=artifacts,
                escalations=group_escalations,
            )
        )
    groups = [
        group
        for _, group in sorted(
            enumerate(groups), key=lambda pair: _group_sort_key(pair[0], pair[1])
        )
    ]

    plan_text: str | None = None
    if snapshot.plan_path:
        plan_file = paths.repo_root / snapshot.plan_path
        if plan_file.is_file():
            plan_text = plan_file.read_text(encoding="utf-8")

    grouping: ExportGrouping | None = None
    if snapshot.grouping is not None or _grouping_granularity(paths) is not None:
        grouping = ExportGrouping(name=snapshot.grouping, granularity=_grouping_granularity(paths))

    return RunExport(
        run_id=paths.run_id,
        repo_root=str(paths.repo_root),
        project=project,
        plan=ExportPlan(path=snapshot.plan_path, text=plan_text),
        grouping=grouping,
        created_at=snapshot.created_at.isoformat() if snapshot.created_at else None,
        base_context=base_context,
        groups=groups,
        llm_calls=_llm_calls(paths, events_dir=events_dir, transcript_root=root),
    )


def export_run(
    repo_root: Path,
    run_id: str,
    *,
    project: str | None = None,
    transcript_root: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    """Build the contract and write the ``ingest/`` package atomically
    (``ingest.json`` plus ``events/<session_id>.jsonl.gz``). Returns the
    package directory."""
    paths = RunPaths(repo_root, run_id)
    package_dir = out_dir or paths.run_dir / "ingest"
    events_dir = package_dir / "events"
    export = build_export(
        paths,
        project=project or repo_root.name,
        events_dir=events_dir,
        transcript_root=transcript_root,
    )
    atomic_write_text(package_dir / "ingest.json", export.model_dump_json(indent=2) + "\n")
    return package_dir
