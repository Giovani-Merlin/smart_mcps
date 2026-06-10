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
  next                      Enriched frontier actions ready to execute
  task-context    <id>      Full execution packet for one action (4-tier hydration)
  task create               Create a new task with auto-linking
  task update     <id>      Update task status, result, or fields
  task list                 List tasks with optional status filter
  graph           <query>   Standalone graph traversal via POST /graph/query
  lessons-search  <query>   Search lessons by semantic query via POST /lessons/search
  insights-search <query>   Search insights by query via POST /insights/search
  timeline        <anchor>  Chronological observation window via POST /timeline
  audit                     Operation ledger via GET /audit
  commits                   Git commit linkage ledger via GET /commits
  session-by-commit <sha>   Sessions that produced a commit via GET /session/by-commit
  crystallize               Crystallize actions into a Crystal via POST /agentmemory/crystallize

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
  smart-mcps-agentmemory graph "authentication" --project smart_mcps --max-depth 2
  smart-mcps-agentmemory graph "jwt" --node-types concept,decision
  smart-mcps-agentmemory lessons-search "always test before push" --min-confidence 0.5 --limit 5
  smart-mcps-agentmemory insights-search "architecture patterns" --limit 8
  smart-mcps-agentmemory timeline devcontainer --before 4 --after 4
  smart-mcps-agentmemory audit --limit 100
  smart-mcps-agentmemory audit --operation consolidate --limit 20
  smart-mcps-agentmemory commits --limit 20
  smart-mcps-agentmemory session-by-commit abc1234
  smart-mcps-agentmemory crystallize --action-ids act_abc act_def

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


_SEMANTIC_OBS_TYPES = {
    "decision",
    "conversation",
    "subagent",
    "other",
    "architecture",
    "bug",
    "pattern",
    "code",
    "error",
}


def _is_useful_observation(obs: dict) -> bool:
    facts = obs.get("facts", [])
    if facts:
        return True
    return obs.get("type", "") in _SEMANTIC_OBS_TYPES


def _follow_memories(
    top_obs: list[dict], all_memories: list[dict], limit: int = 6
) -> list[dict]:
    top_obs_ids = {
        o.get("id") or o.get("obsId") for o in top_obs if o.get("id") or o.get("obsId")
    }
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
_DEFAULT_PROJECT = os.environ.get("AGENTMEMORY_PROJECT_NAME") or os.path.basename(_CWD)


def _out(data) -> None:
    print(json.dumps(data, indent=2, default=str))


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# find — 4-step progressive retrieval
# ---------------------------------------------------------------------------


