"""Per-group reports and verdicts as the run wrote them (plan U8).

``groups/<gid>/`` accumulates one ``report-g<N>-r<M>.json`` per coder round and
one ``verdict-g<N>-r<M>.json`` per reviewer round. They are served parsed but
unvalidated: a verdict written by an older schema should still be readable in
the drill-in, so a file that no longer matches ``CoderReport`` /
``ReviewerVerdict`` is returned as-is rather than rejected.

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from orchestrator.observatory.runs import RUN_PREFIX, resolve_run

router = APIRouter(tags=["artifacts"], prefix=RUN_PREFIX)


class Artifact(BaseModel):
    """``kind`` comes from the ``artifact_name`` convention so the pane can group
    reports and verdicts without re-parsing filenames in TypeScript."""

    name: str
    kind: str  # report | verdict | other
    content: Any = None
    error: str | None = None


@router.get("/groups/{group_id}/artifacts", response_model=list[Artifact])
def get_artifacts(request: Request, project: str, run_id: str, group_id: str) -> list[Artifact]:
    """A group that has not finished a round yet simply has no directory — that
    is an empty list, not a 404."""
    paths = resolve_run(request, project, run_id)
    directory = paths.group_dir(group_id)
    if not directory.is_dir():
        return []
    return [_read(path) for path in sorted(directory.glob("*.json"))]


def _read(path) -> Artifact:
    kind = path.name.split("-", 1)[0]
    artifact = Artifact(name=path.name, kind=kind if kind in ("report", "verdict") else "other")
    try:
        return artifact.model_copy(update={"content": json.loads(path.read_text())})
    except (json.JSONDecodeError, OSError) as exc:
        # Half-written or unreadable: name it rather than failing the whole list.
        return artifact.model_copy(update={"error": str(exc)})
