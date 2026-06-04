#!/usr/bin/env python3
"""
AgentMemory CLI — bash-callable interface to the agentmemory backend.

Subcommands:
  find            <query>   4-step retrieval: compact→expand→parallel hydration
  save            <content> Save a durable curated memory
  lesson          <content> Save a confidence-scored lesson
  profile                   Project snapshot (top concepts, recent sessions)
  sessions        <query>   Find prior sessions by semantic topic
  session-context           Stateless context block via POST /context (no session created)
  enrich          <files>   File-scoped cross-session context (pre-tool hook)

Usage:
  smart-mcps-agentmemory find "authentication refactor"
  smart-mcps-agentmemory find "authentication" --files src/auth.ts --depth deep
  smart-mcps-agentmemory save "decided to use httpx over requests" --type architecture --strength 9
  smart-mcps-agentmemory save "jwt refresh bug" --type bug --source-obs-ids obs_abc obs_def
  smart-mcps-agentmemory lesson "always run tests before pushing" --confidence 0.9
  smart-mcps-agentmemory profile
  smart-mcps-agentmemory sessions "database migration"
  smart-mcps-agentmemory session-context
  smart-mcps-agentmemory enrich src/auth.ts src/middleware.ts

Output: JSON to stdout. Errors to stderr with non-zero exit code.
"""

import argparse
import asyncio
import json
import os
import sys

import httpx

BASE_URL = os.environ.get("AGENTMEMORY_URL", "http://localhost:3111").rstrip("/")
TIMEOUT = 30.0


async def _call(method: str, path: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
            resp = await client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"AgentMemory daemon is unreachable at {BASE_URL}. "
            "Run: systemctl --user start agentmemory"
        ) from exc


def _fmt_memory(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "type": m.get("type"),
        "title": (m.get("title") or "")[:80],
        "content": (m.get("content") or "")[:400],
        "concepts": m.get("concepts", [])[:8],
        "files": m.get("files", [])[:5],
        "strength": m.get("strength"),
        "sourceObservationIds": m.get("sourceObservationIds", [])[:10],
    }


def _fmt_observation(obs: dict, score: float = 0.0) -> dict:
    fmt = {
        "id": obs.get("id") or obs.get("obsId"),
        "sessionId": obs.get("sessionId"),
        "type": obs.get("type"),
        "title": (obs.get("title") or "")[:100],
        "facts": obs.get("facts", [])[:4],
        "concepts": obs.get("concepts", [])[:6],
        "files": obs.get("files", [])[:5],
        "importance": obs.get("importance"),
        "score": round(score, 3),
    }
    gc = obs.get("graphContext")
    if gc:
        fmt["graph_context"] = gc
    return fmt


def _fmt_lesson(lesson: dict) -> dict:
    return {
        "id": lesson.get("id"),
        "content": (lesson.get("content") or "")[:300],
        "context": (lesson.get("context") or "")[:150],
        "confidence": lesson.get("confidence"),
        "tags": lesson.get("tags", []),
    }


def _fmt_crystal(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "sessionId": c.get("sessionId"),
        "narrative": (c.get("narrative") or "")[:400],
        "keyOutcomes": c.get("keyOutcomes", [])[:5],
        "filesAffected": c.get("filesAffected", [])[:5],
    }


def _fmt_insight(ins: dict) -> dict:
    return {
        "id": ins.get("id"),
        "title": (ins.get("title") or "")[:100],
        "content": (ins.get("content") or "")[:300],
        "confidence": ins.get("confidence"),
        "sourceMemoryIds": ins.get("sourceMemoryIds", [])[:5],
    }


_SEMANTIC_OBS_TYPES = {"decision", "conversation", "subagent", "other", "architecture", "bug", "pattern", "code", "error"}


def _is_useful_observation(obs: dict) -> bool:
    facts = obs.get("facts", [])
    if facts:
        return True
    return obs.get("type", "") in _SEMANTIC_OBS_TYPES


def _follow_memories(top_obs: list[dict], all_memories: list[dict], limit: int = 6) -> list[dict]:
    top_obs_ids = {o.get("id") or o.get("obsId") for o in top_obs if o.get("id") or o.get("obsId")}
    top_session_ids = {o.get("sessionId") for o in top_obs if o.get("sessionId")}
    top_concepts = {c for o in top_obs for c in o.get("concepts", [])}
    top_files = {f for o in top_obs for f in o.get("files", [])}

    scored: list[tuple[int, float, dict]] = []
    for m in all_memories:
        if not m.get("isLatest", True):
            continue
        pts = 0
        src_obs = set(m.get("sourceObservationIds") or [])
        m_sessions = set(m.get("sessionIds") or [])
        m_concepts = set(m.get("concepts") or [])
        m_files = set(m.get("files") or [])

        if src_obs & top_obs_ids:
            pts += 3
        if m_sessions & top_session_ids:
            pts += 2
        if m_concepts & top_concepts:
            pts += 1
        if m_files & top_files:
            pts += 1

        if pts > 0:
            scored.append((pts, float(m.get("strength") or 0.0), m))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [m for _, _, m in scored[:limit]]


