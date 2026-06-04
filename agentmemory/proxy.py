#!/usr/bin/env python3
"""
AgentMemory FastMCP Proxy — 10-tool engine-first interface.

Design principle: the MCP is a router and response shaper, not a search engine.
All relevance ranking is delegated to the agentmemory engine (BM25+vector via
/smart-search, file-graph via /enrich). The only client-side filtering is a
weighted concept/file/lineage set-intersection to follow observation hits into
unindexed memory stores that the engine cannot score natively.

Tools:
  memory_find          — general semantic recall (4-step: compact→expandIds→parallel hydration)
  memory_task_context  — full execution packet for one action
  memory_save          — durable curated memory write → POST /remember
  memory_lesson_save   — confidence-scored lesson write → POST /lessons (different store!)
  memory_next          — enriched frontier for orchestrators
  memory_update_task   — create/update/complete/block/cancel actions
  memory_sessions_find — semantic session recovery
  memory_profile       — project snapshot with lessons/insights/frontier
  memory_session_context — stateless context block via POST /context (zero mutation)
  memory_graph_query   — knowledge graph structural traversal
"""

import asyncio
import os

import httpx
from fastmcp import FastMCP

BASE_URL = os.environ.get("AGENTMEMORY_URL", "http://localhost:3111").rstrip("/")
TIMEOUT = 30.0

mcp = FastMCP(
    "agentmemory-proxy",
    instructions=(
        "Agentmemory MCP proxy — 10 tools. "
        "memory_find: 4-step retrieval (compact→expandIds→parallel: memories+insights/search+crystals by sessionId+enrich). "
        "memory_task_context: full execution packet for one action (expandIds if sourceMemoryIds exist, else query). "
        "memory_save: save durable curated memory → POST /remember; include strength, concepts, files, sourceObservationIds. "
        "memory_lesson_save: save confidence-scored lesson → POST /lessons; strengthens on reinforcement. "
        "memory_next: enriched frontier for orchestrators — each action has a pre-loaded context field. "
        "memory_update_task: create/update/complete/block/cancel actions via operation= dispatch. "
        "memory_sessions_find: recover prior sessions by semantic topic (not UUID). "
        "memory_profile: project snapshot at session start or before planning. "
        "memory_session_context: stateless POST /context — no session created, no context cleared. "
        "memory_graph_query: advanced structural traversal — not needed in the standard flow."
    ),
)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


async def _call(method: str, path: str, **kwargs) -> dict:
    """Single HTTP call; raises clearly if the daemon is unreachable.

    WHY a fresh client per call: FastMCP tools are called from different async
    contexts and we don't want shared connection-pool state causing subtle bugs.
    """
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


async def _noop(value):
    """No-op coroutine that returns a fixed value — replaces asyncio.coroutine lambdas."""
    return value


# ---------------------------------------------------------------------------
# Response formatters — compact representations to reduce context window usage
# ---------------------------------------------------------------------------


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


def _fmt_action(a: dict) -> dict:
    ids = a.get("sourceMemoryIds") or []
    return {
        "id": a.get("id"),
        "title": a.get("title"),
        "description": (a.get("description") or "")[:300],
        "status": a.get("status"),
        "priority": a.get("priority"),
        "tags": a.get("tags", []),
        "result": a.get("result"),
        "createdAt": a.get("createdAt"),
        **({"sourceMemoryIds": ids} if ids else {}),
    }


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


# ---------------------------------------------------------------------------
# Internal helpers — not exposed as MCP tools
# ---------------------------------------------------------------------------


_SEMANTIC_OBS_TYPES = {"decision", "conversation", "subagent", "other", "architecture", "bug", "pattern", "code", "error"}


def _is_useful_observation(obs: dict) -> bool:
    """True only if the observation has semantic content worth surfacing.

    WHY: command_run/file_read/file_write without facts are pure action-records
    (noise). Semantic types like decision/subagent/other may carry useful context
    even when facts[] is empty due to summarization pipeline tombstoning.
    """
    facts = obs.get("facts", [])
    if facts:
        return True
    return obs.get("type", "") in _SEMANTIC_OBS_TYPES


