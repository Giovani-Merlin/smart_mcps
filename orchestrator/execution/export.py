"""Export a finished run as a framework-agnostic ``ingest.json`` bundle.

The Observatory stays the live-run surface; *reading* finished runs moves to
external analyzers (Infinity Skills first). The boundary is this file's
contract: a stable, versioned JSON document written into the run directory,
holding the run → group → session join plus artifact/escalation summaries,
with transcript paths resolved against ``~/.claude/projects``. Consumers get a
generic "agent-run bundle" and zero orchestrator-specific code.

Pure read → one atomic write. Everything is derived from what
``observatory/runs.py:build_snapshot`` already composes (state ⋈ manifest ⋈
DAG, with ``stale_failure`` normalization), plus the artifact reader's
tolerant JSON loading and ``classify_denial`` — the same three answers every
other post-hoc reader of a run already agreed on.

Tolerance rules for old runs, deliberate and load-bearing:

- ``transcript_path`` may be null or stale (usage-limit retries adopt new
  session ids; some manifests predate the field) — re-resolve by globbing
  ``<transcript_root>/*/<session_id>.jsonl`` and mark ``transcript_missing``
  rather than fail.
- a ``failure`` string attached to a completed/resolved state is stale
  (last-writer-wins ``state.json``) — exported as null, flagged, never as a
  failure.
- absent fields on old manifests export as null, never invented zeros; the
  token counters are the exception because 0 already means "not recorded"
  in the manifest itself.
- ``surprises.json`` is consumed state, not history — surprises are mined from
  the report/verdict artifacts instead.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, Field

from orchestrator.execution.denial import classify_denial
from orchestrator.execution.manifest import RunPaths, atomic_write_text

#: Bump only on a breaking change to the contract; additive fields are free.
SCHEMA_VERSION = 1
FRAMEWORK = "smart-mcps-orchestrator"

_ARTIFACT_RE = re.compile(r"^(report|verdict)-g(\d+)-r(\d+)\.json$")

#: Artifact filenames that are per-group bookkeeping, not round artifacts.
_ARTIFACT_SKIP = {"heartbeat.json"}


class ExportBaseSession(BaseModel):
    """The run's one shared-context session every group session forked from."""

    session_id: str
    transcript_path: str | None = None
    transcript_missing: bool = False
    #: Relative to the run directory; the frozen shared prefix all groups saw.
    base_context_path: str | None = None
    #: Content hash of that file, so a consumer can key shared-context
    #: summarization across runs of the same grouping.
    base_context_sha256: str | None = None


