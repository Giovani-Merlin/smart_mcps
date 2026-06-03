"""
SCENARIO: Architectural Discovery Loop
========================================
An agent enters an unfamiliar codebase or needs to modify a core subsystem.
Instead of reading every file, it queries the semantic knowledge graph to find
connected entities and understand the blast radius of a change.

Pattern:
  1. graph_query(query)          → find relevant nodes (concept, file, function, etc.)
  2. pick a node_id from results
  3. memory_relations(node_id)   → traverse connected nodes (maxHops=2)
  4. interpret connections to understand coupling and blast radius

Key endpoints:
  POST /agentmemory/graph/query   → semantic graph search by concept/file/function
                                    requires GRAPH_EXTRACTION_ENABLED=true
                                    body: {query, depth?, nodeTypes?, project?}
                                    returns: {nodes, edges, depth, total_nodes, total_edges}
  MCP  memory_relations           → get neighbors of a known node
                                    mem::get-related
                                    args: {memoryId, maxHops (str), minConfidence (str)}
                                    returns: related nodes with confidence scores

  Fallback when graph is empty:
    POST /agentmemory/smart-search with a rich query → concepts field in observations
    gives a similar conceptual map of what topics relate to the query.

Node types:
  concept   — semantic concept extracted across sessions (e.g. "SAMSegmentor")
  file      — file path referenced in observations
  function  — function/class name
  error     — error type that appeared in observations
  decision  — architectural decision record
  pattern   — recurring implementation pattern

Run:
    python mcp/agentmemory/scenario_arch_discovery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, mcp_call, pp, step

# Topics to explore — change these to investigate different areas
TOPICS = [
    "preprocessing pipeline",
    "pose estimation SAM segmentation",
    "agentmemory MCP proxy",
]


def query_graph(topic: str, depth: int = 2, node_types: list[str] | None = None) -> dict:
    """
    Query the agentmemory knowledge graph.

    POST /agentmemory/graph/query
    body: {
        query:     concept, file, or description (free-form)
        depth:     traversal hops (1=direct, 2=two hops)
        nodeTypes: ["concept", "file", "function", "error", "decision", "pattern"]
        project:   project path
    }

    Returns:
        nodes: [{id, type, label, properties, ...}]
        edges: [{from, to, type, weight, ...}]

    Note: requires GRAPH_EXTRACTION_ENABLED=true in agentmemory config.
    If disabled, nodes/edges will be empty — use smart-search fallback.
    """
    body: dict = {"query": topic, "depth": depth, "project": PROJECT}
    if node_types:
        body["nodeTypes"] = node_types
    print(f"\n  POST {BASE_URL}/agentmemory/graph/query  query=\"{topic}\"")
    pp(body, "request body")
    return call("POST", "/agentmemory/graph/query", body=body)


def get_relations(node_id: str, max_hops: int = 2, min_confidence: float = 0.0) -> dict:
    """
    Traverse memory graph from a known node.

    MCP function: memory_relations → mem::get-related
    Endpoint: POST /agentmemory/mcp/call
    args: {memoryId, maxHops (str), minConfidence (str)}

    Returns linked memories and concepts within maxHops of the target node.
    A high-confidence link means both nodes appeared together across many sessions.

    WHY: graph_query finds a concept by text; memory_relations finds what it's
    tightly coupled to — the "blast radius" of changing it.
    """
    print(f"\n  mcp_call memory_relations  nodeId={node_id}  maxHops={max_hops}")
    return mcp_call("memory_relations", {
        "memoryId": node_id,
        "maxHops": str(max_hops),
        "minConfidence": str(min_confidence),
    })


def smart_search_concepts(query: str) -> list[str]:
    """
    Fallback: extract concepts from smart-search observation hits.

    When graph extraction is disabled, observations still carry a `concepts` list.
    Union of top-obs concepts gives a similar conceptual map.

    POST /agentmemory/smart-search
    body: {query, limit, format: "full", project}
    """
    print(f"\n  [fallback] smart-search for concepts: \"{query}\"")
    resp = call("POST", "/agentmemory/smart-search", body={
        "query": query,
        "limit": 8,
        "format": "full",
        "project": PROJECT,
    })
    results = resp.get("results", [])
    all_concepts: set[str] = set()
    for r in results[:5]:
        all_concepts.update(r.get("concepts", []))
    concepts = sorted(all_concepts)
    print(f"  Concepts across top obs: {concepts[:15]}")
    return concepts


def run() -> None:
    banner("Architectural Discovery Loop")
    print("""
  Pattern: need to modify a core subsystem → graph_query finds the concept
  node → memory_relations reveals what it's coupled to (blast radius).
  Falls back to smart-search concept extraction when graph is disabled.
  """)
    check_health()

    topic = TOPICS[0]

    # ── Step 1: graph query ────────────────────────────────────────────────
    step(1, f"Graph query: \"{topic}\"")
    print("""
  POST /agentmemory/graph/query  returns concept/file/function nodes.
  Requires GRAPH_EXTRACTION_ENABLED=true — may return empty on this install.
  """)
    graph_resp = query_graph(topic, depth=2)
    nodes = graph_resp.get("nodes", [])
    edges = graph_resp.get("edges", [])
    print(f"\n  nodes returned: {len(nodes)}  edges: {len(edges)}")

    if nodes:
        print("\n  [nodes]")
        for n in nodes[:8]:
            node_id = n.get("id", "?")
            ntype = n.get("type", "?")
            label = n.get("label", n.get("name", "?"))
            print(f"    [{ntype:10}] {node_id[:24]}  \"{label}\"")
        if edges:
            print("\n  [edges (sample)]")
            for e in edges[:5]:
                print(f"    {e.get('from','?')[:20]} --[{e.get('type','?')}]--> {e.get('to','?')[:20]}  w={e.get('weight','?')}")
    else:
        print("\n  [graph returned no nodes]")
        print("  This is expected if GRAPH_EXTRACTION_ENABLED is not set.")

    # ── Step 2: traverse from a node ──────────────────────────────────────
    step(2, "Traverse relations from top node (blast radius)")
    print("""
  memory_relations expands from a node_id to find everything coupled to it.
  High-confidence edges = things that change together across sessions.
  """)
    target_id = None
    if nodes:
        target_id = nodes[0].get("id")
        label = nodes[0].get("label", nodes[0].get("name", ""))
        print(f"\n  Expanding node: \"{label}\"  (id={target_id})")
        relations = get_relations(target_id, max_hops=2)
        pp(relations, "memory_relations result", truncate=800)
    else:
        print("\n  [no node to traverse — skipping memory_relations]")

    # ── Step 3: second topic with concept fallback ─────────────────────────
    step(3, f"Concept extraction fallback — \"{TOPICS[1]}\"")
    print("""
  When graph is disabled, smart-search returns observations with `concepts[]`.
  Union of concepts across top hits gives a manual dependency map.
  """)
    smart_search_concepts(TOPICS[1])

    # ── Step 4: try getting memories linked to a concept ──────────────────
    step(4, "Find memories related to the area (GET /memories + concept filter)")
    print("""
  GET /agentmemory/memories returns all curated memories.
  Filter client-side by concepts field to find memories related to the topic.
  Note: GET /memories ignores the q= query param — always fetches all.
  """)
    resp = call("GET", "/agentmemory/memories", params={"limit": 100, "project": PROJECT})
    all_memories = resp.get("memories", [])
    keywords = {w.lower() for w in topic.split()}
    related = [
        m for m in all_memories
        if any(k in " ".join(m.get("concepts", [])).lower() for k in keywords)
        or any(k in (m.get("title") or "").lower() for k in keywords)
        or any(k in (m.get("content") or "").lower() for k in keywords)
    ]
    print(f"\n  Total memories: {len(all_memories)}  related to \"{topic}\": {len(related)}")
    for m in related[:5]:
        strength = m.get("strength", "?")
        mtype = m.get("type", "?")
        title = (m.get("title") or m.get("content") or "")[:70]
        concepts = m.get("concepts", [])[:5]
        print(f"    [{mtype:14}] strength={strength}  \"{title}\"")
        print(f"                   concepts: {concepts}")

    # ── Step 5: profile — top concepts + top files ────────────────────────
    step(5, "Project profile — top concepts and file patterns")
    print("""
  GET /agentmemory/profile returns the top concepts and files extracted
  across all sessions — the semantic fingerprint of the project.
  """)
    profile = call("GET", "/agentmemory/profile", params={"project": PROJECT})
    top_concepts = profile.get("topConcepts", profile.get("concepts", []))[:10]
    top_files = profile.get("topFiles", profile.get("files", []))[:8]
    print(f"\n  [top concepts]  {top_concepts}")
    print(f"  [top files]     {top_files}")


if __name__ == "__main__":
    run()