def _follow_memories(
    top_obs: list[dict], all_memories: list[dict], limit: int = 6
) -> list[dict]:
    """Follow observation hits into unindexed memory store via weighted link fields.

    Scoring:
      sourceObservationIds ∩ top_obs_ids → 3pts (lineage — strongest signal)
      sessionId ∈ memory.sessionIds      → 2pts (episodic)
      concepts[] ∩ top_concepts          → 1pt  (structural)
      files[] ∩ top_files                → 1pt  (structural)

    Only isLatest=true memories are considered (older versions are stale).
    Sort: weighted_score desc, then strength desc as tiebreaker.
    """
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


async def _fetch_insights(project: str = "", limit: int = 8) -> list:
    try:
        params: dict = {"limit": limit, "minConfidence": 0.1}
        if project:
            params["project"] = project
        resp = await _call("GET", "/agentmemory/insights", params=params)
        return resp.get("insights", [])
    except Exception:
        return []


async def _search_insights(query: str, project: str = "", limit: int = 8) -> list:
    """BM25 search over insight content via POST /insights/search (separate from GET /insights)."""
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
    """Call /enrich anchored to session_id ("unknown" fallback — required by API).

    Returns the raw enrich response dict (enrichedContext, bugCandidates, bridgingMemories).
    """
    body: dict = {"sessionId": session_id or "unknown", "files": files}
    if project:
        body["project"] = project
    if terms:
        body["terms"] = terms
    try:
        return await _call("POST", "/agentmemory/enrich", json=body)
    except Exception:
        return {}


async def _build_action_context(action: dict, project: str = "") -> dict:
    """Build memory context for one action — used by memory_next and memory_task_context."""
    ids = action.get("sourceMemoryIds") or []
    title = (action.get("title") or "")
    desc = (action.get("description") or "")[:120]
    query = f"{title} {desc}".strip()

    obs_body: dict = {"limit": 8, "format": "full"}
    if ids:
        obs_body["expandIds"] = ids
        if query:
            obs_body["query"] = query
    elif query:
        obs_body["query"] = query
    else:
        return {}

    if project:
        obs_body["project"] = project

    try:
        obs_resp, mem_resp = await asyncio.gather(
            _call("POST", "/agentmemory/smart-search", json=obs_body),
            _call("GET", "/agentmemory/memories", params={"limit": 60, **({"project": project} if project else {})}),
        )
    except Exception:
        return {}

    raw_obs = [r for r in obs_resp.get("results", []) if _is_useful_observation(r)]
    all_memories = mem_resp.get("memories", [])
    linked_memories = _follow_memories(raw_obs[:5], all_memories)

    ctx: dict = {}
    if raw_obs:
        ctx["observations"] = [_fmt_observation(o, o.get("score", 0.0)) for o in raw_obs]
    if linked_memories:
        ctx["memories"] = [_fmt_memory(m) for m in linked_memories]
    return ctx


