"""
SCENARIO: Basic Information Retrieval (Direct Queries)
========================================================
Demonstrates the MINIMAL payloads for the 4 core query types an agent uses
to bootstrap context at session start or during task execution.

Pattern: Query → Receive → Use

This is what practical agents actually send (not all optional fields, just what matters).

Key insight: Consolidation pipeline handles automatic linking of sourceObservationIds
and synthesizing insights — agents don't need to provide raw IDs manually.

Run:
    python mcp/agentmemory/retrieve_information_basic.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, mcp_call, pp, step


def query_type_1_observations() -> dict:
    """
    Query Type 1: Observations (episodic events)

    MINIMAL PAYLOAD: {query, project, limit}

    This is the PRIMARY SCORED ENTRY POINT. Smart-search returns:
      - results: CompressedObservation[] (scored by BM25 + vector + graph)
      - lessons: Lesson[] (automatically bundled, scored by confidence × overlap × recency)
      - mode: which search variant fired (bm25|vector|hybrid)

    Use case: Agent needs to recall "what happened when I last did X?"
    """
    print("\n  [Query Type 1: Observations via smart-search]")
    print("  MINIMAL: {query, project, limit}")

    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": "agentmemory frontier memory_next",
            "project": PROJECT,
            "limit": 5,
        },
    )

    results = resp.get("results", [])
    lessons = resp.get("lessons", [])
    mode = resp.get("mode", "?")

    print(f"  Results: {len(results)} observations  mode={mode}")
    print(f"  Bundled lessons: {len(lessons)} rules")
    for r in results[:2]:
        print(
            f"    obs [{r.get('type')}] score={r.get('score'):.3f}  \"{(r.get('title') or '')[:50]}\""
        )
    for l in lessons[:2]:
        print(
            f"    lesson conf={l.get('confidence')}  \"{(l.get('content') or '')[:50]}\""
        )

    return resp


def query_type_2_lessons() -> list[dict]:
    """
    Query Type 2: Lessons (behavioral rules)

    MINIMAL PAYLOAD (via MCP): {query, project, minConfidence}

    Endpoint: POST /agentmemory/mcp/call with name="memory_lesson_recall"
    Lessons are scored by: confidence × term_overlap × recency_decay

    Use case: "What rule have I learned about docker builds?" → reinforced rules float to top
    """
    print("\n  [Query Type 2: Lessons via memory_lesson_recall]")
    print("  MINIMAL: {query, project, minConfidence}")

    result = mcp_call(
        "memory_lesson_recall",
        {
            "query": "agentmemory frontier memory_next",
            "project": PROJECT,
            "minConfidence": "0.3",  # 0.1-1.0, filters speculative/decayed lessons
            "limit": "5",
        },
    )

    lessons = result if isinstance(result, list) else result.get("lessons", [])
    print(f"  Results: {len(lessons)} lessons (scored by confidence × relevance)")
    for l in lessons[:3]:
        print(
            f"    conf={l.get('confidence')}  score={l.get('score')}  \"{(l.get('content') or '')[:60]}\""
        )

    return lessons


def query_type_3_memories() -> dict:
    """
    Query Type 3: Semantic Facts (curated memories)

    MINIMAL PAYLOAD: {project, limit}

    NOTE: GET /memories is a LISTING endpoint, not a search.
    Agents do NOT use fuzzy search for memories — instead:
      1. Get scored obs from smart-search
      2. List ALL memories
      3. Client-side filter by matching against obs IDs (if memories have sourceObservationIds)
      OR use POST /graph/query to find neighbors of a concept/file node

    Use case: "What have we decided about authentication architecture?"
    → Use graph/query, not smart-search
    """
    print("\n  [Query Type 3: Memories via graph traversal]")
    print("  MINIMAL: {query, project, depth}")

    # Use graph query to find related memories
    resp = call(
        "POST",
        "/agentmemory/graph/query",
        body={
            "query": "agentmemory frontier",
            "project": PROJECT,
            "depth": 2,
        },
    )

    nodes = resp.get("nodes", [])
    edges = resp.get("edges", [])
    print(f"  Graph: {len(nodes)} nodes  {len(edges)} edges")
    if nodes:
        for n in nodes[:3]:
            print(f"    [{n.get('type')}] {n.get('label', n.get('name', '?'))}")
    else:
        print("    (empty — graph extraction may be disabled)")

    return resp


def query_type_4_insights() -> dict:
    """
    Query Type 4: Synthesized Insights (high-level patterns)

    MINIMAL PAYLOAD: {project, minConfidence, limit}

    NOTE: Insights are NOT query-scored. They are list-based, filtered by confidence.
    These are LLM-synthesized conclusions from the Knowledge Graph.

    Use case: "What are the key architectural patterns in this project?"
    Typically called ONCE at session start for high-level planning.
    """
    print("\n  [Query Type 4: Insights (list-based, no query-scoring)]")
    print("  MINIMAL: {project, minConfidence, limit}")

    resp = call(
        "GET",
        "/agentmemory/insights",
        params={
            "project": PROJECT,
            "minConfidence": "0.5",  # 0-1, filters low-confidence insights
            "limit": "5",
        },
    )

    insights = resp.get("insights", [])
    print(f"  Results: {len(insights)} insights (ranked by confidence)")
    for i in insights[:3]:
        print(
            f"    conf={i.get('confidence')}  \"{(i.get('title') or i.get('content') or '')[:60]}\""
        )

    return resp


def query_type_5_profile() -> dict:
    """
    Query Type 5: Project Profile (stable context snapshot)

    MINIMAL PAYLOAD: {project}

    Call this ONCE at session start.
    Returns: topConcepts, topFiles, conventions, commonErrors, recentActivity

    Use case: "What is the mental model of this project?"
    Inject into system prompt to establish shared vocabulary.
    """
    print("\n  [Query Type 5: Project Profile (session-start snapshot)]")
    print("  MINIMAL: {project}")

    resp = call(
        "GET",
        "/agentmemory/profile",
        params={
            "project": PROJECT,
        },
    )

    print(f"  Profile populated:")
    print(f"    topConcepts: {(resp.get('topConcepts') or [])[:5]}")
    print(f"    topFiles: {(resp.get('topFiles') or [])[:5]}")
    print(f"    conventions: {(resp.get('conventions') or [])[:3]}")

    return resp


def run() -> None:
    banner("Direct Query Patterns (Minimal Payloads)")
    print(
        """
  This scenario shows what PRACTICAL agents actually send — minimal payloads,
  no sourceObservationIds, no unnecessary optional fields.

  The consolidation pipeline handles automatic linking and synthesis.
  Agents focus on retrieval, not provenance bookkeeping.
  """
    )
    check_health()

    step(1, "Query Type 1: Observations (primary scored entry point)")
    query_type_1_observations()

    step(2, "Query Type 2: Lessons (behavioral rules with confidence)")
    query_type_2_lessons()

    step(3, "Query Type 3: Memories (via graph traversal, not search)")
    query_type_3_memories()

    step(4, "Query Type 4: Insights (synthesized patterns, no query scoring)")
    query_type_4_insights()

    step(5, "Query Type 5: Profile (session-start snapshot)")
    query_type_5_profile()

    print(f"\n{'═' * 72}")
    print("  RETRIEVAL SUMMARY")
    print(f"{'─' * 72}")
    print("  1. observations      → smart-search (scored BM25+vector+graph)")
    print("  2. lessons           → memory_lesson_recall MCP call (conf-weighted)")
    print("  3. memories          → graph/query (neighbors) or GET /memories + filter")
    print("  4. insights          → insights list endpoint (conf-sorted)")
    print("  5. profile           → call once at session start")
    print(f"{'═' * 72}")
    print(
        "  Key: NO sourceObservationIds needed — let consolidation pipeline handle it"
    )
    print("       Minimal payloads — send only {query, project, limit}")
    print("       Graph traversal for architecture — not fuzzy search")


if __name__ == "__main__":
    run()
