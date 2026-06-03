#!/usr/bin/env python3
"""
AgentMemory proxy test / inspection script.

Usage:
  .venv/bin/python mcp/agentmemory_test.py              # health check + quick summary
  .venv/bin/python mcp/agentmemory_test.py --search "LTX 2.3"
  .venv/bin/python mcp/agentmemory_test.py --search "preprocessing" --min-score 5.0
  .venv/bin/python mcp/agentmemory_test.py --graph-query "SAMSegmentor"
  .venv/bin/python mcp/agentmemory_test.py --graph-query "preprocessing" --node-types "function,error"
  .venv/bin/python mcp/agentmemory_test.py --graph-stats
  .venv/bin/python mcp/agentmemory_test.py --list
  .venv/bin/python mcp/agentmemory_test.py --actions
  .venv/bin/python mcp/agentmemory_test.py --frontier
  .venv/bin/python mcp/agentmemory_test.py --sessions
  .venv/bin/python mcp/agentmemory_test.py --lessons
  .venv/bin/python mcp/agentmemory_test.py --crystals
  .venv/bin/python mcp/agentmemory_test.py --insights
  .venv/bin/python mcp/agentmemory_test.py --test-action-update
  .venv/bin/python mcp/agentmemory_test.py --test-expand-ids
  .venv/bin/python mcp/agentmemory_test.py --test-enrich

MCP browser inspector (requires node):
  fastmcp dev inspector mcp/agentmemory_mcp_proxy.py
  npx @modelcontextprotocol/inspector .venv/bin/python mcp/agentmemory_mcp_proxy.py
"""

import argparse
import asyncio
import os
import sys

import httpx

# Import scoring helpers from proxy so --search reflects what the MCP tool actually surfaces.
# Inline fallback handles running the test script from outside the mcp/ directory.
try:
    import sys as _sys
    import os as _os

    _sys.path.insert(0, _os.path.dirname(__file__))
    from agentmemory_mcp_proxy import _score_and_filter_memories, _MIN_MEMORY_SCORE
except ImportError:
    _MIN_MEMORY_SCORE = 0.0

    def _score_and_filter_memories(memories, query, limit=10):  # type: ignore[misc]
        return memories[:limit]


BASE_URL = os.environ.get("AGENTMEMORY_URL", "http://localhost:3111").rstrip("/")
PROJECT = os.environ.get("AGENTMEMORY_PROJECT", "")
TIMEOUT = 15.0


async def call(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=TIMEOUT) as client:
        resp = await client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json()


def fmt_score(score: float) -> str:
    return f"{score:8.3f}"


def sep(title: str = "") -> None:
    line = "─" * 70
    print(f"\n{line}")
    if title:
        print(f"  {title}")
        print(line)


async def check_health() -> bool:
    try:
        d = await call("GET", "/agentmemory/livez")
        status = d.get("status", "?")
        viewer = d.get("viewerPort", "?")
        print(f"✓ Daemon alive  status={status}  viewer=http://localhost:{viewer}")
        return True
    except httpx.ConnectError:
        print(f"✗ Daemon unreachable at {BASE_URL}")
        print("  Run: systemctl --user start agentmemory")
        return False
    except Exception as exc:
        print(f"✗ Health check failed: {exc}")
        return False


async def list_memories(verbose: bool = False) -> None:
    sep("CURATED MEMORIES  (GET /agentmemory/memories)")
    d = await call("GET", "/agentmemory/memories", params={"limit": 100})
    memories = d.get("memories", [])
    total = d.get("total", len(memories))
    print(f"  {len(memories)} returned / {total} total\n")

    type_counts: dict[str, int] = {}
    for m in memories:
        t = m.get("type", "?")
        type_counts[t] = type_counts.get(t, 0) + 1

    print("  By type: " + "  ".join(f"{t}={n}" for t, n in sorted(type_counts.items())))
    print()

    for m in memories:
        title = str(m.get("title", "")).replace("\n", " ").strip()[:72]
        print(f"  [{m.get('type','?'):12s}] {title}")
        if verbose:
            concepts = ", ".join(m.get("concepts", [])[:6])
            if concepts:
                print(f"               concepts: {concepts}")
            print()


