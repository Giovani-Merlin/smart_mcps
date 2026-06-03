"""
SCENARIO: Querying All 4 Tiers with Minimal Payloads
======================================================
Demonstrates how agents query the FOUR CORE RETRIEVAL TIERS:

  1. Observations  — POST /agentmemory/smart-search  MINIMAL: {query, project, limit}
  2. Lessons       — MCP memory_lesson_recall         MINIMAL: {query, project, minConfidence}
  3. Memories      — POST /graph/query                MINIMAL: {query, project, depth}
  4. Insights      — GET /agentmemory/insights        MINIMAL: {project, minConfidence}

For each tier, agents use minimal payloads and let the engine handle scoring/filtering.

Run:
    python mcp/agentmemory/scenario_queries.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import (
    BASE_URL,
    PROJECT,
    banner,
    call,
    check_health,
    mcp_call,
    print_obs_summary,
    step,
)


def tier_1_observations(query: str) -> list[dict]:
    """
    TIER 1: Observations (episodic events).

    MINIMAL PAYLOAD: {query, project, limit}

    Returns scored observation hits ranked by BM25+vector+graph relevance.
    Also bundles lessons automatically.
    """
    print(
        f"\n  [TIER 1: Observations]  POST /agentmemory/smart-search  MINIMAL: {{query, project, limit}}"
    )
    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": query,
            "project": PROJECT,
            "limit": 8,
        },
    )

    results = resp.get("results", [])
    lessons = resp.get("lessons", [])
    print(f"  observations: {len(results)}  bundled_lessons: {len(lessons)}")
    print_obs_summary(results, "observations")

    if lessons:
        print(f"\n  [lessons bundled in response]")
        for l in lessons[:2]:
            conf = l.get("confidence", "?")
            content = (l.get("content") or "")[:70]
            print(f'    conf={conf}  "{content}"')

    return results


def tier_2_lessons(query: str) -> list[dict]:
    """
    TIER 2: Lessons (behavioral rules).

    MINIMAL PAYLOAD: {query, project, minConfidence, limit} (all strings via MCP)

    Returns lessons scored by confidence × term_overlap × recency_decay.
    """
    print(
        f"\n  [TIER 2: Lessons]  MCP memory_lesson_recall  MINIMAL: {{query, project, minConfidence, limit}}"
    )
    result = mcp_call(
        "memory_lesson_recall",
        {
            "query": query,
            "project": PROJECT,
            "minConfidence": "0.3",
            "limit": "8",
        },
    )

    lessons = result if isinstance(result, list) else result.get("lessons", [])
    print(f"  lessons: {len(lessons)}")
    for l in lessons[:3]:
        conf = l.get("confidence", "?")
        content = (l.get("content") or "")[:70]
        print(f'    conf={conf}  "{content}"')

    return lessons


def tier_3_memories(query: str) -> dict:
    """
    TIER 3: Memories (semantic facts).

    MINIMAL PAYLOAD: {query, project, depth}

    Use graph/query to find architectural neighbors and related memories.
    Memories are NOT full-text searchable — access via graph traversal.
    """
    print(
        f"\n  [TIER 3: Memories]  POST /agentmemory/graph/query  MINIMAL: {{query, project, depth}}"
    )
    resp = call(
        "POST",
        "/agentmemory/graph/query",
        body={
            "query": query,
            "project": PROJECT,
            "depth": 2,
        },
    )

    nodes = resp.get("nodes", [])
    edges = resp.get("edges", [])
    print(f"  nodes: {len(nodes)}  edges: {len(edges)}")
    for n in nodes[:3]:
        ntype = n.get("type", "?")
        label = n.get("label") or n.get("name", "?")
        print(f"    [{ntype:10}] {label[:60]}")

    return resp


def tier_4_insights() -> list[dict]:
    """
    TIER 4: Insights (synthesized patterns).

    MINIMAL PAYLOAD: {project, minConfidence, limit}

    Insights are list-based, NOT query-scored. Filter by confidence threshold.
    """
    print(
        f"\n  [TIER 4: Insights]  GET /agentmemory/insights  MINIMAL: {{project, minConfidence, limit}}"
    )
    resp = call(
        "GET",
        "/agentmemory/insights",
        params={
            "project": PROJECT,
            "minConfidence": "0.3",
            "limit": "8",
        },
    )

    insights = resp.get("insights", [])
    print(f"  insights: {len(insights)}")
    for ins in insights[:3]:
        conf = ins.get("confidence", "?")
        title = (ins.get("title") or "")[:70]
        print(f'    conf={conf}  "{title}"')

    return insights


def query_all_tiers(query: str) -> None:
    """Run all 4 tiers for a single query."""
    print(f"\n{'▓' * 72}")
    print(f'  QUERY: "{query}"')
    print(f"{'▓' * 72}")

    tier_1_observations(query)
    tier_2_lessons(query)
    tier_3_memories(query)
    tier_4_insights()


def run() -> None:
    banner("Querying All 4 Retrieval Tiers — Minimal Payloads")
    print(
        """
  Demonstrates how agents query the 4 core retrieval tiers with MINIMAL payloads:
  1. Observations  — smart-search (BM25+vector+graph)
  2. Lessons       — memory_lesson_recall MCP call (confidence-weighted)
  3. Memories      — graph/query (architectural neighbors)
  4. Insights      — list endpoint (synthesized patterns)
  """
    )
    check_health()

    step(1, "Query 1: agentmemory frontier")
    query_all_tiers("agentmemory frontier memory_next")

    step(2, "Query 2: preprocessing pipeline")
    query_all_tiers("preprocessing pipeline organization delice_gen")

    print(f"\n{'═' * 72}")
    print("  SUMMARY — 4 TIERS WITH MINIMAL PAYLOADS")
    print(f"{'─' * 72}")
    print("  Tier 1 (Observations):  {query, project, limit}")
    print(
        "  Tier 2 (Lessons):       {query, project, minConfidence, limit} (strings via MCP)"
    )
    print("  Tier 3 (Memories):      {query, project, depth} (graph/query)")
    print(
        "  Tier 4 (Insights):      {project, minConfidence, limit} (no query scoring)"
    )
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