class ExportTokens(BaseModel):
    """Cumulative spend over every round of the session. All-zero means
    "actuals not recorded" for runs that predate the counters."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0


class ExportSession(BaseModel):
    session_id: str
    role: str  # coder | reviewer | base
    generation: int = 1
    name: str = ""
    transcript_path: str | None = None
    transcript_missing: bool = False
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
    sessions: list[ExportSession] = Field(default_factory=list)
    artifacts: list[ExportArtifact] = Field(default_factory=list)
    escalations: list[ExportEscalation] = Field(default_factory=list)


class RunExport(BaseModel):
    """The whole contract — ``ingest.json``, ``schema_version`` first."""

    schema_version: int = SCHEMA_VERSION
    framework: str = FRAMEWORK
    run_id: str
    repo_root: str
    project: str
    plan_path: str = ""
    created_at: str | None = None
    base_session: ExportBaseSession | None = None
    groups: list[ExportGroup] = Field(default_factory=list)


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


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _base_session(
    paths: RunPaths, base_session_id: str | None, transcript_root: Path
) -> ExportBaseSession | None:
    if not base_session_id:
        return None
    transcript, missing = _resolve_transcript(base_session_id, None, transcript_root)
    base_context = paths.run_dir / "base-context.md"
    has_context = base_context.is_file()
    return ExportBaseSession(
        session_id=base_session_id,
        transcript_path=transcript,
        transcript_missing=missing,
        base_context_path="base-context.md" if has_context else None,
        base_context_sha256=_sha256(base_context) if has_context else None,
    )


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
    artifact = ExportArtifact(
        kind=kind,
        generation=generation,
        round=round_no,
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
        if path.name not in _ARTIFACT_SKIP
    ]
    # Chronological within the group: by (generation, round); "other" files last.
    artifacts.sort(key=lambda a: (a.generation is None, a.generation or 0, a.round or 0, a.path))
    return artifacts


def _escalations_by_group(paths: RunPaths) -> dict[str, list[ExportEscalation]]:
    """Request/response pairs off disk, tolerant of malformed files — an
    escalation another schema wrote should still list with what it has."""
    from orchestrator.observatory.artifacts import load_json

    directory = paths.escalations_dir
    if not directory.is_dir():
        return {}
    by_group: dict[str, list[ExportEscalation]] = {}
    for request_path in sorted(directory.glob("request-*.json")):
        esc_id = request_path.name[len("request-") : -len(".json")]
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


def _session_sort_key(session: ExportSession) -> tuple[bool, str]:
    # None started_at sorts last, original order preserved among themselves.
    return (session.started_at is None, session.started_at or "")


def _group_sort_key(index: int, group: ExportGroup) -> tuple[bool, str, int]:
    starts = [s.started_at for s in group.sessions if s.started_at]
    # Groups that never started keep their snapshot (DAG) order, after the rest.
    return (not starts, min(starts) if starts else "", index)


def build_export(
    paths: RunPaths, *, project: str, transcript_root: Path | None = None
) -> RunExport:
    """Compose the contract from the run directory. Raises ``ExportError`` only
    when there is no run to export; everything else degrades to null fields."""
    # Local import: observatory.runs pulls fastapi, which the rest of the
    # execution package never needs.
    from orchestrator.observatory.runs import build_snapshot

    if not paths.run_dir.is_dir():
        raise ExportError(f"no run directory at {paths.run_dir}")
    snapshot = build_snapshot(paths, project)
    if not snapshot.groups and snapshot.base_session_id is None:
        raise ExportError(f"run {paths.run_id} has no manifest, state, or DAG to export")

    root = transcript_root or default_transcript_root()
    escalations = _escalations_by_group(paths)

    groups: list[ExportGroup] = []
    for group in snapshot.groups:
        sessions = []
        for session in group.sessions:
            transcript, missing = _resolve_transcript(
                session.session_id, session.transcript_path, root
            )
            sessions.append(
                ExportSession(
                    session_id=session.session_id,
                    role=session.role,
                    generation=session.generation,
                    name=session.name,
                    transcript_path=transcript,
                    transcript_missing=missing,
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
                sessions=sessions,
                artifacts=_group_artifacts(paths, group.group_id),
                escalations=escalations.get(group.group_id, []),
            )
        )
    groups = [
        group
        for _, group in sorted(
            enumerate(groups), key=lambda pair: _group_sort_key(pair[0], pair[1])
        )
    ]

    return RunExport(
        run_id=paths.run_id,
        repo_root=str(paths.repo_root),
        project=project,
        plan_path=snapshot.plan_path,
        created_at=snapshot.created_at.isoformat() if snapshot.created_at else None,
        base_session=_base_session(paths, snapshot.base_session_id, root),
        groups=groups,
    )


def export_run(
    repo_root: Path,
    run_id: str,
    *,
    project: str | None = None,
    transcript_root: Path | None = None,
    out_path: Path | None = None,
) -> Path:
    """Build the contract and write ``<run_dir>/ingest.json`` atomically."""
    paths = RunPaths(repo_root, run_id)
    export = build_export(paths, project=project or repo_root.name, transcript_root=transcript_root)
    destination = out_path or paths.run_dir / "ingest.json"
    atomic_write_text(destination, export.model_dump_json(indent=2) + "\n")
    return destination