# ---------------------------------------------------------------------------
# Tool 1: memory_find — general semantic recall
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_find(
    query: str = "",
    project: str = "",
    files: str = "",
    limit: int = 10,
    depth: str = "normal",
    session_id: str = "",
) -> dict:
    """General semantic recall — the default retrieval tool for all agents.

    4-step progressive retrieval:
      Step 1 — Compact discovery: smart-search format=compact → scored hits + bundled lessons
      Step 2 — Expansion: expandIds on top 3-5 hits → full narrative/facts/concepts/files
      Step 3 — Parallel hydration: memories (weighted link-follow) + insights/search (BM25)
                + crystals (filtered by matched sessionIds + crystallizedInto) + enrich (if files)
      Step 4 — deep only: graph/query for structural neighbors

    query: text query (at least one of query or files is required)
    files: comma-separated file paths — triggers POST /enrich for file-scoped cross-session context
    depth: "normal" = steps 1-3; "deep" = adds graph traversal (step 4)
    session_id: current session ID for /enrich anchor (uses "unknown" if omitted)
    """
    file_list = [f.strip() for f in files.split(",") if f.strip()]
    if not query and not file_list:
        return {"error": "At least one of query or files is required."}

    # Step 1 — Compact discovery
    compact_obs: list[dict] = []
    bundled_lessons: list[dict] = []
    if query:
        compact_body: dict = {"query": query, "format": "compact", "limit": limit * 2}
        if project:
            compact_body["project"] = project
        try:
            compact_resp = await _call("POST", "/agentmemory/smart-search", json=compact_body)
            compact_obs = [r for r in compact_resp.get("results", []) if _is_useful_observation(r)]
            bundled_lessons = compact_resp.get("lessons", [])
        except Exception:
            pass

    # Step 2 — Expand top 3-5 hits for full content
    top_ids = [o.get("id") or o.get("obsId") for o in compact_obs[:5] if o.get("id") or o.get("obsId")]
    full_obs = compact_obs
    if top_ids and query:
        try:
            expand_body: dict = {"expandIds": top_ids, "query": query}
            if project:
                expand_body["project"] = project
            expand_resp = await _call("POST", "/agentmemory/smart-search", json=expand_body)
            expanded = [r for r in expand_resp.get("results", []) if _is_useful_observation(r)]
            if expanded:
                expanded_ids = {r.get("id") or r.get("obsId") for r in expanded}
                remaining = [o for o in compact_obs[5:] if (o.get("id") or o.get("obsId")) not in expanded_ids]
                full_obs = expanded + remaining
        except Exception:
            pass

    # Anchors for step 3
    matched_session_ids = {o.get("sessionId") for o in compact_obs[:5] if o.get("sessionId")}
    crystallized_ids = {o.get("crystallizedInto") for o in compact_obs[:5] if o.get("crystallizedInto")}
    top_obs_for_linking = full_obs[:5]

    # Step 3 — Parallel hydration
    hydration_tasks = [
        _call("GET", "/agentmemory/memories", params={"limit": 80, **({"project": project} if project else {})}),
        _search_insights(query, project, limit) if query else _noop([]),
        _fetch_crystals(project, 20),
        _enrich_files(file_list, project, session_id=session_id) if file_list else _noop({}),
    ]
    hydration_results = await asyncio.gather(*hydration_tasks, return_exceptions=True)

    mem_resp = hydration_results[0] if not isinstance(hydration_results[0], Exception) else {"memories": []}
    raw_insights = hydration_results[1] if not isinstance(hydration_results[1], Exception) else []
    all_crystals = hydration_results[2] if not isinstance(hydration_results[2], Exception) else []
    enrich_resp = hydration_results[3] if not isinstance(hydration_results[3], Exception) else {}

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
        bridging = enrich_resp.get("bridgingMemories", [])
        if enriched:
            out["enriched_file_context"] = [_fmt_observation(o) for o in enriched]
        if bugs:
            out["bug_candidates"] = [_fmt_observation(o) for o in bugs]
        if bridging:
            out["bridging_memories"] = [_fmt_memory(m) for m in bridging]

    # Step 4 — deep only: structural graph traversal
    if depth == "deep" and query:
        try:
            graph_body: dict = {"query": query, "depth": 1}
            if project:
                graph_body["project"] = project
            graph_resp = await _call("POST", "/agentmemory/graph/query", json=graph_body)
            nodes = graph_resp.get("nodes", [])[:20]
            edges = graph_resp.get("edges", [])[:30]
            if nodes or edges:
                out["graph_neighbors"] = {"nodes": nodes, "edges": edges}
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------------
# Tool 2: memory_task_context — full execution packet for one action
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_task_context(
    action_id: str = "",
    task: str = "",
    project: str = "",
    files: str = "",
    agent_id: str = "",
    session_id: str = "",
) -> dict:
    """Full context packet for executing one action.

    Prefer over memory_find when you have an action_id — the action's
    sourceMemoryIds enable engine-native expandIds expansion (direct KV fetch,
    no vector overhead) which is richer and faster than a text query.

    action_id: ID of an action from the DAG. If the action has sourceMemoryIds,
               those are expanded directly via expandIds.
    task: free-form task description — used as fallback query when no action_id
          is provided, or when the action has no sourceMemoryIds.
    files: comma-separated file paths for additional /enrich context.
    session_id: current session ID for /enrich anchor.
    """
    file_list = [f.strip() for f in files.split(",") if f.strip()]

    action_record: dict = {}
    source_ids: list = []
    query_text = task

    if action_id:
        try:
            params: dict = {"limit": 100}
            if project:
                params["project"] = project
            actions_resp = await _call("GET", "/agentmemory/actions", params=params)
            for a in actions_resp.get("actions", []):
                if a.get("id") == action_id:
                    action_record = a
                    break
        except Exception:
            pass

        source_ids = action_record.get("sourceMemoryIds") or []
        if not query_text:
            title = action_record.get("title", "")
            desc = (action_record.get("description") or "")[:150]
            query_text = f"{title} {desc}".strip()

    if not query_text and not source_ids and not file_list:
        return {"error": "Provide action_id, task, or files."}

    obs_body: dict = {"limit": 12, "format": "full"}
    if source_ids:
        obs_body["expandIds"] = source_ids
    if query_text:
        obs_body["query"] = query_text
    if project:
        obs_body["project"] = project

    tasks_coros = [
        _call("POST", "/agentmemory/smart-search", json=obs_body),
        _call("GET", "/agentmemory/memories", params={"limit": 80, **({"project": project} if project else {})}),
        _fetch_lessons(project, 8),
    ]
    if file_list:
        tasks_coros.append(_enrich_files(file_list, project, session_id=session_id))

    results = await asyncio.gather(*tasks_coros, return_exceptions=True)

    obs_resp = results[0] if not isinstance(results[0], Exception) else {"results": []}
    mem_resp = results[1] if not isinstance(results[1], Exception) else {"memories": []}
    raw_lessons = results[2] if not isinstance(results[2], Exception) else []
    enrich_resp = results[3] if len(results) > 3 and not isinstance(results[3], Exception) else {}

    raw_obs = [r for r in obs_resp.get("results", []) if _is_useful_observation(r)]
    all_memories = mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
    linked_memories = _follow_memories(raw_obs[:6], all_memories)

    out: dict = {"objective": query_text or action_record.get("title", "")}
    if action_record:
        out["action"] = _fmt_action(action_record)
    if raw_obs:
        out["relevant_observations"] = [_fmt_observation(o, o.get("score", 0.0)) for o in raw_obs[:10]]
    if linked_memories:
        out["memories"] = [_fmt_memory(m) for m in linked_memories]
    if raw_lessons:
        out["lessons"] = [_fmt_lesson(l) for l in raw_lessons]

    if enrich_resp:
        enriched = enrich_resp.get("enrichedContext", [])
        bugs = enrich_resp.get("bugCandidates", [])
        bridging = enrich_resp.get("bridgingMemories", [])
        if enriched:
            out["enriched_file_context"] = [_fmt_observation(o) for o in enriched]
        if bugs:
            out["bug_candidates"] = [_fmt_observation(o) for o in bugs]
        if bridging:
            out["bridging_memories"] = [_fmt_memory(m) for m in bridging]

    return out


