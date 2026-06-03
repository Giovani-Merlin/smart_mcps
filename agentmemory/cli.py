#!/usr/bin/env python3
"""
AgentMemory CLI — bash-callable interface to the agentmemory backend.

Subcommands:
  find     <query>   Semantic search across observations, memories, lessons
  save     <content> Save a durable curated memory
  lesson   <content> Save a confidence-scored lesson
  profile            Project snapshot (top concepts, recent sessions)
  sessions <query>   Find prior sessions by semantic topic

Usage:
  smart-mcps-agentmemory find "authentication refactor"
  smart-mcps-agentmemory save "decided to use httpx over requests" --type decision
  smart-mcps-agentmemory lesson "always run tests before pushing" --confidence 0.9
  smart-mcps-agentmemory profile
  smart-mcps-agentmemory sessions "database migration"

Output: JSON to stdout. Errors to stderr with non-zero exit code.
"""

import argparse
import asyncio
import json
import os
import sys

from agentmemory.proxy import (
    _call,
    _fetch_lessons,
    _fmt_lesson,
    _fmt_memory,
    _fmt_observation,
    _follow_memories,
    _is_useful_observation,
)

_CWD = os.getcwd()
_DEFAULT_PROJECT = os.path.basename(_CWD)


def _out(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


async def _find(query: str, project: str, depth: str, limit: int) -> dict:
    payload: dict = {"query": query, "limit": limit * 3, "format": "full", "project": project or _DEFAULT_PROJECT}
    results = await _call("POST", "/agentmemory/smart-search", json=payload)
    observations = results.get("observations") or results.get("results") or []

    useful = [o for o in observations if _is_useful_observation(o)][:limit]
    fmt_obs = [_fmt_observation(o, o.get("score", 0.0)) for o in useful]

    memories: list[dict] = []
    lessons: list[dict] = []

    if depth == "deep":
        all_mems_resp = await _call("GET", "/agentmemory/memories", params={"project": project or _DEFAULT_PROJECT})
        all_mems = all_mems_resp.get("memories", [])
        memories = [_fmt_memory(m) for m in _follow_memories(useful, all_mems)]
        lessons = [_fmt_lesson(lesson) for lesson in await _fetch_lessons(project or _DEFAULT_PROJECT)]

    return {"observations": fmt_obs, "memories": memories, "lessons": lessons}


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


async def _save(
    content: str, memory_type: str, project: str, tags: list[str], concepts: list[str]
) -> dict:
    payload = {
        "content": content,
        "type": memory_type,
        "project": project or _DEFAULT_PROJECT,
        "tags": tags,
        "concepts": concepts,
    }
    result = await _call("POST", "/agentmemory/remember", json=payload)
    mem = result.get("memory") or result
    return _fmt_memory(mem) if isinstance(mem, dict) else result


# ---------------------------------------------------------------------------
# lesson
# ---------------------------------------------------------------------------


async def _lesson(content: str, confidence: float, project: str, tags: list[str]) -> dict:
    payload = {
        "content": content,
        "confidence": confidence,
        "project": project or _DEFAULT_PROJECT,
        "tags": tags,
    }
    result = await _call("POST", "/agentmemory/lessons", json=payload)
    lesson = result.get("lesson") or result
    return _fmt_lesson(lesson) if isinstance(lesson, dict) else result


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


async def _profile(project: str) -> dict:
    result = await _call(
        "GET",
        "/agentmemory/profile",
        params={"project": project or _DEFAULT_PROJECT, "limit": 20},
    )
    return {
        "topConcepts": (result.get("topConcepts") or [])[:15],
        "topFiles": (result.get("topFiles") or [])[:10],
        "recentActivity": (result.get("recentActivity") or [])[:5],
        "sessionCount": result.get("sessionCount"),
    }


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


async def _sessions(query: str, project: str, limit: int) -> dict:
    payload = {"q": query, "limit": limit * 3, "project": project or _DEFAULT_PROJECT}
    results = await _call("POST", "/agentmemory/smart-search", json=payload)
    observations = results.get("observations") or results.get("results") or []

    session_ids = list({o.get("sessionId") for o in observations if o.get("sessionId")})[:limit]
    sessions_resp = await _call("GET", "/agentmemory/sessions", params={"project": project or _DEFAULT_PROJECT})
    all_sessions = sessions_resp.get("sessions", [])

    matched = [s for s in all_sessions if s.get("id") in session_ids or s.get("sessionId") in session_ids]
    return {
        "sessions": [
            {
                "sessionId": s.get("id") or s.get("sessionId"),
                "firstPrompt": (s.get("firstPrompt") or "")[:200],
                "summary": (s.get("summary") or "")[:300],
                "startedAt": s.get("startedAt"),
                "cwd": s.get("cwd"),
            }
            for s in matched
        ]
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smart-mcps-agentmemory",
        description="AgentMemory CLI — query and write to the agentmemory backend",
    )
    parser.add_argument("--project", default="", help="Project name (default: cwd basename)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # find
    p_find = sub.add_parser("find", help="Semantic search")
    p_find.add_argument("query")
    p_find.add_argument("--depth", choices=["normal", "deep"], default="normal")
    p_find.add_argument("--limit", type=int, default=10)

    # save
    p_save = sub.add_parser("save", help="Save a curated memory")
    p_save.add_argument("content")
    p_save.add_argument("--type", dest="memory_type", default="architecture")
    p_save.add_argument("--tags", nargs="*", default=[])
    p_save.add_argument("--concepts", nargs="*", default=[])

    # lesson
    p_lesson = sub.add_parser("lesson", help="Save a confidence-scored lesson")
    p_lesson.add_argument("content")
    p_lesson.add_argument("--confidence", type=float, default=0.8)
    p_lesson.add_argument("--tags", nargs="*", default=[])

    # profile
    sub.add_parser("profile", help="Project snapshot")

    # sessions
    p_sessions = sub.add_parser("sessions", help="Find prior sessions by topic")
    p_sessions.add_argument("query")
    p_sessions.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()

    try:
        if args.cmd == "find":
            result = asyncio.run(_find(args.query, args.project, args.depth, args.limit))
        elif args.cmd == "save":
            result = asyncio.run(_save(args.content, args.memory_type, args.project, args.tags, args.concepts))
        elif args.cmd == "lesson":
            result = asyncio.run(_lesson(args.content, args.confidence, args.project, args.tags))
        elif args.cmd == "profile":
            result = asyncio.run(_profile(args.project))
        elif args.cmd == "sessions":
            result = asyncio.run(_sessions(args.query, args.project, args.limit))
        _out(result)
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)
    except Exception as exc:
        _err(f"unexpected error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
