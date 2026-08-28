#!/usr/bin/env python3
"""W10: measure the fork cache miss — per-fork first *genuine* call figures.

For every session in a run's manifest, this finds the fork's first usage record
that is NOT copied from the parent transcript and reports its `cache_read` /
`cache_creation` (and their sum, the inherited-prefix size), plus wall-clock
from session start to first tool use.

The copied-record trap, which produced a wrong first reading of F12 and is the
reason this script exists: a fork's jsonl **replays the parent's entries
verbatim**, so the literally-first usage record in a fork's transcript belongs
to the base session, not the fork. Records are matched against the parent's by
`uuid` and the first one absent from the parent is taken.

Usage:
    python scripts/measure_fork_cache.py <run_dir> [--transcript-root DIR]

    <run_dir> is `.orchestrator/runs/<run_id>` (holds manifest.json).

Output: one line per session — arm-agnostic, so run it once on an arm-A run and
once on an arm-B run (SMART_MCPS_FORK_CWD_EXPERIMENT=repo_root) and compare.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _records(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _uuids(records: list[dict]) -> set[str]:
    return {r["uuid"] for r in records if isinstance(r.get("uuid"), str)}


def _usage_of(record: dict) -> dict | None:
    message = record.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def _ts(record: dict) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_genuine_usage(records: list[dict], parent_uuids: set[str]) -> dict | None:
    """First usage-bearing record not replayed from the parent transcript."""
    for record in records:
        if record.get("uuid") in parent_uuids:
            continue
        usage = _usage_of(record)
        if usage is not None:
            return record
    return None


def _first_tool_use_ts(records: list[dict], parent_uuids: set[str]) -> datetime | None:
    for record in records:
        if record.get("uuid") in parent_uuids:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        ):
            return _ts(record)
    return None


def _find_transcript(session_id: str, recorded: str | None, root: Path) -> Path | None:
    if recorded and Path(recorded).is_file():
        return Path(recorded)
    matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path, help=".orchestrator/runs/<run_id>")
    parser.add_argument(
        "--transcript-root",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="where <encoded-cwd>/<session_id>.jsonl transcripts live",
    )
    args = parser.parse_args()

    manifest_path = args.run_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"error: no manifest at {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text())

    base_id = manifest.get("base_session_id")
    base_entry = manifest.get("base_session") or {}
    base_path = _find_transcript(base_id, base_entry.get("transcript_path"), args.transcript_root)
    if base_path is None:
        print(f"error: base session {base_id} transcript not found", file=sys.stderr)
        return 1
    base_records = _records(base_path)
    parent_uuids = _uuids(base_records)
    print(f"# run {manifest.get('run_id')}  base {base_id}  ({len(base_records)} base records)")
    print(
        f"# {'session':<44} {'role':<9} {'cache_read':>11} {'cache_creation':>14} {'sum':>9} {'to-first-tool':>13}"
    )

    sessions: list[tuple[str, str, dict]] = []
    for gid, group in (manifest.get("groups") or {}).items():
        for entry in group.get("sessions") or []:
            sessions.append((gid, entry.get("role", "?"), entry))

    for gid, role, entry in sessions:
        sid = entry.get("session_id", "?")
        path = _find_transcript(sid, entry.get("transcript_path"), args.transcript_root)
        label = f"{gid}/{sid[:8]}"
        if path is None:
            print(f"  {label:<44} {role:<9} transcript not found")
            continue
        records = _records(path)
        first = _first_genuine_usage(records, parent_uuids)
        if first is None:
            print(f"  {label:<44} {role:<9} no genuine (non-copied) usage record")
            continue
        usage = _usage_of(first) or {}
        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        cc = int(usage.get("cache_creation_input_tokens", 0) or 0)
        start = _ts(first)
        tool = _first_tool_use_ts(records, parent_uuids)
        delta = f"{(tool - start).total_seconds():.1f}s" if start and tool else "n/a"
        print(f"  {label:<44} {role:<9} {cr:>11,} {cc:>14,} {cr + cc:>9,} {delta:>13}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