# ---------------------------------------------------------------------------
# Tool 3: memory_save — durable memory write
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_save(
    content: str,
    memory_type: str = "fact",
    title: str = "",
    concepts: str = "",
    files: str = "",
    project: str = "",
    agent_id: str = "",
    strength: int = 7,
    source_observation_ids: str = "",
    ttl_days: int = 0,
) -> dict:
    """Save a curated memory — for non-obvious decisions, constraints, and patterns.

    memory_type: architecture | workflow | fact | preference | pattern | procedure | constraint | bug
    strength: 1-10, default 7. Salience: architecture(9+) > preference(8) > pattern(7) > bug(6) > fact(5)
    source_observation_ids: comma-separated observation IDs (enables provenance chain)
    ttl_days: days until expiry (0 = no expiry)
    concepts: comma-separated key concepts (2-5 specific phrases, e.g. "jwt-refresh-rotation")
    files: comma-separated exact file paths this memory relates to
    """
    body: dict = {
        "content": content,
        "type": memory_type,
        "concepts": [c.strip() for c in concepts.split(",") if c.strip()],
        "files": [f.strip() for f in files.split(",") if f.strip()],
        "strength": max(1, min(10, strength)),
    }
    if title:
        body["title"] = title
    if project:
        body["project"] = project
    if agent_id:
        body["agentId"] = agent_id
    src_obs_ids = [s.strip() for s in source_observation_ids.split(",") if s.strip()]
    if src_obs_ids:
        body["sourceObservationIds"] = src_obs_ids
    if ttl_days > 0:
        body["ttlDays"] = ttl_days
    return await _call("POST", "/agentmemory/remember", json=body)


