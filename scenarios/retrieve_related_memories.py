"""
SCENARIO: Related Memories via Graph Traversal
================================================
Demonstrates the "one good function" pattern for retrieving memories
related to a concept, file, or keyword — using the knowledge graph
as a semantic router rather than BM25/vector search.

Why graph, not smart-search?
  - smart-search indexes CompressedObservations (episodic events)
  - Memories (POST /remember) are NOT in BM25/vector indexes
  - To find memories semantically, you must traverse the graph

Pattern (get_related_memories):
  PHASE 1 — GRAPH:    POST /graph/query {query, maxDepth: 2}
                       → GraphNode[] for related concepts/files/decisions
  PHASE 2 — FILTER:   Extract node IDs for memory-typed nodes
                       (types: decision, pattern, architecture, bug, preference)
  PHASE 3 — HYDRATE:  GET /memories {project, limit: 100}
                       → filter Memory[] by matching node IDs
  PHASE 4 — FALLBACK: If graph is empty (cold), use GET /memories + keyword filter

Key limitations (confirmed by live testing):
  - Graph returns 0 nodes if GRAPH_EXTRACTION_ENABLED=false
  - Graph is cold until at least one consolidation pass has run:
      POST /agentmemory/consolidate {tier: "episodic"}  (slow, ~30s+)
  - GraphNode IDs SHOULD match Memory IDs (same namespace), but
    this hasn't been verified on a populated graph

Memory object shape:
  {id, createdAt, updatedAt, type, title, content, concepts, files,
   sessionIds, strength, version, isLatest, parentId?, supersedes?,
   relatedIds?, sourceObservationIds?, forgetAfter?}

Run:
    python scenarios/retrieve_related_memories.py

NOTE: This scenario will show empty graph results until graph extraction
is enabled. See the FALLBACK section for the workaround.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import PROJECT, banner, call, check_health, pp, step


def get_related_memories(query: str, max_depth: int = 2) -> list[dict]:
    """
    One-function pattern for semantic memory retrieval via the knowledge graph.

    Returns Memory objects related to the query term.
    Falls back to keyword filtering of all memories if graph is cold.
    """
    # Step 1: graph traversal
    graph_resp = call(
        "POST",
        "/agentmemory/graph/query",
        body={
            "query": query,
            "project": PROJECT,
            "maxDepth": max_depth,
            # optional: nodeType="concept" to narrow to concept nodes only
        },
    )
    nodes = graph_resp.get("nodes", [])

    # Step 2: filter for memory-typed nodes
    # These node types correspond to Memory.type values in the memories store
    memory_node_types = {"decision", "pattern", "architecture", "bug", "preference", "fact", "workflow"}
    target_ids = {n.get("id") for n in nodes if n.get("type") in memory_node_types and n.get("id")}

    # Step 3: hydrate node IDs into full Memory objects
    mem_resp = call("GET", "/agentmemory/memories", params={"project": PROJECT, "limit": 100})
    all_memories = mem_resp.get("memories", [])

    if target_ids:
        related = [m for m in all_memories if m.get("id") in target_ids]
    else:
        # Fallback: graph is cold — keyword filter on content/concepts
        q_lower = query.lower()
        related = [
            m for m in all_memories
            if q_lower in (m.get("content") or "").lower()
            or any(q_lower in c.lower() for c in (m.get("concepts") or []))
            or any(q_lower in f.lower() for f in (m.get("files") or []))
        ]

    return related


def phase_1_graph(query: str) -> tuple[list[dict], list[dict]]:
    """
    Traverse the knowledge graph to find related entities.

    POST /agentmemory/graph/query
    MINIMAL: {query, project, maxDepth}
    OPTIONAL: nodeType — filter by "file"|"function"|"concept"|"decision"|...

    Returns: GraphNode[] and GraphEdge[]

    GraphNode shape: {id, type, label/name, properties?, stale?}
    GraphEdge shape: {source, target, type, weight}

    NOTE: graph is populated during consolidation (mem::graph-extract).
    Requires GRAPH_EXTRACTION_ENABLED=true env var.
    Returns 0 nodes if disabled or consolidation hasn't run.

    To enable: set GRAPH_EXTRACTION_ENABLED=true and run:
      POST /agentmemory/consolidate {"tier": "episodic"}
    """
    print(f"\n  [PHASE 1: GRAPH]  POST /graph/query {{query='{query}', maxDepth=2}}")
    print("  Looking for related concept/file/decision nodes in the knowledge graph")

    resp = call(
        "POST",
        "/agentmemory/graph/query",
        body={
            "query": query,
            "project": PROJECT,
            "maxDepth": 2,
        },
    )

    nodes = resp.get("nodes", [])
    edges = resp.get("edges", [])
    print(f"  nodes: {len(nodes)}  edges: {len(edges)}")

    if nodes:
        # Group by node type
        by_type: dict[str, list] = {}
        for n in nodes:
            by_type.setdefault(n.get("type", "?"), []).append(n)
        for ntype, ns in by_type.items():
            print(f"  [{ntype}] {len(ns)} nodes:")
            for n in ns[:3]:
                label = n.get("label") or n.get("name") or n.get("id", "?")
                print(f"    id={n.get('id','')[:24]}  label={label[:40]}")
    else:
        print("  (empty graph)")
        print("  CAUSE: GRAPH_EXTRACTION_ENABLED=false or no consolidation has run")
        print("  FIX:   export GRAPH_EXTRACTION_ENABLED=true")
        print("         then restart agentmemory and run: POST /consolidate")

    return nodes, edges


def phase_2_filter(nodes: list[dict]) -> set[str]:
    """
    Extract IDs of memory-typed graph nodes.

    Graph node types that correspond to Memory objects:
      decision    → architectural decisions, choices
      pattern     → recurring code/design patterns
      architecture → system-level structures
      bug         → known bugs, workarounds
      preference  → user/project preferences
      fact        → general facts
      workflow    → process descriptions
    """
    print("\n  [PHASE 2: FILTER]  Extract memory-typed node IDs")
    memory_node_types = {"decision", "pattern", "architecture", "bug", "preference", "fact", "workflow"}

    target_ids: set[str] = set()
    for n in nodes:
        if n.get("type") in memory_node_types and n.get("id"):
            target_ids.add(n["id"])
            ntype = n.get("type", "?")
            label = n.get("label") or n.get("name") or n["id"]
            print(f"  [{ntype}] {n['id'][:24]}  {label[:40]}")

    print(f"  target memory IDs: {len(target_ids)}")
    if not target_ids and nodes:
        non_memory = {n.get("type") for n in nodes} - memory_node_types
        print(f"  (no memory-typed nodes found — graph has types: {non_memory})")

    return target_ids


def phase_3_hydrate(target_ids: set[str], query: str) -> list[dict]:
    """
    Fetch all memories and filter by graph-identified IDs.
    Falls back to keyword search if graph is cold.

    GET /agentmemory/memories
    MINIMAL: {project, limit}

    Memory shape: {id, type, content, concepts, files, strength, isLatest, ...}
    NOTE: Memories do NOT appear in BM25/vector search — list + filter is correct.
    Use concepts[] and files[] for retrieval routing, not raw content search.
    """
    print(f"\n  [PHASE 3: HYDRATE]  GET /memories → filter by graph IDs (or keyword fallback)")

    resp = call("GET", "/agentmemory/memories", params={"project": PROJECT, "limit": 100})
    all_memories = resp.get("memories", [])
    print(f"  total memories: {len(all_memories)}")

    if target_ids:
        related = [m for m in all_memories if m.get("id") in target_ids]
        print(f"  matched by graph IDs: {len(related)}")
        if not related and all_memories:
            print("  WARNING: graph node IDs don't match any memory IDs")
            print("  This may mean graph nodes are in a different ID namespace")
            print("  Falling back to keyword filter...")
    else:
        related = []

    if not related:
        # Keyword fallback — searches content, concepts, files
        q_lower = query.lower()
        related = [
            m for m in all_memories
            if q_lower in (m.get("content") or "").lower()
            or any(q_lower in c.lower() for c in (m.get("concepts") or []))
            or any(q_lower in f.lower() for f in (m.get("files") or []))
        ]
        print(f"  keyword fallback matches: {len(related)}")

    for m in related[:4]:
        mid = (m.get("id") or "")[:24]
        mtype = m.get("type", "?")
        content = (m.get("content") or "")[:60]
        strength = m.get("strength", "?")
        concepts = (m.get("concepts") or [])[:3]
        files = (m.get("files") or [])[:2]
        print(f"\n  [{mtype:12}] id={mid}  strength={strength}")
        print(f'    content: "{content}"')
        if concepts:
            print(f"    concepts: {concepts}")
        if files:
            print(f"    files: {files}")

    if not related:
        print("  (no memories found — create some with POST /remember)")

    return related


def run() -> None:
    query = "agentmemory"

    banner("Related Memories via Knowledge Graph")
    print(
        f"""
  Query: "{query}"

  Memories are NOT in BM25/vector indexes — smart-search won't find them directly.
  The correct pattern is graph traversal → node IDs → memory hydration.

  If graph is cold (GRAPH_EXTRACTION_ENABLED=false), keyword fallback is used.
  """
    )
    check_health()

    step(1, "Graph: POST /graph/query → related concept/decision nodes")
    nodes, edges = phase_1_graph(query)

    step(2, "Filter: extract memory-typed node IDs from graph results")
    target_ids = phase_2_filter(nodes)

    step(3, "Hydrate: GET /memories → filter by node IDs (keyword fallback if graph cold)")
    related_memories = phase_3_hydrate(target_ids, query)

    print(f"\n{'═' * 72}")
    print("  RELATED MEMORIES SUMMARY")
    print(f"{'─' * 72}")
    print("  get_related_memories(query) pattern:")
    print("    1. POST /graph/query {query, project, maxDepth: 2}")
    print("       → nodes: [{id, type, label}]  edges: [{source, target, weight}]")
    print("    2. filter nodes where type in {decision, pattern, architecture, bug, ...}")
    print("       → target_ids: set of memory-related node IDs")
    print("    3. GET /memories {project, limit: 100}")
    print("       → filter memories where id in target_ids")
    print("       → FALLBACK: keyword match on content/concepts/files if graph cold")
    print(f"{'─' * 72}")
    print("  Memory object shape:")
    print("    {id, type, content, concepts: [], files: [], strength, isLatest,")
    print("     sessionIds: [], version, parentId?, supersedes?: []}")
    print(f"{'─' * 72}")
    print("  To populate the graph:")
    print("    1. Set GRAPH_EXTRACTION_ENABLED=true in agentmemory env")
    print("    2. Restart agentmemory")
    print("    3. POST /agentmemory/consolidate {tier: 'episodic'}  (slow, ~30s+)")
    print("    4. Re-run this scenario — nodes will appear")
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
