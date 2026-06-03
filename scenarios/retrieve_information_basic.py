"""
SCENARIO: Basic Information Retrieval (Direct Queries)
========================================================
Demonstrates the PRACTICAL query types an agent uses to bootstrap context
at session start or during task execution — ordered by typical agent workflow.

Pattern: Query → Receive → Use

Correct order (notebook-validated):
  1. profile      → stable snapshot of project conventions/files (call ONCE at session start)
  2. lessons      → behavioral rules before taking actions (POST /lessons/search)
  3. smart-search → episodic events + bundled lessons (primary scored recall)
  4. search       → session discovery by semantic content (POST /search format=compact)
  5. graph        → structural/architectural context (POST /graph/query)
  6. insights     → high-level synthesized patterns (POST /insights/search)

Key clarifications:
  - smart-search results mix observations AND memories (memories coerced to type="decision")
  - lessons/search uses term-overlap × confidence × recency (not BM25/vector)
  - graph/query uses maxDepth (not depth); only meaningful if GRAPH_EXTRACTION_ENABLED=true
  - insights have NO query relevance scorer — sorted by confidence only
  - consolidation pipeline handles automatic linking — agents skip sourceObservationIds

Run:
    python scenarios/retrieve_information_basic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, pp, step


def query_type_1_profile() -> dict:
    """
    Query Type 1: Project Profile (stable context snapshot)

    MINIMAL: {project}
    OPTIONAL: refresh=true → re-compute from scratch (use on new session after big changes)

    Call ONCE at session start. Injects stable "mental model" into the agent:
      topConcepts, topFiles, conventions, commonErrors, recentActivity

    Do NOT call repeatedly — it's a snapshot, not a search.
    """
    print("\n  [Query Type 1: Project Profile — call once at session start]")
    print("  MINIMAL: {project}")
    print("  OPTIONAL: refresh=true (re-compute topConcepts/topFiles from scratch)")

    resp = call(
        "GET",
        "/agentmemory/profile",
        params={
            "project": PROJECT,
            # refresh=true: force re-scan of all observations for this project.
            # Use when the codebase has changed significantly or first session.
            # Omit on subsequent calls to use the cached profile.
            # "refresh": "true",
        },
    )

    # Response shape: {profile: {topConcepts: [{concept, frequency}], topFiles: [{file, frequency}], ...}, reason: str|None}
    profile = resp.get("profile") or {}
    reason = resp.get("reason")
    if reason:
        print(f"  reason: {reason}  (check that PROJECT matches session project identifier)")
    top_concepts = [(c.get("concept") if isinstance(c, dict) else c) for c in (profile.get("topConcepts") or [])]
    top_files = [(f.get("file") if isinstance(f, dict) else f) for f in (profile.get("topFiles") or [])]
    print("  Profile fields (nested under 'profile' key):")
    print(f"    topConcepts: {top_concepts[:5]}")
    print(f"    topFiles:    {top_files[:3]}")
    print(f"    conventions: {(profile.get('conventions') or [])[:3]}")

    return resp


def query_type_2_lessons() -> list[dict]:
    """
    Query Type 2: Lessons (behavioral rules — what the agent has learned)

    MINIMAL: {query, project, minConfidence}

    POST /agentmemory/lessons/search  (direct REST, not MCP wrapper)
    Scoring: confidence × term_overlap_ratio × recency_decay  (NOT BM25/vector)

    Note: limit and minConfidence must be NUMBERS, not strings.
    Use before taking any "how-to" action — rules float to top when reinforced.
    """
    print("\n  [Query Type 2: Lessons — behavioral rules before taking actions]")
    print("  MINIMAL: {query, project, minConfidence}")
    print("  POST /lessons/search  (direct REST, NOT /mcp/call wrapper)")

    resp = call(
        "POST",
        "/agentmemory/lessons/search",
        body={
            "query": "agentmemory frontier memory_next",
            "project": PROJECT,
            "minConfidence": 0.3,  # number, not string — filters decayed/speculative rules
            "limit": 5,            # number, not string
        },
    )

    lessons = resp if isinstance(resp, list) else resp.get("lessons", [])
    print(f"  Results: {len(lessons)} lessons (scored: confidence × overlap × recency)")
    for l in lessons[:3]:
        print(
            f"    conf={l.get('confidence')}  score={l.get('score')}  "
            f"\"{(l.get('content') or '')[:60]}\""
        )

    return lessons


def query_type_3_observations() -> dict:
    """
    Query Type 3: Observations (episodic events — what happened in past sessions)

    MINIMAL: {query, project, limit}
    USEFUL EXTRA: includeLessons=True → bundles lessons into same response (saves a round-trip)

    POST /agentmemory/smart-search — PRIMARY SCORED ENTRY POINT
    Scoring: BM25 + vector (HNSW cosine) fused via Reciprocal Rank Fusion (RRF)

    Compact result shape (both /search and /smart-search):
      {obsId, score, sessionId, timestamp, title, type}
      NOTE: field is 'obsId' (NOT 'id') in compact results.

    IMPORTANT: results[] mixes observations AND memories.
      Memories are coerced into CompressedObservation shape with:
        - type = "decision" (normalized)
        - sessionId = "memory" (synthetic)
      Only CompressedObservations are in BM25/vector indexes.
      Memories, lessons, insights are NOT indexed — they appear via graph linkage.

    To get full content (narrative, facts): use /search format=full or expandIds.
    Full result shape: {observation: {id, narrative, facts, concepts, files, ...}, score, sessionId}

    Use for: "what happened when I last did X?" or "find past decisions about Y"
    """
    print("\n  [Query Type 3: Observations via smart-search — primary scored recall]")
    print("  MINIMAL: {query, project, limit}")
    print("  compact results: {obsId, score, sessionId, title, type}  (obsId, not id)")

    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": "agentmemory frontier memory_next",
            "project": PROJECT,
            "limit": 5,
            "includeLessons": True,  # bundle lessons into same response (avoids extra call)
        },
    )

    results = resp.get("results", [])
    lessons = resp.get("lessons", [])
    mode = resp.get("mode", "?")

    print(f"  Results: {len(results)} hits  mode={mode}  bundled lessons={len(lessons)}")
    for r in results[:3]:
        obs_id = (r.get("obsId") or r.get("id") or "")[:24]  # 'obsId' in compact results
        obs_type = r.get("type", "?")
        score = r.get("score", 0)                             # 'score', not 'combinedScore'
        session = (r.get("sessionId") or "")[:20]
        title = (r.get("title") or "")[:50]
        print(f"    [{obs_type:12}] score={score:.4f}  session={session}")
        print(f"      obsId={obs_id}  \"{title}\"")
    for l in lessons[:2]:
        print(
            f"    lesson conf={l.get('confidence')}  \"{(l.get('content') or '')[:50]}\""
        )

    return resp


def query_type_4_sessions() -> dict:
    """
    Query Type 4: Session Discovery (find related past sessions by meaning)

    MINIMAL: {query, project, format}

    POST /agentmemory/search  (NOT smart-search — this is the general search endpoint)
    format="compact" → returns observation hits with sessionId anchors (token-efficient)

    This is the correct tool for "find sessions where we worked on X" — it searches
    across all compressed observations and returns unique sessionId anchors.

    Follow-up pattern:
      1. Extract unique sessionIds from results
      2. GET /agentmemory/sessions → filter client-side for those sessionIds
      3. GET /agentmemory/crystals?project=... → filter by sessionId for narrative
    """
    print("\n  [Query Type 4: Session Discovery — POST /search format=compact]")
    print("  MINIMAL: {query, project, format}")
    print("  Follow-up: GET /sessions → GET /crystals for narrative")

    resp = call(
        "POST",
        "/agentmemory/search",
        body={
            "query": "agentmemory frontier memory_next",
            "project": PROJECT,
            "format": "compact",  # token-efficient; returns hits with sessionId anchors
            "limit": 10,
        },
    )

    results = resp.get("results", resp.get("observations", []))
    session_ids = list({r.get("sessionId") for r in results if r.get("sessionId")})

    print(f"  Results: {len(results)} observation hits")
    print(f"  Unique sessions: {len(session_ids)}")
    for sid in session_ids[:3]:
        print(f"    sessionId: {sid[:40]}")

    if session_ids:
        # Fetch session metadata for the matched sessions
        sessions_resp = call(
            "GET",
            "/agentmemory/sessions",
            params={"limit": 50},
        )
        all_sessions = sessions_resp.get("sessions", [])
        matched = [s for s in all_sessions if s.get("id") in session_ids]
        print(f"\n  Session summaries ({len(matched)} matched):")
        for s in matched[:3]:
            summary = (s.get("summary") or s.get("firstPrompt") or "")[:60]
            print(f"    {s.get('id', '')[:20]}  \"{summary}\"")

    return resp


def query_type_5_graph() -> dict:
    """
    Query Type 5: Structural Context (architectural/dependency relationships)

    MINIMAL: {query, project, maxDepth}
    NOTE: parameter is maxDepth (NOT depth) — default 3, hard cap 5

    POST /agentmemory/graph/query — neighborhood traversal over the knowledge graph
    Returns: {nodes: GraphNode[], edges: GraphEdge[]}
    nodeType filter: "file" | "function" | "concept" | "decision" | "pattern" | "bug"

    Returns GraphNode objects, NOT Memory objects. To get full memory content:
      extract node IDs → GET /memories → filter client-side

    NOTE: Only meaningful if GRAPH_EXTRACTION_ENABLED=true and consolidation has run.
    If graph is cold (no consolidation), nodes[] will be empty — skip this call.
    """
    print("\n  [Query Type 5: Graph Traversal — structural/architectural context]")
    print("  MINIMAL: {query, project, maxDepth}  (NOT 'depth')")
    print("  Returns GraphNode[], not Memory[] — needs second fetch to hydrate content")

    resp = call(
        "POST",
        "/agentmemory/graph/query",
        body={
            "query": "agentmemory frontier",
            "project": PROJECT,
            "maxDepth": 2,          # correct param (not 'depth'); default 3, hard cap 5
            # "nodeType": "concept",  # optional filter: "file"|"function"|"concept" etc.
        },
    )

    nodes = resp.get("nodes", [])
    edges = resp.get("edges", [])
    print(f"  Graph: {len(nodes)} nodes  {len(edges)} edges")
    if nodes:
        for n in nodes[:4]:
            ntype = n.get("type", "?")
            label = n.get("label") or n.get("name") or n.get("id", "?")
            print(f"    [{ntype:12}] {label[:50]}")
    else:
        print("    (empty — graph extraction may be disabled or consolidation not run)")
        print("    → set GRAPH_EXTRACTION_ENABLED=true and run /agentmemory/consolidate")

    return resp


def query_type_6_insights() -> dict:
    """
    Query Type 6: Synthesized Insights (high-level patterns, LLM-derived)

    MINIMAL: {query, project, minConfidence, limit}

    POST /agentmemory/insights/search — use this instead of GET /insights when
    you have a specific topic. Filters by keyword, sorts by confidence descending.

    NOTE: Insights have NO query-relevance scorer — they are filtered by confidence,
    not ranked by semantic similarity. This is a filtering endpoint, not a search engine.

    Typically called ONCE during high-level planning, not on every task.
    """
    print("\n  [Query Type 6: Insights — high-level synthesized patterns]")
    print("  MINIMAL: {query, project, minConfidence, limit}")
    print("  NOTE: no query-relevance score — filters by keyword, sorts by confidence")

    resp = call(
        "POST",
        "/agentmemory/insights/search",
        body={
            "query": "agentmemory frontier",
            "project": PROJECT,
            "minConfidence": 0.5,  # number — filters low-confidence/decayed insights
            "limit": 5,
        },
    )

    insights = resp.get("insights", [])
    print(f"  Results: {len(insights)} insights (sorted by confidence, no query scoring)")
    for i in insights[:3]:
        conf = i.get("confidence", "?")
        title = (i.get("title") or i.get("content") or "")[:60]
        print(f"    conf={conf}  \"{title}\"")

    return resp


def run() -> None:
    banner("Direct Query Patterns — Ordered by Agent Workflow")
    print(
        """
  This scenario shows the PRACTICAL queries an agent sends — in the correct
  order for a real workflow. Each query type has a distinct role:

    1. profile      → stable project snapshot (call once at session start)
    2. lessons      → behavioral rules BEFORE taking actions
    3. smart-search → episodic recall (obs + memories mixed, BM25+vector scored)
    4. search       → session discovery by semantic content (compact, with sessionIds)
    5. graph        → structural/architectural context (if graph extraction enabled)
    6. insights     → synthesized high-level patterns (planning only)

  Key: consolidation handles sourceObservationIds — agents don't need to provide them.
  Key: memories/lessons/insights are NOT in BM25/vector indexes (observations only).
  """
    )
    check_health()

    step(1, "Profile — stable project snapshot (call once at session start)")
    query_type_1_profile()

    step(2, "Lessons — behavioral rules before taking actions (POST /lessons/search)")
    query_type_2_lessons()

    step(3, "Observations — episodic recall (POST /smart-search, BM25+vector RRF)")
    query_type_3_observations()

    step(4, "Session Discovery — find related sessions by meaning (POST /search)")
    query_type_4_sessions()

    step(5, "Graph — structural context (POST /graph/query, maxDepth not depth)")
    query_type_5_graph()

    step(6, "Insights — synthesized patterns (POST /insights/search, confidence-filtered)")
    query_type_6_insights()

    print(f"\n{'═' * 72}")
    print("  RETRIEVAL SUMMARY — ENDPOINTS AND KEY PARAMS")
    print(f"{'─' * 72}")
    print("  1. GET  /profile              {project, refresh?}  → topConcepts, topFiles")
    print("  2. POST /lessons/search       {query, project, minConfidence: 0.3, limit: 5}")
    print("     └ scoring: confidence × term_overlap × recency_decay (NOT BM25)")
    print("  3. POST /smart-search         {query, project, limit, includeLessons: true}")
    print("     └ scoring: BM25 + vector (HNSW) via RRF → combinedScore")
    print("     └ results[] = observations + memories (memories coerced to type=decision)")
    print("  4. POST /search               {query, project, format: compact, limit}")
    print("     └ follow: GET /sessions → GET /crystals?project=... for narrative")
    print("  5. POST /graph/query          {query, project, maxDepth: 2}")
    print("     └ returns GraphNode[] — needs GET /memories to hydrate content")
    print("     └ only useful if GRAPH_EXTRACTION_ENABLED=true")
    print("  6. POST /insights/search      {query, project, minConfidence: 0.5, limit: 5}")
    print("     └ NO query relevance score — keyword filter + confidence sort only")
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