# ---------------------------------------------------------------------------
# Tool 3b: memory_lesson_save — confidence-scored lesson write
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_lesson_save(
    content: str,
    context: str = "",
    confidence: float = 0.7,
    project: str = "",
    tags: str = "",
) -> dict:
    """Save a lesson learned — a different store from memory_save.

    Lessons have confidence scores (0.0–1.0) that automatically strengthen when the
    same insight is reinforced across sessions. Use for: recurring constraints,
    known gotchas, architectural decisions agents should keep re-encountering.

    memory_save → POST /remember (curated memories, immutable, link-followed).
    memory_lesson_save → POST /lessons (confidence-scored, self-strengthening).

    confidence: 0.0–1.0, default 0.7. Auto-increases when a lesson is re-saved.
    context: where/when this lesson applies (e.g. "during pose preprocessing").
    tags: comma-separated, e.g. "area:model,type:constraint".
    """
    body: dict = {"content": content, "confidence": confidence}
    if context:
        body["context"] = context
    if project:
        body["project"] = project
    if tags:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    return await _call("POST", "/agentmemory/lessons", json=body)


# ---------------------------------------------------------------------------
# Tool 4: memory_next — enriched frontier for orchestrators
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_next(
    project: str = "",
    limit: int = 5,
    include_context: bool = True,
) -> dict:
    """Get unblocked, highest-priority actions ready to execute, with memory context.

    Each returned action has a 'context' field pre-loaded with relevant observations
    and memories — pass it directly to workers. No separate memory_find call needed
    per action.
    """
    params: dict = {"limit": limit}
    if project:
        params["project"] = project
    data = await _call("GET", "/agentmemory/frontier", params=params)
    frontier_entries = data.get("frontier", []) if isinstance(data, dict) else []

    if not frontier_entries:
        return data if isinstance(data, dict) else {"actions": []}

    if include_context:
        enriched = await asyncio.gather(
            *[_enrich_frontier_entry(e, project) for e in frontier_entries]
        )
        data["actions"] = list(enriched)
    else:
        data["actions"] = [_fmt_frontier_entry(e) for e in frontier_entries]

    return data


def _fmt_frontier_entry(entry: dict) -> dict:
    """Format a frontier envelope {action, blockers, leased, score} into a flat dict."""
    fmt = _fmt_action(entry.get("action", {}))
    fmt["score"] = entry.get("score")
    fmt["leased"] = entry.get("leased", False)
    blockers = entry.get("blockers", [])
    if blockers:
        fmt["blockers"] = blockers
    return fmt


async def _enrich_frontier_entry(entry: dict, project: str) -> dict:
    fmt = _fmt_frontier_entry(entry)
    try:
        ctx = await _build_action_context(entry.get("action", {}), project)
        if ctx:
            fmt["context"] = ctx
    except Exception:
        pass  # enrichment failure must never block the frontier response
    return fmt


# ---------------------------------------------------------------------------
# Tool 5: memory_update_task — unified action DAG mutations
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_update_task(
    operation: str,
    action_id: str = "",
    title: str = "",
    description: str = "",
    project: str = "",
    priority: int = 5,
    tags: str = "",
    requires: str = "",
    result: str = "",
    source_memory_ids: str = "",
) -> dict:
    """Create, update, complete, block, or cancel a task action.

    operation: create | update | complete | block | cancel

    For create:
      title, description, priority (1-10), tags (comma-sep), requires (comma-sep
      action IDs), source_memory_ids (comma-sep memory IDs, optional).
      Auto-links: smart-search on title+tags+description[:80] → weighted
      _follow_memories → sourceMemoryIds (capped at 8, deduped).

    For update/complete/block/cancel:
      action_id required. complete sets status=done and attaches result.
      block sets status=blocked. cancel sets status=cancelled.
    """
    if operation == "create":
        if not title:
            return {"error": "title is required for create"}
        explicit_ids = [m.strip() for m in source_memory_ids.split(",") if m.strip()]

        auto_ids: list[str] = []
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        search_q = f"{title} {' '.join(tag_list)} {description[:80]}".strip()
        if search_q:
            try:
                obs_body: dict = {"query": search_q, "limit": 6, "format": "compact"}
                if project:
                    obs_body["project"] = project
                obs_resp, mem_resp = await asyncio.gather(
                    _call("POST", "/agentmemory/smart-search", json=obs_body),
                    _call("GET", "/agentmemory/memories", params={"limit": 80, **({"project": project} if project else {})}),
                )
                raw_obs = [r for r in obs_resp.get("results", []) if _is_useful_observation(r)]
                all_memories = mem_resp.get("memories", []) if isinstance(mem_resp, dict) else []
                linked = _follow_memories(raw_obs[:4], all_memories, limit=5)
                auto_ids = [m["id"] for m in linked if m.get("id")]
            except Exception:
                pass  # auto-link failure must never block action creation

        all_ids = list(dict.fromkeys(explicit_ids + auto_ids))[:8]  # dedupe, cap at 8

        body: dict = {
            "title": title,
            "description": description,
            "priority": max(1, min(10, priority)),
            "tags": tag_list,
            "sourceMemoryIds": all_ids,
        }
        if project:
            body["project"] = project

        edges = [
            {"type": "requires", "targetActionId": dep_id.strip()}
            for dep_id in requires.split(",")
            if dep_id.strip()
        ]
        if edges:
            body["edges"] = edges

        return await _call("POST", "/agentmemory/actions", json=body)

    if not action_id:
        return {"error": "action_id is required for update/complete/block/cancel"}

    status_map = {"complete": "done", "block": "blocked", "cancel": "cancelled", "update": ""}
    if operation not in status_map:
        return {"error": f"Unknown operation '{operation}'. Use: create|update|complete|block|cancel"}

    body = {"actionId": action_id}
    derived_status = status_map[operation]
    if derived_status:
        body["status"] = derived_status
    if result:
        body["result"] = result
    if title:
        body["title"] = title
    if description:
        body["description"] = description
    if priority != 5:
        body["priority"] = max(1, min(10, priority))

    return await _call("POST", "/agentmemory/actions/update", json=body)


