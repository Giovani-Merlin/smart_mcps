"""Session transcript reading and normalization (plan U8).

The file is Claude Code's own ``.jsonl`` — one JSON object per line, discriminated
by a top-level ``type`` — and it is not our format: it will drift. So the parser
is tolerant *by construction* rather than by exception handling. It keeps only
the block types it renders and silently drops every other row type and every
line that does not parse. A row type that does not exist yet is therefore
already handled, and a transcript caught half-written mid-append yields the
lines that are complete instead of a 500.

``thinking`` and ``redacted_thinking`` are renderable. "What the agent thought"
is the thing the operator actually asked to see, and it was literally what this
filter was dropping.

A caveat worth knowing before building on this: **every thinking block in every
transcript on this machine carries an empty ``thinking`` string and a signature
only** — the reasoning itself is not persisted to the ``.jsonl``. So enabling
the block type recovers the *shape* of the reasoning (where the agent thought,
for how many tokens, between which tool calls) but not its prose, unless a CLI
that does persist it writes the file. Rather than drop those blocks — which
would hide that the agent reasoned at all — they are emitted with an explicit
withheld marker, and a block carrying real text renders it. Redacted blocks are
the same idea for a different reason: they carry no readable text by
construction.

Assistant rows carry a per-turn ``usage`` object, and it is attached to the
event rather than summed here: the envelope's top-level total sums every turn
and reading it as a context size once produced a 50x-inflated figure that
retired healthy sessions. Per-event usage keeps the distinction visible instead
of baking in a choice.

``?after_seq=`` exists because the drill-in polls every 3s and was re-downloading
whole 342-turn transcripts per tick. ``seq`` counts emitted events from the
start of the file, so it is stable across a full fetch and an incremental one
and ``?seq=`` deep links survive.

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
RENDERABLE = frozenset({"text", "thinking", "redacted_thinking", "tool_use", "tool_result"})

# What a redacted block shows. It has no readable content by construction, and
# an omitted event would make the reasoning look continuous when it is not.
REDACTED_PLACEHOLDER = "[redacted thinking]"

# What a signed-but-empty thinking block shows — the common case in practice.
WITHHELD_PLACEHOLDER = "[thinking not persisted in this transcript]"


class EventUsage(BaseModel):
    """One assistant turn's token usage, as the envelope reported it.

    Four classes, kept apart: cache reads are the cheap class, and a session
    whose spend is mostly cache-read is healthy rather than expensive. Folding
    them together is exactly the loss the cost panel exists to undo.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class TranscriptEvent(BaseModel):
    """One renderable moment.

    ``seq`` is 1-based and counts emitted events from the start of the file, not
    file lines. Counting from the file start — rather than from the start of the
    response — is what makes it stable under ``?after_seq=``: the same event has
    the same ``seq`` whether it arrived in a full fetch or an incremental one, so
    a ``?seq=`` deep link keeps pointing at the same turn.
    """

    seq: int
    role: str  # assistant | user
    kind: str  # text | thinking | redacted_thinking | tool_use | tool_result
    text: str | None = None
    tool_name: str | None = None
    tool_input: Any = None
    tool_result: str | None = None
    is_error: bool = False
    timestamp: str | None = None
    # Present on assistant rows only; the model that produced the turn, and what
    # it cost. Absent (None) on user rows and on any row the CLI wrote without
    # one, which is not an error.
    usage: EventUsage | None = None
    model: str | None = None
    # A thinking block that exists but whose prose the transcript did not keep.
    # The client renders the card with the marker rather than the reasoning, so
    # "the agent thought here" and "the agent said nothing" stay distinguishable.
    thinking_withheld: bool = False


def find_session(paths: RunPaths, session_id: str) -> tuple[str, SessionEntry] | None:
    """The manifest is the only cross-session join — group id plus entry, or None.

    The run-level base session (F8) resolves too, with ``""`` for the group id:
    it belongs to no group by construction (forcing it into one would corrupt
    per-group cost roll-ups), and the transcript endpoint only reads the entry.
    """
    store = ManifestStore(paths)
    if not store.exists():
        return None
    manifest = store.load()
    for group_id, entry in manifest.groups.items():
        for session in entry.sessions:
            if session.session_id == session_id:
                return group_id, session
    base = manifest.base_session
    if base is not None and base.session_id == session_id:
        return "", base
    return None