async def _find(
    query: str,
    project: str = "",
    depth: str = "normal",
    limit: int = 10,
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
            "POST",
            "/agentmemory/smart-search",
            json={
                "query": query,
                "format": "compact",
                "limit": limit * 2,
                "project": proj,
            },
        )
        compact_obs = [
            r for r in compact_resp.get("results", []) if _is_useful_observation(r)
        ]
        bundled_lessons = compact_resp.get("lessons", [])
    except Exception:
        pass

    # Step 2 — expand top 3-5 hits for full content
    # expandIds must be objects: {obsId, sessionId} — bare strings silently return 0
    top_expand = [
        {"obsId": o.get("id") or o.get("obsId"), "sessionId": o.get("sessionId") or ""}
        for o in compact_obs[:5]
        if o.get("id") or o.get("obsId")
    ]
    full_obs = compact_obs
    if top_expand:
        try:
            expand_resp = await _call(
                "POST",
                "/agentmemory/smart-search",
                json={"expandIds": top_expand, "query": query, "project": proj},
            )
            expanded = [
                r for r in expand_resp.get("results", []) if _is_useful_observation(r)
            ]
            if expanded:
                expanded_ids = {r.get("id") or r.get("obsId") for r in expanded}
                remaining = [
                    o
                    for o in compact_obs[5:]
                    if (o.get("id") or o.get("obsId")) not in expanded_ids
                ]
                full_obs = expanded + remaining
        except Exception:
            pass


    matched_session_ids = {
        o.get("sessionId") for o in compact_obs[:5] if o.get("sessionId")
    }
    crystallized_ids = {
        o.get("crystallizedInto") for o in compact_obs[:5] if o.get("crystallizedInto")
    }
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

    mem_resp = (
        hydration_results[0]
        if not isinstance(hydration_results[0], Exception)
        else {"memories": []}
    )
    raw_insights = (
        hydration_results[1] if not isinstance(hydration_results[1], Exception) else []
    )
    all_crystals = (
        hydration_results[2] if not isinstance(hydration_results[2], Exception) else []
    )
    enrich_resp = (
        hydration_results[3]
        if len(hydration_results) > 3
        and not isinstance(hydration_results[3], Exception)
        else {}
    )

    all_memories = mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
    linked_memories = (
        _follow_memories(top_obs_for_linking, all_memories)
        if top_obs_for_linking
        else []
    )

    matched_crystals = [
        c
        for c in (all_crystals if isinstance(all_crystals, list) else [])
        if c.get("sessionId") in matched_session_ids or c.get("id") in crystallized_ids
    ]

    out: dict = {
        "observations": [
            _fmt_observation(o, o.get("score", 0.0)) for o in full_obs[:limit]
        ]
    }
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
                "POST",
                "/agentmemory/graph/query",
                json={"query": query, "maxDepth": 1, "project": proj},
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
    memory_type: str = "architecture",
    project: str = "",
    tags: list[str] | None = None,
    concepts: list[str] | None = None,
    files: list[str] | None = None,
    strength: int = 7,
    source_obs_ids: list[str] | None = None,
    ttl_days: int = 0,
) -> dict:
    payload: dict = {
        "content": content,
        "type": memory_type,
        "project": project or _DEFAULT_PROJECT,
        "tags": tags or [],
        "concepts": concepts or [],
        "files": files or [],
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


async def _lesson(
    content: str, confidence: float = 0.8, project: str = "", tags: list[str] | None = None
) -> dict:
    payload = {
        "content": content,
        "confidence": confidence,
        "project": project or _DEFAULT_PROJECT,
        "tags": tags or [],
    }
    result = await _call("POST", "/agentmemory/lessons", json=payload)
    lesson = result.get("lesson") or result
    return _fmt_lesson(lesson) if isinstance(lesson, dict) else result


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


async def _profile(project: str = "") -> dict:
    raw = await _call(
        "GET",
        "/agentmemory/profile",
        params={"project": project or _DEFAULT_PROJECT, "limit": 20},
    )
    # Backend wraps the profile under a "profile" key: {"profile": {...}, "reason": ...}
    # Unwrap so downstream code sees topConcepts/topFiles/sessionCount directly.
    result = raw.get("profile") or raw
    return {
        "topConcepts": (result.get("topConcepts") or [])[:15],
        "topFiles": (result.get("topFiles") or [])[:10],
        "recentActivity": (result.get("recentActivity") or [])[:5],
        "sessionCount": result.get("sessionCount"),
    }


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


async def _sessions(query: str, project: str = "", limit: int = 5) -> dict:
    payload = {
        "query": query,
        "limit": limit * 3,
        "format": "full",
        "project": project or _DEFAULT_PROJECT,
    }
    results = await _call("POST", "/agentmemory/smart-search", json=payload)
    observations = results.get("observations") or results.get("results") or []

    session_ids = list(
        {o.get("sessionId") for o in observations if o.get("sessionId")}
    )[:limit]
    sessions_resp = await _call(
        "GET", "/agentmemory/sessions", params={"project": project or _DEFAULT_PROJECT}
    )
    all_sessions = sessions_resp.get("sessions", [])

    matched = [
        s
        for s in all_sessions
        if s.get("id") in session_ids or s.get("sessionId") in session_ids
    ]
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


async def _session_context(project: str = "", session_id: str = "", budget: int = 0) -> dict:
    body: dict = {"sessionId": session_id or "unknown"}
    if project:
        body["project"] = project
    if budget:
        body["budget"] = budget
    return await _call("POST", "/agentmemory/context", json=body)


# ---------------------------------------------------------------------------
# enrich — file-scoped cross-session context (pre-tool hook)
# ---------------------------------------------------------------------------


async def _enrich(file_paths: list[str], project: str = "", session_id: str = "") -> dict:
    return await _enrich_files(
        file_paths, project or _DEFAULT_PROJECT, session_id=session_id
    )


# ---------------------------------------------------------------------------
# Action formatting helpers
# ---------------------------------------------------------------------------


def _fmt_action(a: dict) -> dict:
    ids = a.get("sourceMemoryIds") or []
    fmt = {
        "id": a.get("id"),
        "title": (a.get("title") or "")[:100],
        "description": (a.get("description") or "")[:300],
        "status": a.get("status"),
        "priority": a.get("priority"),
        "tags": a.get("tags", []),
        "createdAt": a.get("createdAt"),
    }
    if a.get("result"):
        fmt["result"] = a["result"]
    if ids:
        fmt["sourceMemoryIds"] = ids
    return fmt


def _fmt_frontier_entry(entry: dict) -> dict:
    fmt = _fmt_action(entry.get("action", {}))
    fmt["score"] = entry.get("score")
    fmt["leased"] = entry.get("leased", False)
    blockers = entry.get("blockers", [])
    if blockers:
        fmt["blockers"] = blockers
    return fmt


# ---------------------------------------------------------------------------
# _build_action_context — enriched context per action (includes crystals)
# ---------------------------------------------------------------------------


async def _build_action_context(action: dict, project: str = "") -> dict:
    title = action.get("title") or ""
    desc = (action.get("description") or "")[:120]
    query = f"{title} {desc}".strip()

    obs_body: dict = {"limit": 8, "format": "full"}
    if query:
        obs_body["query"] = query
    else:
        return {}

    if project:
        obs_body["project"] = project

    results = await asyncio.gather(
        _call("POST", "/agentmemory/smart-search", json=obs_body),
        _call(
            "GET",
            "/agentmemory/memories",
            params={"limit": 60, **({"project": project} if project else {})},
        ),
        _fetch_crystals(project, 20),
        return_exceptions=True,
    )

    obs_resp = results[0] if not isinstance(results[0], Exception) else {}
    mem_resp = results[1] if not isinstance(results[1], Exception) else {}
    all_crystals = results[2] if not isinstance(results[2], Exception) else []

    raw_obs = [
        r
        for r in (obs_resp.get("results", []) if isinstance(obs_resp, dict) else [])
        if _is_useful_observation(r)
    ]
    all_memories = mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
    linked_memories = _follow_memories(raw_obs[:5], all_memories)

    matched_session_ids = {
        o.get("sessionId") for o in raw_obs[:5] if o.get("sessionId")
    }
    matched_crystals = [
        c
        for c in (all_crystals if isinstance(all_crystals, list) else [])
        if c.get("sessionId") in matched_session_ids
    ]

    ctx: dict = {}
    if raw_obs:
        ctx["observations"] = [
            _fmt_observation(o, o.get("score", 0.0)) for o in raw_obs
        ]
    if linked_memories:
        ctx["memories"] = [_fmt_memory(m) for m in linked_memories]
    if matched_crystals:
        ctx["crystals"] = [_fmt_crystal(c) for c in matched_crystals]
    return ctx


# ---------------------------------------------------------------------------
# next — enriched frontier for orchestrators
# ---------------------------------------------------------------------------


async def _enrich_frontier_entry(entry: dict, project: str) -> dict:
    fmt = _fmt_frontier_entry(entry)
    try:
        ctx = await _build_action_context(entry.get("action", {}), project)
        if ctx:
            fmt["context"] = ctx
    except Exception:
        pass
    return fmt


async def _next(project: str = "", limit: int = 5, with_context: bool = True) -> dict:
    proj = project or _DEFAULT_PROJECT
    data = await _call(
        "GET", "/agentmemory/frontier", params={"project": proj, "limit": limit}
    )
    frontier_entries = data.get("frontier", []) if isinstance(data, dict) else []

    if not frontier_entries:
        return {"actions": []}

    if with_context:
        enriched = await asyncio.gather(
            *[_enrich_frontier_entry(e, proj) for e in frontier_entries],
            return_exceptions=True,
        )
        actions = [e for e in enriched if not isinstance(e, Exception)]
    else:
        actions = [_fmt_frontier_entry(e) for e in frontier_entries]

    return {"actions": actions}


# ---------------------------------------------------------------------------
# task-context — full execution packet per action (4-tier hydration)
# ---------------------------------------------------------------------------


async def _task_context(
    action_id: str,
    project: str = "",
    task: str = "",
    files: str = "",
    session_id: str = "",
) -> dict:
    proj = project or _DEFAULT_PROJECT
    file_list = [f.strip() for f in files.split(",") if f.strip()] if files else []

    action_record: dict = {}
    try:
        params: dict = {"limit": 100}
        if proj:
            params["project"] = proj
        actions_resp = await _call("GET", "/agentmemory/actions", params=params)
        for a in actions_resp.get("actions", []):
            if a.get("id") == action_id:
                action_record = a
                break
    except Exception:
        pass

    title = action_record.get("title", "")
    desc = (action_record.get("description") or "")[:150]
    query_text = task if task else f"{title} {desc}".strip()

    tier1_body: dict = {"limit": 12, "format": "full"}
    if query_text:
        tier1_body["query"] = query_text
    if proj:
        tier1_body["project"] = proj

    gather_tasks: list = [
        _call("POST", "/agentmemory/smart-search", json=tier1_body),
        _call(
            "GET",
            "/agentmemory/memories",
            params={"limit": 80, **({"project": proj} if proj else {})},
        ),
        _fetch_crystals(proj, 20),
        _fetch_lessons(proj, 15),
    ]
    enrich_idx = None
    if file_list:
        enrich_idx = len(gather_tasks)
        gather_tasks.append(
            _enrich_files(file_list, proj, session_id=session_id or "unknown")
        )

    results = await asyncio.gather(*gather_tasks, return_exceptions=True)

    search_resp = results[0] if not isinstance(results[0], Exception) else {}
    mem_resp = results[1] if not isinstance(results[1], Exception) else {}
    all_crystals = results[2] if not isinstance(results[2], Exception) else []
    all_lessons = results[3] if not isinstance(results[3], Exception) else []
    enrich_resp = (
        results[enrich_idx]
        if enrich_idx is not None and not isinstance(results[enrich_idx], Exception)
        else {}
    )

    raw_obs = [
        r
        for r in (
            search_resp.get("results", []) if isinstance(search_resp, dict) else []
        )
        if _is_useful_observation(r)
    ]
    top_obs = raw_obs[:6]
    all_memories = mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
    linked_memories = _follow_memories(top_obs, all_memories) if top_obs else []

    matched_session_ids = {o.get("sessionId") for o in top_obs if o.get("sessionId")}
    matched_crystals = [
        c
        for c in (all_crystals if isinstance(all_crystals, list) else [])
        if c.get("sessionId") in matched_session_ids
    ]

    action_fmt: dict = {}
    if action_record:
        action_fmt = {
            "id": action_record.get("id"),
            "title": (action_record.get("title") or "")[:100],
            "description": (action_record.get("description") or "")[:300],
            "status": action_record.get("status"),
            "priority": action_record.get("priority"),
            "tags": action_record.get("tags", []),
        }
        if action_record.get("result"):
            action_fmt["result"] = action_record["result"]

    out: dict = {
        "objective": query_text or title,
        "action": action_fmt,
        "relevant_observations": [
            _fmt_observation(o, o.get("score", 0.0)) for o in top_obs
        ],
    }
    if linked_memories:
        out["memories"] = [_fmt_memory(m) for m in linked_memories]
    if matched_crystals:
        out["crystals"] = [_fmt_crystal(c) for c in matched_crystals]
    if all_lessons and isinstance(all_lessons, list):
        out["lessons"] = [_fmt_lesson(l) for l in all_lessons]
    if enrich_resp and isinstance(enrich_resp, dict):
        enriched = enrich_resp.get("enrichedContext", [])
        bugs = enrich_resp.get("bugCandidates", [])
        if enriched:
            out["enriched_file_context"] = [_fmt_observation(o) for o in enriched]
        if bugs:
            out["bug_candidates"] = [_fmt_observation(o) for o in bugs]

    return out


# ---------------------------------------------------------------------------
# task create / update / list
# ---------------------------------------------------------------------------


async def _task_create(
    title: str,
    description: str = "",
    project: str = "",
    priority: int = 5,
    tags: list[str] | None = None,
    requires: list[str] | None = None,
    source_memory_ids: list[str] | None = None,
) -> dict:
    proj = project or _DEFAULT_PROJECT

    query_parts = [title] + list(tags or [])
    if description:
        query_parts.append(description[:80])
    search_q = " ".join(query_parts).strip()

    auto_ids: list[str] = []
    if search_q:
        try:
            obs_resp, mem_resp = await asyncio.gather(
                _call(
                    "POST",
                    "/agentmemory/smart-search",
                    json={
                        "query": search_q,
                        "format": "compact",
                        "limit": 6,
                        "project": proj,
                    },
                ),
                _call(
                    "GET",
                    "/agentmemory/memories",
                    params={"limit": 80, "project": proj},
                ),
            )
            raw_obs = [
                r for r in obs_resp.get("results", []) if _is_useful_observation(r)
            ]
            all_memories = (
                mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
            )
            linked = _follow_memories(raw_obs[:4], all_memories, limit=4)
            auto_ids = [m["id"] for m in linked if m.get("id")]
        except Exception:
            pass

    explicit = list(source_memory_ids or [])
    combined = explicit + [i for i in auto_ids if i not in explicit]
    final_source_ids = combined[:8]

    edges = [
        {"type": "requires", "targetActionId": rid} for rid in (requires or []) if rid
    ]

    payload: dict = {
        "title": title,
        "project": proj,
        "priority": max(1, min(10, priority)),
    }
    if description:
        payload["description"] = description
    if tags:
        payload["tags"] = tags
    if final_source_ids:
        payload["sourceMemoryIds"] = final_source_ids
    if edges:
        payload["edges"] = edges

    return await _call("POST", "/agentmemory/actions", json=payload)


async def _task_update(
    action_id: str,
    project: str = "",
    status: str = "",
    title: str = "",
    description: str = "",
    result: str = "",
    priority: int = 0,
) -> dict:
    proj = project or _DEFAULT_PROJECT
    payload: dict = {"actionId": action_id}
    if proj:
        payload["project"] = proj
    if status:
        payload["status"] = status
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    if result:
        payload["result"] = result
    if priority:
        payload["priority"] = max(1, min(10, priority))
    return await _call("POST", "/agentmemory/actions/update", json=payload)


async def _task_list(project: str = "", status: str = "", limit: int = 50) -> dict:
    proj = project or _DEFAULT_PROJECT
    resp = await _call(
        "GET", "/agentmemory/actions", params={"project": proj, "limit": limit}
    )
    actions = resp.get("actions", [])
    if status:
        actions = [a for a in actions if a.get("status") == status]
    return {
        "actions": [
            {
                "id": a.get("id"),
                "title": (a.get("title") or "")[:100],
                "status": a.get("status"),
                "priority": a.get("priority"),
                "tags": a.get("tags", []),
                **(
                    {"result": (a.get("result") or "")[:200]} if a.get("result") else {}
                ),
                "createdAt": a.get("createdAt"),
            }
            for a in actions
        ]
    }


# ---------------------------------------------------------------------------
# graph — standalone graph traversal (POST /graph/query)
# NOTE: the correct param name is `maxDepth` (NOT `depth`) — see SCENARIO_NOTES.md
# gotcha #8.  The existing _find --depth deep path sends `depth` which is a
# pre-existing bug (★B in implementation_plan.md §4); this subcommand uses the
# correct name from the start.
# ---------------------------------------------------------------------------


async def _graph(
    query: str,
    project: str = "",
    max_depth: int = 1,
    node_types: str = "",
) -> dict:
    proj = project or _DEFAULT_PROJECT
    body: dict = {"query": query, "maxDepth": max_depth, "project": proj}
    if node_types:
        body["nodeTypes"] = [t.strip() for t in node_types.split(",") if t.strip()]
    resp = await _call("POST", "/agentmemory/graph/query", json=body)
    return {
        "nodes": resp.get("nodes", [])[:20],
        "edges": resp.get("edges", [])[:30],
        "depth": resp.get("depth", max_depth),
        "total_nodes": len(resp.get("nodes", [])),
        "total_edges": len(resp.get("edges", [])),
    }


# ---------------------------------------------------------------------------
# lessons-search — POST /lessons/search
# NOTE: minConfidence and limit MUST be JSON numbers (not strings) — gotcha #7.
# ---------------------------------------------------------------------------


async def _lessons_search(
    query: str,
    project: str = "",
    min_confidence: float = 0.3,
    limit: int = 10,
) -> dict:
    proj = project or _DEFAULT_PROJECT
    body: dict = {
        "query": query,
        "project": proj,
        "minConfidence": min_confidence,  # float, not str — gotcha #7
        "limit": limit,                   # int, not str — gotcha #7
    }
    resp = await _call("POST", "/agentmemory/lessons/search", json=body)
    lessons = resp.get("lessons", [])
    return {"lessons": [_fmt_lesson(l) for l in lessons]}


# ---------------------------------------------------------------------------
# insights-search — POST /insights/search
# ---------------------------------------------------------------------------


async def _insights_search(
    query: str,
    project: str = "",
    limit: int = 8,
) -> dict:
    proj = project or _DEFAULT_PROJECT
    body: dict = {"query": query, "project": proj}
    resp = await _call("POST", "/agentmemory/insights/search", json=body)
    insights = resp.get("insights", [])[:limit]
    return {"insights": [_fmt_insight(i) for i in insights]}


# ---------------------------------------------------------------------------
# timeline — chronological observation window (POST /timeline)
# ---------------------------------------------------------------------------


async def _timeline(
    anchor: str,
    project: str = "",
    before: int = 4,
    after: int = 4,
) -> dict:
    proj = project or _DEFAULT_PROJECT
    body: dict = {
        "anchor": anchor,
        "project": proj,
        "before": before,
        "after": after,
    }
    resp = await _call("POST", "/agentmemory/timeline", json=body)
    return resp


# ---------------------------------------------------------------------------
# audit — operation ledger (GET /audit)
# ---------------------------------------------------------------------------


async def _audit(
    limit: int = 100,
    operation: str = "",
) -> dict:
    params: dict = {"limit": limit}
    if operation:
        params["operation"] = operation
    resp = await _call("GET", "/agentmemory/audit", params=params)
    return resp


# ---------------------------------------------------------------------------
# commits — git commit linkage ledger (GET /commits)
# ---------------------------------------------------------------------------


async def _commits(limit: int = 20) -> dict:
    resp = await _call("GET", "/agentmemory/commits", params={"limit": limit})
    return resp


# ---------------------------------------------------------------------------
# session-by-commit — sessions that produced a commit (GET /session/by-commit)
# ---------------------------------------------------------------------------


async def _session_by_commit(sha: str) -> dict:
    resp = await _call("GET", "/agentmemory/session/by-commit", params={"sha": sha})
    return resp


# ---------------------------------------------------------------------------
# crystallize — create a Crystal from completed actions
# POST /agentmemory/crystallize — returns 404 on this instance (crystallize
# is configured to run automatically server-side).  The CLI subcommand is kept
# so callers can trigger it explicitly when needed; 404 is a non-fatal outcome.
# ---------------------------------------------------------------------------


async def _crystallize(
    action_ids: list[str],
    project: str = "",
    session_id: str = "",
) -> dict:
    body: dict = {"actionIds": action_ids, "project": project or _DEFAULT_PROJECT}
    if session_id:
        body["sessionId"] = session_id
    try:
        return await _call("POST", "/agentmemory/crystallize", json=body)
    except Exception as exc:
        return {"error": str(exc), "note": "crystallize runs automatically server-side on this instance"}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="smart-mcps-agentmemory",
        description="AgentMemory CLI — query and write to the agentmemory backend",
    )
    parser.add_argument(
        "--project", default="", help="Project name (default: cwd basename)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # find
    p_find = sub.add_parser(
        "find", help="Semantic search (4-step: compact→expand→hydrate)"
    )
    p_find.add_argument("query")
    p_find.add_argument("--depth", choices=["normal", "deep"], default="normal")
    p_find.add_argument("--limit", type=int, default=10)
    p_find.add_argument(
        "--files", default="", help="Comma-separated file paths for /enrich context"
    )
    p_find.add_argument(
        "--session-id",
        default="",
        dest="session_id",
        help="Current session ID for /enrich anchor",
    )

    # save
    p_save = sub.add_parser("save", help="Save a curated memory")
    p_save.add_argument("content")
    p_save.add_argument("--type", dest="memory_type", default="architecture")
    p_save.add_argument("--tags", nargs="*", default=[])
    p_save.add_argument("--concepts", nargs="*", default=[])
    p_save.add_argument("--files", nargs="*", default=[], dest="save_files")
    p_save.add_argument(
        "--strength",
        type=int,
        default=7,
        help="1-10, default 7 (use 9 for architecture/critical)",
    )
    p_save.add_argument(
        "--source-obs-ids",
        nargs="*",
        default=[],
        dest="source_obs_ids",
        help="Source observation IDs (enables provenance chain)",
    )
    p_save.add_argument(
        "--ttl-days",
        type=int,
        default=0,
        dest="ttl_days",
        help="Days until expiry (0 = no expiry)",
    )

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
    p_ctx = sub.add_parser(
        "session-context",
        help="Stateless context block via POST /context (no session created)",
    )
    p_ctx.add_argument("--session-id", default="", dest="session_id")
    p_ctx.add_argument("--budget", type=int, default=0)

    # enrich
    p_enrich = sub.add_parser(
        "enrich",
        help="File-scoped cross-session context (pre-tool hook, uses POST /enrich)",
    )
    p_enrich.add_argument("files", nargs="+", help="File paths to enrich")
    p_enrich.add_argument("--session-id", default="", dest="session_id")

    # next
    p_next = sub.add_parser("next", help="Enriched frontier actions ready to execute")
    p_next.add_argument("--limit", type=int, default=5)
    p_next.add_argument(
        "--no-context",
        action="store_true",
        dest="no_context",
        help="Skip memory enrichment per action",
    )

    # task-context
    p_task_ctx = sub.add_parser(
        "task-context", help="Full execution packet for one action (4-tier hydration)"
    )
    p_task_ctx.add_argument("action_id")
    p_task_ctx.add_argument(
        "--task",
        default="",
        help="Task description override (fallback query when no sourceMemoryIds)",
    )
    p_task_ctx.add_argument(
        "--files", default="", help="Comma-separated file paths for /enrich context"
    )
    p_task_ctx.add_argument("--session-id", default="", dest="session_id")

    # task (nested subcommands: create / update / list)
    p_task = sub.add_parser("task", help="Task management: create, update, list")
    task_sub = p_task.add_subparsers(dest="task_op", required=True)

    p_task_create = task_sub.add_parser(
        "create", help="Create a new task with auto-linking"
    )
    p_task_create.add_argument("--title", required=True, help="Task title")
    p_task_create.add_argument(
        "--description", default="", help="Plain-text objective (no context dumps)"
    )
    p_task_create.add_argument(
        "--priority", type=int, default=5, help="Priority 1-10 (default 5)"
    )
    p_task_create.add_argument("--tags", nargs="*", default=[], help="Domain tags")
    p_task_create.add_argument(
        "--requires", nargs="*", default=[], help="Action IDs this task depends on"
    )
    p_task_create.add_argument(
        "--source-memory-ids",
        nargs="*",
        default=[],
        dest="source_memory_ids",
        help="Explicit memory IDs to link",
    )

    p_task_update = task_sub.add_parser(
        "update", help="Update task status, result, or fields"
    )
    p_task_update.add_argument("action_id", help="Action ID to update")
    p_task_update.add_argument(
        "--status",
        choices=["pending", "active", "done", "blocked", "cancelled"],
        default="",
    )
    p_task_update.add_argument("--title", default="")
    p_task_update.add_argument("--description", default="")
    p_task_update.add_argument(
        "--result",
        default="",
        help="Outcome summary (feeds future task-context enrichment)",
    )
    p_task_update.add_argument("--priority", type=int, default=0)

    p_task_list = task_sub.add_parser(
        "list", help="List tasks with optional status filter"
    )
    p_task_list.add_argument(
        "--status",
        choices=["pending", "active", "done", "blocked", "cancelled"],
        default="",
    )
    p_task_list.add_argument("--limit", type=int, default=50)

    # graph
    p_graph = sub.add_parser(
        "graph",
        help="Standalone knowledge-graph traversal via POST /graph/query",
    )
    p_graph.add_argument("query", help="Concept, file, function, or free-form description")
    p_graph.add_argument(
        "--max-depth",
        type=int,
        default=1,
        dest="max_depth",
        help="Traversal depth (1=direct neighbors, 2=two hops). Sends as `maxDepth` — NOT `depth`.",
    )
    p_graph.add_argument(
        "--node-types",
        default="",
        dest="node_types",
        help="Comma-separated node-type filter e.g. concept,function,decision,file",
    )

    # lessons-search
    p_lsearch = sub.add_parser(
        "lessons-search",
        help="Search lessons by semantic query via POST /lessons/search",
    )
    p_lsearch.add_argument("query")
    p_lsearch.add_argument(
        "--min-confidence",
        type=float,
        default=0.3,
        dest="min_confidence",
        help="Minimum confidence threshold (float, default 0.3)",
    )
    p_lsearch.add_argument("--limit", type=int, default=10)

    # insights-search
    p_isearch = sub.add_parser(
        "insights-search",
        help="Search insights by query via POST /insights/search",
    )
    p_isearch.add_argument("query")
    p_isearch.add_argument("--limit", type=int, default=8)

    # timeline
    p_timeline = sub.add_parser(
        "timeline",
        help="Chronological observation window around an anchor via POST /timeline",
    )
    p_timeline.add_argument(
        "anchor",
        help="Concept keyword or ISO date to anchor the timeline window",
    )
    p_timeline.add_argument(
        "--before", type=int, default=4, help="Observations before the anchor (default 4)"
    )
    p_timeline.add_argument(
        "--after", type=int, default=4, help="Observations after the anchor (default 4)"
    )

    # audit
    p_audit = sub.add_parser(
        "audit",
        help="Operation ledger — every memory mutation with targetIds and details",
    )
    p_audit.add_argument("--limit", type=int, default=100)
    p_audit.add_argument(
        "--operation",
        default="",
        help="Filter by operation type e.g. consolidate, evolve, delete, lesson_save",
    )

    # commits
    p_commits = sub.add_parser(
        "commits",
        help="Git commit linkage ledger via GET /commits",
    )
    p_commits.add_argument("--limit", type=int, default=20)

    # session-by-commit
    p_sbc = sub.add_parser(
        "session-by-commit",
        help="Sessions that produced a given git commit via GET /session/by-commit",
    )
    p_sbc.add_argument("sha", help="Git commit SHA (full or prefix)")

    # crystallize
    p_crystallize = sub.add_parser(
        "crystallize",
        help="Create a Crystal from completed actions via POST /agentmemory/crystallize",
    )
    p_crystallize.add_argument(
        "--action-ids",
        nargs="+",
        required=True,
        dest="action_ids",
        help="One or more action IDs to crystallize",
    )
    p_crystallize.add_argument(
        "--session-id",
        default="",
        dest="session_id",
        help="Session ID to associate with the Crystal",
    )

    args = parser.parse_args()

    try:
        if args.cmd == "find":
            result = asyncio.run(
                _find(
                    args.query,
                    args.project,
                    args.depth,
                    args.limit,
                    args.files,
                    args.session_id,
                )
            )
        elif args.cmd == "save":
            result = asyncio.run(
                _save(
                    args.content,
                    args.memory_type,
                    args.project,
                    args.tags,
                    args.concepts,
                    args.save_files,
                    args.strength,
                    args.source_obs_ids,
                    args.ttl_days,
                )
            )
        elif args.cmd == "lesson":
            result = asyncio.run(
                _lesson(args.content, args.confidence, args.project, args.tags)
            )
        elif args.cmd == "profile":
            result = asyncio.run(_profile(args.project))
        elif args.cmd == "sessions":
            result = asyncio.run(_sessions(args.query, args.project, args.limit))
        elif args.cmd == "session-context":
            result = asyncio.run(
                _session_context(args.project, args.session_id, args.budget)
            )
        elif args.cmd == "enrich":
            result = asyncio.run(_enrich(args.files, args.project, args.session_id))
        elif args.cmd == "next":
            result = asyncio.run(_next(args.project, args.limit, not args.no_context))
        elif args.cmd == "task-context":
            result = asyncio.run(
                _task_context(
                    args.action_id, args.project, args.task, args.files, args.session_id
                )
            )
        elif args.cmd == "task":
            if args.task_op == "create":
                result = asyncio.run(
                    _task_create(
                        args.title,
                        args.description,
                        args.project,
                        args.priority,
                        args.tags,
                        args.requires,
                        args.source_memory_ids,
                    )
                )
            elif args.task_op == "update":
                result = asyncio.run(
                    _task_update(
                        args.action_id,
                        args.project,
                        args.status,
                        args.title,
                        args.description,
                        args.result,
                        args.priority,
                    )
                )
            elif args.task_op == "list":
                result = asyncio.run(_task_list(args.project, args.status, args.limit))
        elif args.cmd == "graph":
            result = asyncio.run(
                _graph(args.query, args.project, args.max_depth, args.node_types)
            )
        elif args.cmd == "lessons-search":
            result = asyncio.run(
                _lessons_search(args.query, args.project, args.min_confidence, args.limit)
            )
        elif args.cmd == "insights-search":
            result = asyncio.run(
                _insights_search(args.query, args.project, args.limit)
            )
        elif args.cmd == "timeline":
            result = asyncio.run(
                _timeline(args.anchor, args.project, args.before, args.after)
            )
        elif args.cmd == "audit":
            result = asyncio.run(_audit(args.limit, args.operation))
        elif args.cmd == "commits":
            result = asyncio.run(_commits(args.limit))
        elif args.cmd == "session-by-commit":
            result = asyncio.run(_session_by_commit(args.sha))
        elif args.cmd == "crystallize":
            result = asyncio.run(
                _crystallize(args.action_ids, args.project, args.session_id)
            )
        _out(result)
    except RuntimeError as exc:
        _err(str(exc))
        sys.exit(1)
    except Exception as exc:
        _err(f"unexpected error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
