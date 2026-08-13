"""Per-group reports and verdicts as the run wrote them (plan U8).

``groups/<gid>/`` accumulates one ``report-g<N>-r<M>.json`` per coder round and
one ``verdict-g<N>-r<M>.json`` per reviewer round. They are served parsed but
unvalidated: a verdict written by an older schema should still be readable in
the drill-in, so a file that no longer matches ``CoderReport`` /
``ReviewerVerdict`` is returned as-is rather than rejected.

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.

``load_json``/``load_text`` are the package's one artifact reader. Every
Observatory router reads the same kind of thing — a file some other process
wrote, which may be absent, half-written, or from a schema this code predates —
and each one that rolled its own ``try: json.loads`` picked a slightly different
answer for those three cases. They live here rather than in a new module because
this is already the module about reading artifacts off disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from orchestrator.execution.denial import classify_denial
from orchestrator.observatory.runs import RUN_PREFIX, resolve_run

router = APIRouter(tags=["artifacts"], prefix=RUN_PREFIX)


# ------------------------------------------------------------- shared reading


def load_json(path: Path) -> tuple[Any, str | None]:
    """``(content, error)`` for a JSON artifact — never raises.

    A file that is absent, unreadable or half-written all come back as
    ``(None, "<why>")``. Every Observatory surface degrades on artifacts rather
    than failing the request, so the error is data to be shown next to the path
    it came from, not an exception to propagate.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def load_text(path: Path) -> tuple[str | None, str | None]:
    """``(content, error)`` for a text artifact, on ``load_json``'s contract.

    ``errors="replace"`` matches the transcript reader: a prompt or a raw model
    response is whatever bytes the runner wrote, and mojibake in one line is
    worth reading around, not worth losing the file over.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, str(exc)


class Artifact(BaseModel):
    """``kind`` comes from the ``artifact_name`` convention so the pane can group
    reports and verdicts without re-parsing filenames in TypeScript."""

    name: str
    kind: str  # report | verdict | other
    content: Any = None
    error: str | None = None
    #: For a `permission_denied` report only: which of the three unrelated causes
    #: it was (plan P2). Derived on read by the *same* `classify_denial` the review
    #: loop uses, rather than stored — so artifacts are never rewritten, every
    #: report already on disk gains attribution retroactively, and there is exactly
    #: one classifier to keep correct.
    denial_kind: str | None = None


@router.get("/groups/{group_id}/artifacts", response_model=list[Artifact])
def get_artifacts(request: Request, project: str, run_id: str, group_id: str) -> list[Artifact]:
    """A group that has not finished a round yet simply has no directory — that
    is an empty list, not a 404."""
    paths = resolve_run(request, project, run_id)
    directory = paths.group_dir(group_id)
    if not directory.is_dir():
        return []
    return [_read(path) for path in sorted(directory.glob("*.json"))]


def _read(path: Path) -> Artifact:
    kind = path.name.split("-", 1)[0]
    artifact = Artifact(name=path.name, kind=kind if kind in ("report", "verdict") else "other")
    content, error = load_json(path)
    # Half-written or unreadable: name it rather than failing the whole list.
    return artifact.model_copy(
        update={"content": content, "error": error, "denial_kind": _denial_kind(content)}
    )


def _denial_kind(content: Any) -> str | None:
    """Attribute a denial report on read, or return None for anything else.

    Unvalidated on purpose, like everything else here: a report from an older
    schema has no `denial_error`/`denial_source` and must still classify (as
    UNKNOWN, honestly) rather than 500 the Artifacts tab. `deny_rules` is not
    available to this process — it belongs to the run that spawned the worker — so
    a `POLICY_FORBIDDEN` can only be attributed live, in the run log; here that
    same report reads as whatever its text supports.
    """
    if not isinstance(content, dict) or content.get("status") != "permission_denied":
        return None
    return str(
        classify_denial(
            denied_command=str(content.get("denied_command") or ""),
            denial_error=str(content.get("denial_error") or ""),
            denial_source=str(content.get("denial_source") or ""),
        )
    )