def _is_useful(obs: dict) -> bool:
    """Allow-list filter: keep only observations with semantic content.

    Mirrors the proxy's _is_useful_observation logic so the test script shows
    the same signal that memory_smart_search would surface.
    """
    if obs.get("facts"):
        return True
    return obs.get("type") in {"decision", "conversation"}


async def search(query: str, min_score: float = 0.0) -> None:
    sep(f"SEARCH  query={repr(query)}  min_score={min_score}")

    # Curated memories — fetch all then apply client-side relevance filter
    # (server ignores q, returns all memories by recency)
    d = await call("GET", "/agentmemory/memories", params={"q": query, "limit": 100})
    raw_memories = d.get("memories", [])
    memories = _score_and_filter_memories(raw_memories, query, limit=20)
    print(
        f"  Curated memories: {len(memories)} relevant"
        f" (of {len(raw_memories)} total, threshold={_MIN_MEMORY_SCORE})"
    )
    for m in memories:
        title = str(m.get("title", "")).replace("\n", " ").strip()[:60]
        score = m.get("score", "")
        score_str = f" score={score:.3f}" if isinstance(score, float) else ""
        print(f"    [{m.get('type', '?'):12s}]{score_str}  {title}")

    print()

    # Observation graph via smart-search (triple-stream: BM25 + vector + graph)
    d2 = await call(
        "POST",
        "/agentmemory/smart-search",
        json={"query": query, "limit": 3},
    )
    results = d2.get("results", [])
    above = [r for r in results if r.get("score", 0) >= min_score]
    useful = [r for r in above if _is_useful(r.get("observation", r))]
    filtered_count = len(above) - len(useful)
    print(
        f"  Observations: {len(results)} total, {len(above)} above min_score={min_score}"
        f", {len(useful)} with content"
    )
    if filtered_count:
        print(f"  ({filtered_count} low-content observations filtered)")
    for r in useful[:10]:
        score = r.get("score", 0)
        obs = r.get("observation", {})
        title = str(obs.get("title", r.get("title", "?"))).replace("\n", " ")[:50]
        otype = obs.get("type", "?")
        print(f"    {fmt_score(score)}  [{otype:14s}] {title}")
        gc = r.get("graphContext")
        if gc:
            print(f"      graphContext: {str(gc)[:120]}")

    lessons = d2.get("lessons", [])
    if lessons:
        print(f"\n  Lessons: {len(lessons)}")
        for lesson in lessons[:3]:
            print(f"    - {str(lesson.get('content',''))[:70]}")


async def list_actions() -> None:
    """Show action frontier and all actions — original --actions behaviour."""
    sep("ACTIONS")

    # Frontier (unblocked)
    params: dict = {"limit": 20}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/frontier", params=params)
    items = d.get("actions", d.get("frontier", []))
    print(f"  Frontier (ready): {len(items)}")
    for a in items:
        action = a.get("action", a)
        score = a.get("score", "")
        score_str = f"  score={score:.2f}" if isinstance(score, float) else ""
        print(
            f"    [{action.get('status','?'):10s}] p={action.get('priority',5)}"
            f"{score_str}  {action.get('title','?')[:55]}"
        )

    # All actions
    d2 = await call("GET", "/agentmemory/actions", params={"limit": 50})
    all_actions = d2.get("actions", [])
    print(f"\n  All actions: {len(all_actions)}")
    by_status: dict[str, list] = {}
    for a in all_actions:
        s = a.get("status", "?")
        by_status.setdefault(s, []).append(a)
    for status, acts in sorted(by_status.items()):
        print(f"    {status:10s}: {len(acts)}")


