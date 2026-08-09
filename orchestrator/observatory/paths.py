"""On-disk paths, and the raw bytes behind them.

Two endpoints with deliberately different risk profiles.

``/paths`` is display-only: it returns *strings*, never contents, for every
file-backed panel in the run — including the ones that do not exist, because
"we looked for it here and it was not there" is the fact the operator acts on.
Nothing about it can leak a file, so it needs no defences at all beyond not
reading anything.

``/file`` serves raw bytes and therefore carries the whole security surface.
The model is one sentence: **``root`` is a server-side key, never a directory**.
A client picks from a fixed allowlist (:func:`file_roots`) and supplies only a
path *relative* to whatever that key resolves to, so no client-supplied string
ever becomes the base of a lookup. On top of that:

* ``..`` segments and absolute paths are rejected by :func:`check_relative`,
  which touches no filesystem — a cheap, obvious first gate that also makes the
  rejection independent of what happens to exist on disk;
* the *authoritative* gate is ``resolve()`` + ``is_relative_to(root)``, applied
  after. This is the one that matters: a symlink sitting inside the run
  directory and pointing out of it contains no ``..`` and is not absolute, so
  only resolving it and comparing catches it.

Every refusal carries a machine-readable ``code`` so the UI can tell "you asked
for a root that does not exist" from "that path is not allowed" from "the file
genuinely is not there" — three different messages for the operator, and the
last one is not even an error in the interesting cases.

The endpoint is read-only by construction: one GET, no writes, no directory
listing (a directory is a refusal, not an index — an endpoint that lists is an
endpoint that can be walked), and a byte cap with an honest ``truncated`` flag
rather than an unbounded stream into a browser tab.

Parsed grouping artifacts belong to ``grouping.py``; this module hands back
bytes and nothing else.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.execution.manifest import RunPaths
from orchestrator.observatory.grouping import (
    BASE_CONTEXT_FILENAME,
    EDGE_PROVENANCE_FILENAME,
    TRACE_FILENAME,
    resolve_dag_source,
)
from orchestrator.observatory.runs import RUN_PREFIX, load_manifest, resolve_run, run_groups_path

router = APIRouter(tags=["paths"], prefix=RUN_PREFIX)

# How much of a file ``/file`` will hand over in one response. Artifacts are
# JSON and markdown a human is about to read; a transcript jsonl can be tens of
# megabytes and there is a dedicated parsed endpoint for those.
MAX_FILE_BYTES = 1 << 20  # 1 MiB


class PathEntry(BaseModel):
    """One ``PathChip``.

    ``root`` + ``rel`` are what ``/file`` takes, and they are ``None`` for a
    path that is deliberately not fetchable — a session transcript lives outside
    the run directory entirely and has its own parsed endpoint. The chip still
    shows it: the operator's next move is to open it in an editor, not in the
    browser.
    """

    key: str
    label: str
    panel: str  # board | grouping | log | escalations | sessions
    path: str
    kind: str  # file | directory
    exists: bool
    root: str | None = None
    rel: str | None = None
    description: str = ""


class RunPathsView(BaseModel):
    """Everything the per-route paths drawer needs, with copy-all in mind.

    ``roots`` is the ``/file`` allowlist, resolved, so the drawer can show what
    a root key actually means without guessing.
    """

    project: str
    run_id: str
    roots: dict[str, str] = Field(default_factory=dict)
    entries: list[PathEntry] = Field(default_factory=list)


class FileContent(BaseModel):
    """Raw bytes of one artifact, decoded for display.

    Decoding is UTF-8 with replacement rather than a guess: everything under a
    run directory is text, and a replacement character is a better answer than a
    500 on a half-written file. ``truncated`` says the response stops short of
    ``size_bytes``; it is never silent.
    """

    root: str
    rel: str
    path: str
    size_bytes: int
    returned_bytes: int
    truncated: bool
    encoding: str = "utf-8-replace"
    content: str


# ---------------------------------------------------------------- root registry


def file_roots(paths: RunPaths) -> dict[str, Path]:
    """The ``/file`` allowlist for one run: key → directory.

    Kept here, beside the path resolution the rest of the Observatory already
    does, so it moves when the layout moves. ``grouping`` goes through
    :func:`resolve_dag_source` rather than re-deriving the directory, so it
    follows the run's own snapshot, its named grouping, or the shared fallback
    exactly as the Grouping tab does — one resolution, not two that can drift.
    """
    roots: dict[str, Path] = {"run": paths.run_dir}
    manifest = load_manifest(paths)
    source = resolve_dag_source(paths, manifest.grouping if manifest else None)
    if source.directory:
        roots["grouping"] = Path(source.directory)
    return roots


# ------------------------------------------------------------------ path listing


def build_paths_view(paths: RunPaths, project: str) -> RunPathsView:
    """Every file-backed panel source in the run, existing or not.

    Deliberately does not filter by existence: a missing ``grouping-trace.json``
    is exactly the entry the operator most wants the path of.
    """
    roots = file_roots(paths)
    grouping_dir = roots.get("grouping", paths.run_dir)

    entries = [
        _entry("run_dir", "Run directory", "board", paths.run_dir, roots, kind="directory"),
        _entry(
            "manifest",
            "manifest.json",
            "board",
            paths.manifest_path,
            roots,
            description="append-only session history; ground truth for what attempts happened",
        ),
        _entry(
            "state",
            "state.json",
            "board",
            paths.state_path,
            roots,
            description="current group states only; last-writer-wins, so not an attempt history",
        ),
        _entry("groups", "groups.json", "board", run_groups_path(paths), roots),
        _entry("surprises", "surprises.json", "board", paths.surprises_path, roots),
        _entry("logs_dir", "Logs directory", "log", paths.logs_dir, roots, kind="directory"),
        _entry("event_log", "run.log", "log", paths.event_log_path, roots),
        _entry(
            "escalations_dir",
            "Escalations directory",
            "escalations",
            paths.escalations_dir,
            roots,
            kind="directory",
        ),
        _entry(
            "grouping_dir",
            "Grouping directory",
            "grouping",
            grouping_dir,
            roots,
            kind="directory",
        ),
        _entry("trace", TRACE_FILENAME, "grouping", grouping_dir / TRACE_FILENAME, roots),
        _entry(
            "edge_provenance",
            EDGE_PROVENANCE_FILENAME,
            "grouping",
            grouping_dir / EDGE_PROVENANCE_FILENAME,
            roots,
            description="not written by any orchestrator yet (plan A2)",
        ),
        _entry(
            "base_context",
            BASE_CONTEXT_FILENAME,
            "grouping",
            grouping_dir / BASE_CONTEXT_FILENAME,
            roots,
        ),
        _entry(
            "llm_calls",
            "llm/calls.json",
            "grouping",
            grouping_dir / "llm" / "calls.json",
            roots,
            description="the grouper's own LLM call records",
        ),
    ]
    entries.extend(_transcript_entries(paths))
    return RunPathsView(
        project=project,
        run_id=paths.run_id,
        roots={key: str(value) for key, value in roots.items()},
        entries=entries,
    )


def _entry(
    key: str,
    label: str,
    panel: str,
    path: Path,
    roots: dict[str, Path],
    *,
    kind: str = "file",
    description: str = "",
) -> PathEntry:
    root_key, rel = _locate(path, roots)
    return PathEntry(
        key=key,
        label=label,
        panel=panel,
        path=str(path),
        kind=kind,
        # The only stat this endpoint does. It reports presence, never content.
        exists=path.exists(),
        root=root_key if kind == "file" else None,
        rel=rel if kind == "file" else None,
        description=description,
    )


def _locate(path: Path, roots: dict[str, Path]) -> tuple[str | None, str | None]:
    """Which root key this path is fetchable under, if any.

    Longest root first, so a path under a grouping directory nested inside the
    run directory is offered under the more specific key.
    """
    for key, root in sorted(roots.items(), key=lambda item: -len(str(item[1]))):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        return key, rel.as_posix()
    return None, None


def _transcript_entries(paths: RunPaths) -> list[PathEntry]:
    """Session transcripts, display-only.

    They live under ``~/.claude/projects/``, outside every root by design, so
    they are shown and never served here — the parsed transcript endpoint is the
    supported way to read one.
    """
    manifest = load_manifest(paths)
    if manifest is None:
        return []
    entries = []
    for group_id, group in manifest.groups.items():
        for session in group.sessions:
            if not session.transcript_path:
                continue
            path = Path(session.transcript_path)
            entries.append(
                PathEntry(
                    key=f"transcript:{session.session_id}",
                    label=f"{group_id} · {session.role.value} · {session.session_id[:8]}",
                    panel="sessions",
                    path=str(path),
                    kind="file",
                    exists=path.exists(),
                    description="transcript jsonl, outside the run directory; open it locally",
                )
            )
    return entries


# --------------------------------------------------------------- the file gate


class FileAccessError(Exception):
    """A refusal with a code the UI branches on."""

    def __init__(self, code: str, status_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


def check_relative(rel: str) -> str | None:
    """The cheap first gate. Pure: it touches no filesystem.

    Returns an error code, or ``None`` when the string is shaped like a
    relative path. Being pure is the point — the refusal cannot depend on what
    happens to exist, and it happens before anything is opened, stat-ed or
    resolved. It is *not* the authoritative check; ``..`` and absolute paths are
    simply the forms worth naming, and a symlink is neither.
    """
    if not rel or rel.strip() == "":
        return "rejected_path"
    # Both flavours, so a Windows-style ``C:\x`` or a backslash separator is not
    # a POSIX-relative path that happens to look odd.
    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        return "rejected_path"
    if rel.startswith("/") or rel.startswith("\\"):
        return "rejected_path"
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if any(part == ".." for part in parts):
        return "rejected_path"
    if "\x00" in rel:
        return "rejected_path"
    return None


def resolve_file(paths: RunPaths, root: str, rel: str) -> Path:
    """The requested file, or a :class:`FileAccessError` naming why not.

    Order is load-bearing: unknown key, then the pure syntactic gate, then
    ``resolve()`` + ``is_relative_to``. The last one is the gate that actually
    holds — everything before it is triage.
    """
    roots = file_roots(paths)
    if root not in roots:
        raise FileAccessError(
            "unknown_root",
            404,
            f"unknown root {root!r}; roots are server-side keys, not directories "
            f"(known: {', '.join(sorted(roots))})",
        )
    code = check_relative(rel)
    if code is not None:
        raise FileAccessError(
            code, 400, f"path {rel!r} is not an allowed relative path under root {root!r}"
        )

    base = roots[root].resolve()
    candidate = (base / rel).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        # A symlink inside the run directory pointing out of it lands here and
        # nowhere earlier: it has no ``..`` and is not absolute.
        raise FileAccessError("outside_root", 403, f"path {rel!r} resolves outside root {root!r}")
    if candidate.is_dir():
        # No listing, by design: an endpoint that indexes is an endpoint that
        # can be walked.
        raise FileAccessError("not_a_file", 400, f"path {rel!r} is a directory")
    if not candidate.exists():
        raise FileAccessError("not_found", 404, f"no file at {candidate}")
    if not candidate.is_file():
        raise FileAccessError("not_a_file", 400, f"path {rel!r} is not a regular file")
    return candidate


def read_capped(path: Path, *, limit: int = MAX_FILE_BYTES) -> tuple[str, int, int, bool]:
    """``(text, size_bytes, returned_bytes, truncated)``.

    Reads at most ``limit`` bytes; ``size_bytes`` comes from ``stat`` so the
    response can say how much it is *not* showing rather than pretending the
    file ends there.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        raw = handle.read(limit)
    return raw.decode("utf-8", errors="replace"), size, len(raw), size > len(raw)


# ------------------------------------------------------------------- endpoints


@router.get("/paths", response_model=RunPathsView)
def get_paths(request: Request, project: str, run_id: str) -> RunPathsView:
    """Display-only strings for every file-backed panel in this run.

    Returns paths that do not exist too, flagged — that is what lets a panel say
    "looked for it here" instead of showing nothing.
    """
    return build_paths_view(resolve_run(request, project, run_id), project)


@router.get("/file", response_model=FileContent)
def get_file(request: Request, project: str, run_id: str, root: str, path: str) -> FileContent:
    """Raw bytes of one artifact under an allowlisted root key.

    ``root`` is a key into :func:`file_roots`, never a directory: a client
    cannot name a base to read from, only a file relative to one the server
    chose.
    """
    paths = resolve_run(request, project, run_id)
    try:
        target = resolve_file(paths, root, path)
    except FileAccessError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
        ) from exc
    try:
        text, size, returned, truncated = read_capped(target)
    except OSError as exc:
        raise HTTPException(
            status_code=422, detail={"code": "unreadable", "message": str(exc)}
        ) from exc
    return FileContent(
        root=root,
        rel=path,
        path=str(target),
        size_bytes=size,
        returned_bytes=returned,
        truncated=truncated,
        content=text,
    )