async def _fetch_lessons(project: str = "", limit: int = 10) -> list:
    try:
        params: dict = {"limit": limit, "minConfidence": 0.1}
        if project:
            params["project"] = project
        resp = await _call("GET", "/agentmemory/lessons", params=params)
        return resp.get("lessons", [])
    except Exception:
        return []


async def _fetch_crystals(project: str = "", limit: int = 8) -> list:
    try:
        params: dict = {"limit": limit}
        if project:
            params["project"] = project
        resp = await _call("GET", "/agentmemory/crystals", params=params)
        return resp.get("crystals", [])
    except Exception:
        return []


async def _search_insights(query: str, project: str = "", limit: int = 8) -> list:
    try:
        body: dict = {"query": query}
        if project:
            body["project"] = project
        resp = await _call("POST", "/agentmemory/insights/search", json=body)
        return resp.get("insights", [])[:limit]
    except Exception:
        return []


async def _enrich_files(
    files: list[str],
    project: str = "",
    terms: list[str] | None = None,
    session_id: str = "",
) -> dict:
    body: dict = {"sessionId": session_id or "unknown", "files": files}
    if project:
        body["project"] = project
    if terms:
        body["terms"] = terms
    try:
        return await _call("POST", "/agentmemory/enrich", json=body)
    except Exception:
        return {}

_CWD = os.getcwd()
_DEFAULT_PROJECT = os.path.basename(_CWD)