async def list_frontier() -> None:
    """Show unblocked highest-priority actions from the frontier endpoint."""
    sep("FRONTIER  (GET /agentmemory/frontier)")
    params: dict = {"limit": 10}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/frontier", params=params)
    items = d.get("actions", d.get("frontier", []))
    if not items:
        print("  No frontier actions.")
        return
    for a in items:
        action = a.get("action", a)
        score = a.get("score", "")
        score_str = f"{score:.3f}" if isinstance(score, float) else str(score)
        desc = str(action.get("description", "")).replace("\n", " ")[:200]
        tags = ", ".join(action.get("tags", []))
        aid = action.get("id", "?")
        title = action.get("title", "?")
        status = action.get("status", "?")
        priority = action.get("priority", "?")
        print(f"  id={aid}")
        print(f"    title:    {title}")
        print(f"    status:   {status}  priority={priority}  score={score_str}")
        if desc:
            print(f"    desc:     {desc}")
        if tags:
            print(f"    tags:     {tags}")
        source_ids = action.get("sourceMemoryIds", [])
        if source_ids:
            print(f"    sourceMemoryIds: {source_ids}")
        print()


async def list_sessions() -> None:
    sep("SESSIONS  (GET /agentmemory/sessions)")
    d = await call("GET", "/agentmemory/sessions", params={"limit": 10})
    sessions = d.get("sessions", [])
    print(f"  {len(sessions)} most recent\n")
    for s in sessions:
        started = s.get("startedAt", "?")[:16]
        ended = s.get("endedAt", "?")[:16] if s.get("endedAt") else "running "
        obs = s.get("observationCount", "?")
        project = s.get("project", "?").split("/")[-1]
        print(f"  {started} → {ended}  obs={obs:>4}  {project}")


async def list_lessons() -> None:
    """Show synthesised lessons from the background consolidation pass (iii)."""
    sep("LESSONS  (GET /agentmemory/lessons)")
    params: dict = {"limit": 20, "minConfidence": 0.1}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/lessons", params=params)
    lessons = d.get("lessons", d if isinstance(d, list) else [])
    if not lessons:
        print("  No lessons synthesized yet (iii background consolidation not run).")
        return
    for lesson in lessons:
        lid = lesson.get("id", "?")
        content = lesson.get("content", "")
        context = lesson.get("context", "")
        confidence = lesson.get("confidence", "?")
        tags = ", ".join(lesson.get("tags", []))
        print(f"  id={lid}  confidence={confidence}")
        print(f"    content:  {content}")
        if context:
            print(f"    context:  {context}")
        if tags:
            print(f"    tags:     {tags}")
        print()


async def list_crystals() -> None:
    """Show crystallised memory summaries — high-level narrative distillations."""
    sep("CRYSTALS  (GET /agentmemory/crystals)")
    params: dict = {"limit": 10}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/crystals", params=params)
    crystals = d.get("crystals", d if isinstance(d, list) else [])
    if not crystals:
        print("  No crystals yet.")
        return
    for crystal in crystals:
        cid = crystal.get("id", "?")
        narrative = crystal.get("narrative", "")
        key_outcomes = crystal.get("keyOutcomes", [])
        c_lessons = crystal.get("lessons", [])
        files = ", ".join(crystal.get("filesAffected", []))
        print(f"  id={cid}")
        print(f"    narrative:   {narrative}")
        if key_outcomes:
            print("    keyOutcomes:")
            for ko in key_outcomes:
                print(f"      - {ko}")
        if c_lessons:
            print("    lessons:")
            for cl in c_lessons:
                print(f"      - {cl}")
        if files:
            print(f"    files:       {files}")
        print()


async def list_insights() -> None:
    """Show synthesised insights — higher-level patterns across lessons."""
    sep("INSIGHTS  (GET /agentmemory/insights)")
    params: dict = {"limit": 10, "minConfidence": 0.1}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/insights", params=params)
    insights = d.get("insights", d if isinstance(d, list) else [])
    if not insights:
        print("  No insights yet.")
        return
    for insight in insights:
        iid = insight.get("id", "?")
        title = insight.get("title", "")
        content = insight.get("content", "")
        confidence = insight.get("confidence", "?")
        tags = ", ".join(insight.get("tags", []))
        print(f"  id={iid}  confidence={confidence}")
        if title:
            print(f"    title:    {title}")
        print(f"    content:  {content}")
        if tags:
            print(f"    tags:     {tags}")
        print()


