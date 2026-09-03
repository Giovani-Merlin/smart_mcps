"""Parse a Claude Code transcript into harness-neutral events.

Zero imports from infinity-skills or any other consumer: this module only
knows the raw Claude Code jsonl shape (one JSON object per line, `type`
user/assistant, `message.content` blocks of text/tool_use/tool_result) and
emits a schema any framework can read.

A worker's first prompt is exactly ``f"{base_context}\\n\\n{prompt}"``
(``SessionRunner.start_worker``, ``sessions.py:460``), so the first user
message's text begins with the byte-exact base context. ``strip_prefix``
matches only that exact case — a byte-for-byte prefix — and leaves the
transcript untouched on any mismatch, never a fuzzy or partial strip.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class NeutralEvent(BaseModel):
    """One user turn, assistant turn, tool call, or tool result."""

    event_id: str
    role: str  # user | assistant | tool
    timestamp: str | None = None
    text: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: Any | None = None  # JSON, uncapped
    tool_output: str | None = None  # uncapped
    is_error: bool = False


class StripResult(BaseModel):
    """What was removed from the first user message, if the byte-exact
    base-context prefix matched."""

    applied: bool = False
    char_len: int = 0
    sha256: str | None = None


class ParsedTranscript(BaseModel):
    events: list[NeutralEvent] = Field(default_factory=list)
    strip: StripResult = Field(default_factory=StripResult)


def _tool_result_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _events_from_content(
    role: str, content: Any, uuid: str, timestamp: str | None
) -> list[NeutralEvent]:
    if isinstance(content, str):
        return [NeutralEvent(event_id=uuid, role=role, timestamp=timestamp, text=content)]
    if not isinstance(content, list):
        return []

    events: list[NeutralEvent] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        event_id = uuid if index == 0 else f"{uuid}#{index}"
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                continue
            events.append(
                NeutralEvent(event_id=event_id, role=role, timestamp=timestamp, text=text)
            )
        elif block_type == "thinking":
            events.append(
                NeutralEvent(
                    event_id=event_id,
                    role=role,
                    timestamp=timestamp,
                    text=str(block.get("thinking") or ""),
                )
            )
        elif block_type == "tool_use":
            events.append(
                NeutralEvent(
                    event_id=event_id,
                    role="assistant",
                    timestamp=timestamp,
                    tool_name=block.get("name"),
                    tool_use_id=block.get("id"),
                    tool_input=block.get("input"),
                )
            )
        elif block_type == "tool_result":
            events.append(
                NeutralEvent(
                    event_id=event_id,
                    role="tool",
                    timestamp=timestamp,
                    tool_use_id=block.get("tool_use_id"),
                    tool_output=_tool_result_text(block.get("content")),
                    is_error=bool(block.get("is_error", False)),
                )
            )
        # Unknown block types (image, etc.) are skipped; the contract only
        # promises role/text/tool fidelity.
    return events


def _apply_strip(
    line_events: list[NeutralEvent], strip_prefix: str
) -> tuple[list[NeutralEvent], StripResult]:
    if not line_events:
        return line_events, StripResult()
    first = line_events[0]
    marker = strip_prefix + "\n\n"
    if first.text is None or not first.text.startswith(marker):
        return line_events, StripResult()
    stripped = first.model_copy(update={"text": first.text[len(marker) :]})
    result = StripResult(
        applied=True,
        char_len=len(strip_prefix),
        sha256=hashlib.sha256(strip_prefix.encode("utf-8")).hexdigest(),
    )
    return [stripped, *line_events[1:]], result


def parse_transcript(path: Path, *, strip_prefix: str | None = None) -> ParsedTranscript:
    """Parse one Claude Code ``.jsonl`` transcript file.

    Empty and malformed lines are skipped without raising — a torn write at
    the tail of a live transcript should not lose the rest of the session.
    """
    events: list[NeutralEvent] = []
    strip = StripResult()
    seen_first_user_line = False

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        record_type = record.get("type")
        if record_type not in ("user", "assistant"):
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue

        uuid = str(record.get("uuid") or "")
        timestamp = record.get("timestamp")
        line_events = _events_from_content(record_type, message.get("content"), uuid, timestamp)

        if record_type == "user" and not seen_first_user_line:
            seen_first_user_line = True
            if strip_prefix:
                line_events, strip = _apply_strip(line_events, strip_prefix)

        events.extend(line_events)

    return ParsedTranscript(events=events, strip=strip)


def write_events_gz(path: Path, events: list[NeutralEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json())
            handle.write("\n")


def read_events_gz(path: Path) -> list[NeutralEvent]:
    events: list[NeutralEvent] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            events.append(NeutralEvent.model_validate_json(line))
    return events
