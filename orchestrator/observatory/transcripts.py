"""Session transcript reading and normalization (plan U8).

The file is Claude Code's own ``.jsonl`` — one JSON object per line, discriminated
by a top-level ``type`` — and it is not our format: it will drift. So the parser
is tolerant *by construction* rather than by exception handling. It keeps only
the block types it renders (``text``, ``tool_use``, ``tool_result``) and silently
drops every other row type and every line that does not parse. A row type that
does not exist yet is therefore already handled, and a transcript caught
half-written mid-append yields the lines that are complete instead of a 500.

``transcript_path`` is absolute and already resolved in ``manifest.json``, so
the session lookup is a manifest read, not a glob.

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.model import SessionEntry
from orchestrator.observatory.runs import RUN_PREFIX, resolve_run

router = APIRouter(tags=["transcripts"], prefix=RUN_PREFIX)

# The only block types the drill-in renders; everything else is noise to it.
RENDERABLE = frozenset({"text", "tool_use", "tool_result"})


class TranscriptEvent(BaseModel):
    """One renderable moment. ``seq`` is 1-based and counts emitted events, not
    file lines — the UI uses it as a stable React key within one response."""

    seq: int
    role: str  # assistant | user
    kind: str  # text | tool_use | tool_result
    text: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_result: str | None = None
    is_error: bool = False
    timestamp: str | None = None


def find_session(paths: RunPaths, session_id: str) -> tuple[str, SessionEntry] | None:
    """The manifest is the only cross-session join — group id plus entry, or None."""
    store = ManifestStore(paths)
    if not store.exists():
        return None
    for group_id, entry in store.load().groups.items():
        for session in entry.sessions:
            if session.session_id == session_id:
                return group_id, session
    return None


def parse_transcript(path: Path) -> list[TranscriptEvent]:
    """Normalize a Claude Code transcript. Never raises on content."""
    events: list[TranscriptEvent] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A partially written final line, or a stray non-JSON line.
                continue
            if not isinstance(row, dict):
                continue
            events.extend(_events_of(row, start=len(events) + 1))
    return events


def _events_of(row: dict, start: int) -> list[TranscriptEvent]:
    role = row.get("type")
    if role not in ("assistant", "user"):
        return []  # attachment, mode, agent-name, custom-title, whatever comes next
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    timestamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None

    content = message.get("content")
    if isinstance(content, str):
        # User turns carry their prompt as a bare string rather than blocks.
        text = content.strip()
        if not text:
            return []
        return [TranscriptEvent(seq=start, role=role, kind="text", text=text, timestamp=timestamp)]
    if not isinstance(content, list):
        return []

    events: list[TranscriptEvent] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in RENDERABLE:
            continue
        event = _event_of(block, role=role, seq=start + len(events), timestamp=timestamp)
        if event is not None:
            events.append(event)
    return events


def _event_of(block: dict, *, role: str, seq: int, timestamp: str | None) -> TranscriptEvent | None:
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return TranscriptEvent(seq=seq, role=role, kind="text", text=text, timestamp=timestamp)
    if kind == "tool_use":
        return TranscriptEvent(
            seq=seq,
            role=role,
            kind="tool_use",
            tool_name=str(block.get("name") or ""),
            tool_input=block.get("input"),
            timestamp=timestamp,
        )
    if kind == "tool_result":
        return TranscriptEvent(
            seq=seq,
            role=role,
            kind="tool_result",
            tool_result=_stringify(block.get("content")),
            is_error=bool(block.get("is_error")),
            timestamp=timestamp,
        )
    return None


def _stringify(content: Any) -> str:
    """Tool results arrive as a plain string or as a list of blocks; the pane
    renders text either way."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part) or json.dumps(content, default=str)
    return json.dumps(content, default=str)


@router.get("/sessions/{session_id}/transcript", response_model=list[TranscriptEvent])
def get_transcript(
    request: Request, project: str, run_id: str, session_id: str
) -> list[TranscriptEvent]:
    """Re-read on every call — that is what makes the SPA's poll show new turns
    while a session is still writing."""
    paths = resolve_run(request, project, run_id)
    found = find_session(paths, session_id)
    if found is None:
        raise HTTPException(
            status_code=404, detail=f"session {session_id!r} is not in run {run_id!r}'s manifest"
        )
    _, session = found
    if not session.transcript_path:
        raise HTTPException(
            status_code=404, detail=f"session {session_id!r} has no transcript path recorded"
        )
    path = Path(session.transcript_path)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"transcript for session {session_id!r} is missing at {path}",
        )
    return parse_transcript(path)