# ---------------------------------------------------------------------------
# Tool 6: memory_sessions_find — semantic session recovery
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_sessions_find(
    query: str,
    project: str = "",
    limit: int = 8,
    include_timeline: bool = False,
) -> dict:
    """Recover prior sessions by semantic topic — not by UUID.

    Uses BM25+vector observation search to find sessionId anchors, then joins
    session metadata (summary, firstPrompt, startedAt, cwd) from /sessions.
    Optionally includes session crystal narratives for richer context.
    """
    search_body: dict = {"query": query, "limit": limit * 3, "format": "compact"}
    if project:
        search_body["project"] = project

    search_resp, sessions_resp = await asyncio.gather(
        _call("POST", "/agentmemory/smart-search", json=search_body),
        _call("GET", "/agentmemory/sessions", params={"limit": 100}),
    )

    matched_obs = search_resp.get("results", [])
    matched_session_ids: dict = {}
    for obs in matched_obs:
        sid = obs.get("sessionId")
        if sid and sid not in matched_session_ids:
            matched_session_ids[sid] = obs

    all_sessions = sessions_resp.get("sessions", [])
    matched_sessions = []
    for s in all_sessions:
        sid = s.get("id") or s.get("sessionId")
        if sid in matched_session_ids:
            entry = {
                "sessionId": sid,
                "summary": (s.get("summary") or "")[:400],
                "firstPrompt": (s.get("firstPrompt") or "")[:200],
                "startedAt": s.get("startedAt"),
                "endedAt": s.get("endedAt"),
                "cwd": s.get("cwd"),
                "matched_observation": _fmt_observation(matched_session_ids[sid]),
            }
            matched_sessions.append(entry)

    out: dict = {"sessions": matched_sessions[:limit]}

    raw_crystals = await _fetch_crystals(project, 20)
    matched_session_id_set = {s["sessionId"] for s in matched_sessions}
    session_crystals = [
        _fmt_crystal(c) for c in raw_crystals
        if c.get("sessionId") in matched_session_id_set
    ]
    if session_crystals:
        out["crystals"] = session_crystals

    if include_timeline and matched_sessions:
        try:
            earliest = min(
                (s["startedAt"] for s in matched_sessions if s.get("startedAt")),
                default=None,
            )
            if earliest:
                anchor = earliest[:10]
                tl_body: dict = {"anchor": anchor}
                if project:
                    tl_body["project"] = project
                tl_resp = await _call("POST", "/agentmemory/timeline", json=tl_body)
                out["timeline"] = tl_resp
        except Exception:
            pass

    return out