def parse_transcript(path: Path, *, after_seq: int = 0) -> list[TranscriptEvent]:
    """Normalize a Claude Code transcript. Never raises on content.

    ``after_seq`` filters the *result*, deliberately, rather than seeking into
    the file: ``seq`` counts emitted events, and the mapping from byte offset to
    event number is only knowable by parsing. The saving is the response body,
    which is what the 3s poll was actually paying for.
    """
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
    if after_seq > 0:
        return [event for event in events if event.seq > after_seq]
    return events


def _events_of(row: dict, start: int) -> list[TranscriptEvent]:
    role = row.get("type")
    if role not in ("assistant", "user"):
        return []  # attachment, mode, agent-name, custom-title, whatever comes next
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    timestamp = row.get("timestamp") if isinstance(row.get("timestamp"), str) else None
    usage = _usage_of(message)
    model = message.get("model") if isinstance(message.get("model"), str) else None

    content = message.get("content")
    if isinstance(content, str):
        # User turns carry their prompt as a bare string rather than blocks.
        text = content.strip()
        if not text:
            return []
        return [
            TranscriptEvent(
                seq=start,
                role=role,
                kind="text",
                text=text,
                timestamp=timestamp,
                usage=usage,
                model=model,
            )
        ]
    if not isinstance(content, list):
        return []

    events: list[TranscriptEvent] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in RENDERABLE:
            continue
        event = _event_of(block, role=role, seq=start + len(events), timestamp=timestamp)
        if event is not None:
            # The turn's usage belongs to the turn, so every block it produced
            # carries it. Summing over events would double-count; the client
            # groups by seq of the first block of the turn instead.
            event.usage = usage
            event.model = model
            events.append(event)
    return events


def _usage_of(message: dict) -> EventUsage | None:
    """The assistant turn's own usage. Missing or malformed reads as absent —
    an old transcript without it is normal, not broken."""
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    return EventUsage(
        input_tokens=_int(raw.get("input_tokens")),
        output_tokens=_int(raw.get("output_tokens")),
        cache_read_input_tokens=_int(raw.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_int(raw.get("cache_creation_input_tokens")),
    )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _event_of(block: dict, *, role: str, seq: int, timestamp: str | None) -> TranscriptEvent | None:
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return TranscriptEvent(seq=seq, role=role, kind="text", text=text, timestamp=timestamp)
    if kind == "thinking":
        text = block.get("thinking")
        if isinstance(text, str) and text.strip():
            return TranscriptEvent(
                seq=seq, role=role, kind="thinking", text=text, timestamp=timestamp
            )
        if block.get("signature"):
            # Signed but empty: the agent did think here, and the content was
            # simply not written to the file. Say so; dropping it would claim
            # the agent went straight from one tool call to the next.
            return TranscriptEvent(
                seq=seq,
                role=role,
                kind="thinking",
                text=WITHHELD_PLACEHOLDER,
                thinking_withheld=True,
                timestamp=timestamp,
            )
        return None
    if kind == "redacted_thinking":
        # No readable content by construction; the event exists so the gap in
        # the reasoning is visible instead of silently closed up.
        return TranscriptEvent(
            seq=seq,
            role=role,
            kind="redacted_thinking",
            text=REDACTED_PLACEHOLDER,
            timestamp=timestamp,
        )
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
    request: Request, project: str, run_id: str, session_id: str, after_seq: int = 0
) -> list[TranscriptEvent]:
    """Re-read on every call — that is what makes the SPA's poll show new turns
    while a session is still writing.

    Pass ``?after_seq=<highest seq already held>`` to get only what is new. The
    file is still parsed in full (event numbering demands it), but the response
    carries the tail instead of 342 turns every three seconds.
    """
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
    return parse_transcript(path, after_seq=after_seq)