def _out(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# find — 4-step progressive retrieval
# ---------------------------------------------------------------------------


async def _find(
    query: str,
    project: str,
    depth: str,
    limit: int,
    files: str = "",
    session_id: str = "",
) -> dict:
    file_list = [f.strip() for f in files.split(",") if f.strip()] if files else []
    proj = project or _DEFAULT_PROJECT

    # Step 1 — compact discovery
    compact_obs: list[dict] = []
    bundled_lessons: list[dict] = []
    try:
        compact_resp = await _call(
            "POST", "/agentmemory/smart-search",
            json={"query": query, "format": "compact", "limit": limit * 2, "project": proj},
        )
        compact_obs = [r for r in compact_resp.get("results", []) if _is_useful_observation(r)]
        bundled_lessons = compact_resp.get("lessons", [])
    except Exception:
        pass

    # Step 2 — expand top 3-5 hits for full content
    top_ids = [o.get("id") or o.get("obsId") for o in compact_obs[:5] if o.get("id") or o.get("obsId")]
    full_obs = compact_obs
    if top_ids:
        try:
            expand_resp = await _call(
                "POST", "/agentmemory/smart-search",
                json={"expandIds": top_ids, "query": query, "project": proj},
            )
            expanded = [r for r in expand_resp.get("results", []) if _is_useful_observation(r)]
            if expanded:
                expanded_ids = {r.get("id") or r.get("obsId") for r in expanded}
                remaining = [o for o in compact_obs[5:] if (o.get("id") or o.get("obsId")) not in expanded_ids]
                full_obs = expanded + remaining
        except Exception:
            pass

    matched_session_ids = {o.get("sessionId") for o in compact_obs[:5] if o.get("sessionId")}
    crystallized_ids = {o.get("crystallizedInto") for o in compact_obs[:5] if o.get("crystallizedInto")}
    top_obs_for_linking = full_obs[:5]

    # Step 3 — parallel hydration
    hydration_tasks = [
        _call("GET", "/agentmemory/memories", params={"limit": 80, "project": proj}),
        _search_insights(query, proj, limit),
        _fetch_crystals(proj, 20),
    ]
    if file_list:
        hydration_tasks.append(_enrich_files(file_list, proj, session_id=session_id))

    hydration_results = await asyncio.gather(*hydration_tasks, return_exceptions=True)

    mem_resp = hydration_results[0] if not isinstance(hydration_results[0], Exception) else {"memories": []}
    raw_insights = hydration_results[1] if not isinstance(hydration_results[1], Exception) else []
    all_crystals = hydration_results[2] if not isinstance(hydration_results[2], Exception) else []
    enrich_resp = hydration_results[3] if len(hydration_results) > 3 and not isinstance(hydration_results[3], Exception) else {}

    all_memories = mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
    linked_memories = _follow_memories(top_obs_for_linking, all_memories) if top_obs_for_linking else []

    matched_crystals = [
        c for c in (all_crystals if isinstance(all_crystals, list) else [])
        if c.get("sessionId") in matched_session_ids or c.get("id") in crystallized_ids
    ]

    out: dict = {"observations": [_fmt_observation(o, o.get("score", 0.0)) for o in full_obs[:limit]]}
    if linked_memories:
        out["memories"] = [_fmt_memory(m) for m in linked_memories]
    if bundled_lessons:
        out["lessons"] = [_fmt_lesson(l) for l in bundled_lessons]
    if raw_insights:
        out["insights"] = [_fmt_insight(i) for i in raw_insights]
    if matched_crystals:
        out["crystals"] = [_fmt_crystal(c) for c in matched_crystals]
    if enrich_resp:
        enriched = enrich_resp.get("enrichedContext", [])
        bugs = enrich_resp.get("bugCandidates", [])
        if enriched:
            out["enriched_file_context"] = [_fmt_observation(o) for o in enriched]
        if bugs:
            out["bug_candidates"] = [_fmt_observation(o) for o in bugs]

    # Step 4 — deep only: graph traversal
    if depth == "deep":
        try:
            graph_resp = await _call(
                "POST", "/agentmemory/graph/query",
                json={"query": query, "depth": 1, "project": proj},
            )
            nodes = graph_resp.get("nodes", [])[:20]
            edges = graph_resp.get("edges", [])[:30]
            if nodes or edges:
                out["graph_neighbors"] = {"nodes": nodes, "edges": edges}
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


async def _save(
    content: str,
    memory_type: str,
    project: str,
    tags: list[str],
    concepts: list[str],
    files: list[str],
    strength: int = 7,
    source_obs_ids: list[str] | None = None,
    ttl_days: int = 0,
) -> dict:
    payload: dict = {
        "content": content,
        "type": memory_type,
        "project": project or _DEFAULT_PROJECT,
        "tags": tags,
        "concepts": concepts,
        "files": files,
        "strength": max(1, min(10, strength)),
    }
    if source_obs_ids:
        payload["sourceObservationIds"] = source_obs_ids
    if ttl_days > 0:
        payload["ttlDays"] = ttl_days
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
    payload = {"query": query, "limit": limit * 3, "format": "full", "project": project or _DEFAULT_PROJECT}
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
# session-context — stateless POST /context (zero DB mutation)
# ---------------------------------------------------------------------------


async def _session_context(project: str, session_id: str, budget: int) -> dict:
    body: dict = {"sessionId": session_id or "unknown"}
    if project:
        body["project"] = project
    if budget:
        body["budget"] = budget
    return await _call("POST", "/agentmemory/context", json=body)


# ---------------------------------------------------------------------------
# enrich — file-scoped cross-session context (pre-tool hook)
# ---------------------------------------------------------------------------


async def _enrich(file_paths: list[str], project: str, session_id: str) -> dict:
    return await _enrich_files(file_paths, project or _DEFAULT_PROJECT, session_id=session_id)


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
    p_find = sub.add_parser("find", help="Semantic search (4-step: compact→expand→hydrate)")
    p_find.add_argument("query")
    p_find.add_argument("--depth", choices=["normal", "deep"], default="normal")
    p_find.add_argument("--limit", type=int, default=10)
    p_find.add_argument("--files", default="", help="Comma-separated file paths for /enrich context")
    p_find.add_argument("--session-id", default="", dest="session_id", help="Current session ID for /enrich anchor")

    # save
    p_save = sub.add_parser("save", help="Save a curated memory")
    p_save.add_argument("content")
    p_save.add_argument("--type", dest="memory_type", default="architecture")
    p_save.add_argument("--tags", nargs="*", default=[])
    p_save.add_argument("--concepts", nargs="*", default=[])
    p_save.add_argument("--files", nargs="*", default=[], dest="save_files")
    p_save.add_argument("--strength", type=int, default=7, help="1-10, default 7 (use 9 for architecture/critical)")
    p_save.add_argument("--source-obs-ids", nargs="*", default=[], dest="source_obs_ids", help="Source observation IDs (enables provenance chain)")
    p_save.add_argument("--ttl-days", type=int, default=0, dest="ttl_days", help="Days until expiry (0 = no expiry)")

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

    # session-context
    p_ctx = sub.add_parser("session-context", help="Stateless context block via POST /context (no session created)")
    p_ctx.add_argument("--session-id", default="", dest="session_id")
    p_ctx.add_argument("--budget", type=int, default=0)

    # enrich
    p_enrich = sub.add_parser("enrich", help="File-scoped cross-session context (pre-tool hook, uses POST /enrich)")
    p_enrich.add_argument("files", nargs="+", help="File paths to enrich")
    p_enrich.add_argument("--session-id", default="", dest="session_id")

    args = parser.parse_args()

    try:
        if args.cmd == "find":
            result = asyncio.run(_find(args.query, args.project, args.depth, args.limit, args.files, args.session_id))
        elif args.cmd == "save":
            result = asyncio.run(_save(
                args.content, args.memory_type, args.project,
                args.tags, args.concepts, args.save_files,
                args.strength, args.source_obs_ids, args.ttl_days,
            ))
        elif args.cmd == "lesson":
            result = asyncio.run(_lesson(args.content, args.confidence, args.project, args.tags))
        elif args.cmd == "profile":
            result = asyncio.run(_profile(args.project))
        elif args.cmd == "sessions":
            result = asyncio.run(_sessions(args.query, args.project, args.limit))
        elif args.cmd == "session-context":
            result = asyncio.run(_session_context(args.project, args.session_id, args.budget))
        elif args.cmd == "enrich":
            result = asyncio.run(_enrich(args.files, args.project, args.session_id))
        _out(result)
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)
    except Exception as exc:
        _err(f"unexpected error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
