"""External-oracle check for `transcript_events.parse_transcript` (g2-2).

Globs one real Claude Code transcript with >= 50 lines from the live session
store, parses it, and checks full fidelity against the raw jsonl:

- every source line whose `type` is user/assistant yields >= 1 event
- every `tool_use` id in the source appears as an event's `tool_use_id`
- no event text is truncated (longest `tool_output` equals the source
  block's length)

Not a pytest test: the live session store lives outside any worktree, and
this script is the "verification script" alternative the spec allows for
that case. Run manually: `uv run python scripts/verify_transcript_fidelity.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orchestrator.execution.transcript_events import parse_transcript


def _find_candidate(root: Path) -> Path | None:
    best: Path | None = None
    best_lines = 0
    for candidate in root.glob("*/*.jsonl"):
        try:
            lines = sum(1 for _ in candidate.open(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if lines >= 50 and lines > best_lines:
            best, best_lines = candidate, lines
    return best


def main() -> int:
    root = Path.home() / ".claude" / "projects"
    transcript = _find_candidate(root)
    if transcript is None:
        print("no transcript with >= 50 lines found", file=sys.stderr)
        return 1
    print(f"using {transcript}")

    source_lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    source_records = []
    for line in source_lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            source_records.append(record)

    ua_records = [r for r in source_records if r.get("type") in ("user", "assistant")]
    source_tool_use_ids: set[str] = set()
    max_tool_output_len = 0
    for record in ua_records:
        content = record.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                source_tool_use_ids.add(block["id"])
            if block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, str):
                    max_tool_output_len = max(max_tool_output_len, len(text))
                elif isinstance(text, list):
                    joined = "".join(
                        str(b.get("text", ""))
                        for b in text
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    max_tool_output_len = max(max_tool_output_len, len(joined))

    parsed = parse_transcript(transcript)

    # Every user/assistant source line yields >= 1 event. Correlate by uuid
    # prefix since one line can fan out into several events.
    event_ids_by_prefix: dict[str, int] = {}
    for event in parsed.events:
        prefix = event.event_id.split("#", 1)[0]
        event_ids_by_prefix[prefix] = event_ids_by_prefix.get(prefix, 0) + 1

    missing_lines = [
        r.get("uuid")
        for r in ua_records
        if event_ids_by_prefix.get(str(r.get("uuid") or ""), 0) < 1
    ]

    parsed_tool_use_ids = {e.tool_use_id for e in parsed.events if e.tool_use_id and e.tool_name}
    missing_tool_use_ids = source_tool_use_ids - parsed_tool_use_ids

    parsed_max_tool_output = max(
        (len(e.tool_output) for e in parsed.events if e.tool_output), default=0
    )

    ok = True
    if missing_lines:
        print(
            f"FAIL: {len(missing_lines)} user/assistant lines produced no event: {missing_lines[:5]}"
        )
        ok = False
    if missing_tool_use_ids:
        print(f"FAIL: {len(missing_tool_use_ids)} tool_use ids missing from parsed events")
        ok = False
    if parsed_max_tool_output < max_tool_output_len:
        print(
            f"FAIL: longest parsed tool_output ({parsed_max_tool_output}) < source ({max_tool_output_len})"
        )
        ok = False

    if ok:
        print(
            f"OK: {len(ua_records)} lines, {len(source_tool_use_ids)} tool_use ids, "
            f"longest tool_output {max_tool_output_len} chars — full fidelity"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