async def graph_stats() -> None:
    """Show knowledge graph summary — node/edge counts by type."""
    sep("GRAPH STATS  (GET /agentmemory/graph/stats)")
    try:
        d = await call("GET", "/agentmemory/graph/stats")
    except httpx.HTTPStatusError as exc:
        print(f"  HTTP {exc.response.status_code} — graph stats unavailable")
        return
    nodes_by_type = d.get("nodesByType", {})
    edges_by_type = d.get("edgesByType", {})
    total_nodes = d.get("totalNodes", sum(nodes_by_type.values()))
    total_edges = d.get("totalEdges", sum(edges_by_type.values()))
    print(f"  Total nodes: {total_nodes}  Total edges: {total_edges}\n")
    print("  Nodes by type:")
    for t, n in sorted(nodes_by_type.items(), key=lambda x: -x[1]):
        print(f"    {t:20s}: {n}")
    print("\n  Edges by type:")
    for t, n in sorted(edges_by_type.items(), key=lambda x: -x[1]):
        print(f"    {t:20s}: {n}")


async def graph_query(query: str, node_types: str = "", depth: int = 1) -> None:
    """Query the knowledge graph for nodes and edges related to query."""
    sep(f"GRAPH QUERY  query={repr(query)}  depth={depth}")
    body: dict = {"query": query, "depth": depth}
    if node_types:
        body["nodeTypes"] = [t.strip() for t in node_types.split(",") if t.strip()]
    if PROJECT:
        body["project"] = PROJECT
    try:
        d = await call("POST", "/agentmemory/graph/query", json=body)
    except httpx.HTTPStatusError as exc:
        print(f"  HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        return
    nodes = d.get("nodes", [])
    edges = d.get("edges", [])
    print(f"  {len(nodes)} nodes, {len(edges)} edges\n")
    print("  Nodes:")
    for n in nodes[:20]:
        ntype = n.get("type", "?")
        name = str(n.get("name", "?"))[:55]
        nid = n.get("id", "?")
        print(f"    [{ntype:12s}] {name}  ({nid})")
    if len(nodes) > 20:
        print(f"    ... {len(nodes) - 20} more nodes")
    if edges:
        print("\n  Edges (sample):")
        for e in edges[:10]:
            etype = e.get("type", "?")
            src = e.get("sourceNodeId", "?")[:25]
            tgt = e.get("targetNodeId", "?")[:25]
            print(f"    {etype:15s}  {src} → {tgt}")
        if len(edges) > 10:
            print(f"    ... {len(edges) - 10} more edges")


async def test_action_update() -> None:
    """Verify the correct action update route: POST /agentmemory/actions/update."""
    sep("TEST ACTION UPDATE  (POST /agentmemory/actions/update)")
    params: dict = {"limit": 10}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/frontier", params=params)
    items = d.get("actions", d.get("frontier", []))
    if not items:
        print("  No frontier actions to test with.")
        return

    first = items[0]
    action = first.get("action", first)
    action_id = action.get("id", "?")
    title = action.get("title", "?")
    print(f"  Using first frontier action: id={action_id}  title={title}")
    print(f"  Calling POST /agentmemory/actions/update with status='active' ...")

    try:
        result = await call(
            "POST",
            "/agentmemory/actions/update",
            json={"actionId": action_id, "status": "active"},
        )
        print(f"  Result: {result}")
    except httpx.HTTPStatusError as exc:
        print(f"  HTTP error {exc.response.status_code}: {exc.response.text[:300]}")
    except Exception as exc:
        print(f"  Error: {exc}")


async def test_expand_ids() -> None:
    """Exercise the expand_ids flow: find an action with sourceMemoryIds then
    call smart-search with those IDs so the server expands them with full graph
    context — the same path the MCP proxy takes when memory_smart_search receives
    expand_ids from a tool call.
    """
    sep("TEST EXPAND IDS  (POST /agentmemory/smart-search with expandIds)")
    params: dict = {"limit": 3}
    if PROJECT:
        params["project"] = PROJECT
    d = await call("GET", "/agentmemory/frontier", params=params)
    items = d.get("actions", d.get("frontier", []))

    target_ids: list[str] = []
    for a in items:
        action = a.get("action", a)
        ids = action.get("sourceMemoryIds", [])
        if ids:
            target_ids = ids
            print(
                f"  Found action '{action.get('title','?')}' with sourceMemoryIds={ids}"
            )
            break

    if not target_ids:
        print(
            "  No actions with sourceMemoryIds found — try creating one with"
            " auto_link=True first"
        )
        return

    d2 = await call(
        "POST",
        "/agentmemory/smart-search",
        json={"expandIds": target_ids, "limit": 5},
    )
    results = d2.get("results", [])
    print(f"  smart-search returned {len(results)} result(s)\n")
    for r in results:
        score = r.get("score", 0)
        obs = r.get("observation", {})
        title = str(obs.get("title", r.get("title", "?"))).replace("\n", " ")[:55]
        otype = obs.get("type", "?")
        print(f"    {fmt_score(score)}  [{otype:14s}] {title}")
        gc = r.get("graphContext")
        if gc:
            print(f"      graphContext: {str(gc)[:120]}")

    lessons = d2.get("lessons", [])
    if lessons:
        print(f"\n  Lessons: {len(lessons)}")
        for lesson in lessons[:3]:
            print(f"    - {str(lesson.get('content',''))[:70]}")


async def test_enrich() -> None:
    """Call POST /agentmemory/enrich on the most recent session to verify the
    server can extract enriched context, bug candidates, and bridging memories
    from a known file — useful for confirming the enrich endpoint is live.
    """
    sep("TEST ENRICH  (POST /agentmemory/enrich)")
    d = await call("GET", "/agentmemory/sessions", params={"limit": 1})
    sessions = d.get("sessions", [])
    if not sessions:
        print("  No sessions found")
        return

    session_id = sessions[0].get("id", "?")
    print(f"  Using session id={session_id}\n")

    try:
        result = await call(
            "POST",
            "/agentmemory/enrich",
            json={
                "sessionId": session_id,
                "files": ["/home/gbm1996/wksp/gionodes/mcp/agentmemory_mcp_proxy.py"],
                "project": "/home/gbm1996/wksp/gionodes",
            },
        )
    except httpx.HTTPStatusError as exc:
        print(f"  HTTP {exc.response.status_code}: {exc.response.text[:300]}")
        return

    enriched = result.get("enrichedContext", "")
    bug_candidates = result.get("bugCandidates", [])
    bridging = result.get("bridgingMemories", [])
    print(f"  enrichedContext (first 200 chars): {str(enriched)[:200]}")
    print(f"  bugCandidates count: {len(bug_candidates)}")
    print(f"  bridgingMemories count: {len(bridging)}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="AgentMemory proxy test script")
    parser.add_argument(
        "--search", metavar="QUERY", help="Search memories and observations"
    )
    parser.add_argument(
        "--min-score", type=float, default=0.0, help="Min score for observations"
    )
    parser.add_argument("--list", action="store_true", help="List all curated memories")
    parser.add_argument(
        "--actions", action="store_true", help="Show action frontier and all actions"
    )
    parser.add_argument(
        "--frontier",
        action="store_true",
        help="Show frontier (unblocked, highest-priority actions)",
    )
    parser.add_argument("--sessions", action="store_true", help="List recent sessions")
    parser.add_argument(
        "--lessons",
        action="store_true",
        help="Show synthesised lessons (iii background consolidation)",
    )
    parser.add_argument(
        "--crystals",
        action="store_true",
        help="Show crystallised memory summaries",
    )
    parser.add_argument(
        "--insights",
        action="store_true",
        help="Show synthesised insights",
    )
    parser.add_argument(
        "--test-action-update",
        action="store_true",
        help="Test POST /agentmemory/actions/update with first frontier action",
    )
    parser.add_argument(
        "--test-expand-ids",
        action="store_true",
        help="Test smart-search with expandIds from first frontier action's sourceMemoryIds",
    )
    parser.add_argument(
        "--test-enrich",
        action="store_true",
        help="Test POST /agentmemory/enrich on most recent session",
    )
    parser.add_argument(
        "--graph-query",
        metavar="QUERY",
        help="Query the knowledge graph for related nodes and edges",
    )
    parser.add_argument(
        "--node-types",
        metavar="TYPES",
        default="",
        help="Comma-separated node type filter for --graph-query (e.g. function,error)",
    )
    parser.add_argument(
        "--graph-stats",
        action="store_true",
        help="Show knowledge graph node/edge counts by type",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Extra detail")
    args = parser.parse_args()

    sep("AGENTMEMORY HEALTH")
    alive = await check_health()
    if not alive:
        sys.exit(1)

    if args.search:
        await search(args.search, args.min_score)
    elif args.list:
        await list_memories(verbose=args.verbose)
    elif args.actions:
        await list_actions()
    elif args.frontier:
        await list_frontier()
    elif args.sessions:
        await list_sessions()
    elif args.lessons:
        await list_lessons()
    elif args.crystals:
        await list_crystals()
    elif args.insights:
        await list_insights()
    elif args.test_action_update:
        await test_action_update()
    elif args.test_expand_ids:
        await test_expand_ids()
    elif args.test_enrich:
        await test_enrich()
    elif args.graph_query:
        await graph_query(args.graph_query, args.node_types)
    elif args.graph_stats:
        await graph_stats()
    else:
        # Default: health + quick summary
        d = await call("GET", "/agentmemory/memories", params={"limit": 1})
        total = d.get("total", "?")
        print(f"  Curated memories: {total}")

        d2 = await call("GET", "/agentmemory/sessions", params={"limit": 1})
        sess = d2.get("sessions", [])
        print(
            f"  Latest session:   {sess[0].get('startedAt','?')[:16] if sess else 'none'}"
        )

        d3 = await call("GET", "/agentmemory/frontier", params={"limit": 1})
        frontier = d3.get("actions", d3.get("frontier", []))
        print(f"  Frontier actions: {len(frontier)} ready")

        print(
            "\n  Quick commands:"
            "\n    .venv/bin/python mcp/agentmemory_test.py --list"
            "\n    .venv/bin/python mcp/agentmemory_test.py --search 'LTX 2.3'"
            "\n    .venv/bin/python mcp/agentmemory_test.py --actions"
            "\n    .venv/bin/python mcp/agentmemory_test.py --frontier"
            "\n    .venv/bin/python mcp/agentmemory_test.py --sessions"
            "\n    .venv/bin/python mcp/agentmemory_test.py --lessons"
            "\n    .venv/bin/python mcp/agentmemory_test.py --crystals"
            "\n    .venv/bin/python mcp/agentmemory_test.py --insights"
            "\n    .venv/bin/python mcp/agentmemory_test.py --test-action-update"
            "\n    .venv/bin/python mcp/agentmemory_test.py --test-expand-ids"
            "\n    .venv/bin/python mcp/agentmemory_test.py --test-enrich"
            "\n    .venv/bin/python mcp/agentmemory_test.py --graph-stats"
            "\n    .venv/bin/python mcp/agentmemory_test.py --graph-query 'SAMSegmentor'"
            "\n    fastmcp dev mcp/agentmemory_mcp_proxy.py   # browser inspector"
        )


if __name__ == "__main__":
    asyncio.run(main())