# ---------------------------------------------------------------------------
# Tool 7: memory_profile — project snapshot
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_profile(
    project: str = "",
    include_lessons: bool = True,
    include_insights: bool = True,
    include_frontier: bool = True,
    limit: int = 20,
) -> dict:
    """Project snapshot — call at session start or before planning.

    Returns top concepts, top files, recent activity, and session count from
    the profile endpoint, optionally enriched with lessons, insights, and the
    active action frontier. All enrichment calls run in parallel.
    """
    params: dict = {}
    if project:
        params["project"] = project
    if limit != 20:
        params["limit"] = limit

    tasks_coros = [_call("GET", "/agentmemory/profile", params=params)]
    if include_lessons:
        tasks_coros.append(_fetch_lessons(project, 10))
    if include_insights:
        tasks_coros.append(_fetch_insights(project, 8))
    if include_frontier:
        frontier_params: dict = {"limit": 5}
        if project:
            frontier_params["project"] = project
        tasks_coros.append(_call("GET", "/agentmemory/frontier", params=frontier_params))

    results = await asyncio.gather(*tasks_coros, return_exceptions=True)

    out = results[0] if not isinstance(results[0], Exception) else {}
    idx = 1
    if include_lessons:
        raw = results[idx] if not isinstance(results[idx], Exception) else []
        if raw:
            out["lessons"] = [_fmt_lesson(l) for l in raw]
        idx += 1
    if include_insights:
        raw = results[idx] if not isinstance(results[idx], Exception) else []
        if raw:
            out["insights"] = [_fmt_insight(i) for i in raw]
        idx += 1
    if include_frontier:
        raw = results[idx] if not isinstance(results[idx], Exception) else {}
        frontier_entries = raw.get("frontier", []) if isinstance(raw, dict) else []
        if frontier_entries:
            out["frontier"] = [_fmt_frontier_entry(e) for e in frontier_entries]

    return out


# ---------------------------------------------------------------------------
# Tool 8: memory_session_context — stateless context block
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_session_context(
    project: str = "",
    session_id: str = "",
    budget: int = 0,
) -> dict:
    """Get the full project context block via stateless POST /context.

    Zero DB mutation — does NOT create or modify any session. Safe to call at
    any time without side effects (unlike the old temp-session approach).

    session_id: current session ID (uses "unknown" if omitted)
    budget: optional token budget hint for the context response
    project: canonical project path or name
    """
    body: dict = {"sessionId": session_id or "unknown"}
    if project:
        body["project"] = project
    if budget:
        body["budget"] = budget
    return await _call("POST", "/agentmemory/context", json=body)


# ---------------------------------------------------------------------------
# Advanced: memory_graph_query — not in standard agent flow
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_graph_query(
    query: str,
    depth: int = 1,
    node_types: str = "",
    project: str = "",
) -> dict:
    """Query the agentmemory knowledge graph for structural relationships.

    Advanced tool — not needed in the standard orchestration flow. Use only
    when you need explicit causality chains or dependency traversal that
    memory_find and memory_task_context do not surface.

    query: concept, file, function name, or free-form description.
    depth: traversal depth (1 = direct neighbors, 2 = two hops).
    node_types: comma-sep filter e.g. "concept,function,error,file,decision".
    """
    body: dict = {"query": query, "depth": depth}
    if node_types:
        body["nodeTypes"] = [t.strip() for t in node_types.split(",") if t.strip()]
    if project:
        body["project"] = project
    resp = await _call("POST", "/agentmemory/graph/query", json=body)
    return {
        "nodes": resp.get("nodes", [])[:20],
        "edges": resp.get("edges", [])[:30],
        "depth": resp.get("depth", depth),
        "total_nodes": len(resp.get("nodes", [])),
        "total_edges": len(resp.get("edges", [])),
    }


# ---------------------------------------------------------------------------
# FastMCP prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def session_start_context(project: str = "/home/gbm1996/wksp/gionodes") -> str:
    """Template for session-start memory lookup."""
    return f"""Run memory_profile with project="{project}" (includes lessons, insights, frontier).
Then run memory_find with query="prior decisions constraints architecture patterns" project="{project}" depth="normal".
Summarize in 3-5 bullets: relevant prior decisions, active constraints, recent work areas."""


@mcp.prompt()
def pre_action_search(
    action_title: str, project: str = "/home/gbm1996/wksp/gionodes"
) -> str:
    """Template for memory context before executing a specific task action.

    If the action came from memory_next, the 'context' field is already populated
    — use it directly and skip this. Only call memory_task_context when context is absent.
    """
    return f"""Before executing: {action_title}

If the action has a 'context' field from memory_next, use it directly — no search needed.

Otherwise run memory_task_context with:
  task="{action_title}"
  project="{project}"

Focus on: prior decisions, known bugs or constraints, existing patterns to follow."""


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
