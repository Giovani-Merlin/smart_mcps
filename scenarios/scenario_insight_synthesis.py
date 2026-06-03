"""
SCENARIO: Insight Synthesis & Memory Health Loop (meta-cognition)
=================================================================
Most scenarios *use* memory. This one *maintains and grows* it — the periodic
housekeeping a long-lived agent should run so its memory stays trustworthy and
keeps producing higher-order knowledge. It exercises the meta-cognitive tier no
other scenario touches:

  1. POST /diagnostics          — health checks over the whole store: orphaned
                                  actions, dangling relations, contradictions,
                                  stale graph nodes. Each check is fixable or not.
  2. POST /diagnostics/heal     — dry-run the auto-repairs so you can see what
                                  *would* be fixed before committing.
  3. mcp memory_reflect         — the synthesis engine: traverse the knowledge
                                  graph, cluster related memories by concept, and
                                  have the LLM distill each cluster into an
                                  Insight. SLOW (~40s, real LLM work).
  4. GET  /insights             — list the synthesized insights, ranked by
                                  confidence; each cites its source concept
                                  cluster + memories + lessons + crystals.
  5. POST /insights/search      — semantic query against the insight tier — the
                                  fastest way to pull "what have we learned about
                                  X across everything" in one call.

Insights sit at the very top of the memory hierarchy: observations →
memories/lessons → crystals → insights. They are the compounded wisdom you want
injected at session start, which is exactly why memory_profile bundles them.

Run:
    python mcp/agentmemory/scenario_insight_synthesis.py

Set RUN_REFLECT = False to skip the slow synthesis step and just inspect the
existing insight tier + health.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, step

# Reflection runs the LLM over graph clusters and routinely takes 40-60s.
# The shared _client uses a 30s timeout, so reflect needs its own long-lived one.
_long = httpx.Client(base_url=BASE_URL, timeout=180.0)

INSIGHT_QUERY = "standalone preprocessing library architecture decisions"


def diagnose(categories: list[str] | None = None) -> list[dict]:
    """
    Run integrity checks across the memory store.

    POST /agentmemory/diagnostics
    body: {categories?}   → mem::diagnose

    Returns {checks: [{name, category, message, fixable, ...}]}. Categories
    cover actions, relations, graph, lessons, etc. A `fixable` check is one the
    heal pass can repair automatically; non-fixable ones need human judgement.
    """
    body = {"categories": categories} if categories else {}
    print(f"\n  POST {BASE_URL}/agentmemory/diagnostics  categories={categories or 'all'}")
    resp = call("POST", "/agentmemory/diagnostics", body=body)
    checks = resp.get("checks", [])

    # A check whose name ends in "-ok" is a category all-clear; anything else is
    # a specific finding (a flagged session, an unscoped memory, …). We collapse
    # the all-clears into one line per category and surface only the findings —
    # otherwise dozens of abandoned-session notes drown the signal.
    clears = [c for c in checks if str(c.get("name", "")).endswith("-ok")]
    findings = [c for c in checks if not str(c.get("name", "")).endswith("-ok")]
    fixable = [c for c in findings if c.get("fixable")]
    print(f"    {len(checks)} checks → {len(clears)} category all-clears, "
          f"{len(findings)} findings ({len(fixable)} auto-fixable)\n")

    print("    category all-clears:")
    for c in clears:
        print(f"      ✓ {c.get('category','?'):12} {(c.get('message') or '')[:54]}")

    if findings:
        print("\n    findings (need attention):")
        for c in findings[:12]:
            flag = "⚠ FIXABLE" if c.get("fixable") else "•  note"
            print(f"      [{flag:9}] {c.get('category','?'):10} {(c.get('message') or '')[:60]}")
        if len(findings) > 12:
            print(f"      … +{len(findings) - 12} more findings")
    return checks


def heal_dry_run() -> dict:
    """
    Preview the auto-repairs without applying them.

    POST /agentmemory/diagnostics/heal
    body: {dryRun: true}   → mem::heal

    Returns {fixed, skipped, details}. With dryRun the `fixed` count is what
    *would* be repaired. Always dry-run first — heal can delete dangling rows.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/diagnostics/heal  dryRun=true")
    resp = call("POST", "/agentmemory/diagnostics/heal", body={"dryRun": True})
    print(f"    wouldFix={resp.get('fixed')}  skipped={resp.get('skipped')}")
    for d in (resp.get("details") or [])[:8]:
        print(f"      - {str(d)[:80]}")
    if not resp.get("details"):
        print("      (nothing to repair — store is consistent)")
    return resp


def reflect(max_clusters: int = 4) -> dict:
    """
    Synthesize new insights from concept clusters in the knowledge graph.

    mcp memory_reflect → mem::reflect (POST /agentmemory/mcp/call)
    args: {project, maxClusters}   — maxClusters capped at 20

    This is the only *generative* step in the memory system reachable here: it
    walks the graph, groups memories/lessons/crystals into concept clusters, and
    asks the LLM to distill each into an Insight. Returns counts:
    {clustersProcessed, clustersSkipped, newInsights, reinforced, usedFallback}.
    `reinforced` > 0 means it re-confirmed existing insights (strengthening their
    confidence) rather than only creating new ones. SLOW — uses a 180s client.
    """
    print(f"\n  mcp memory_reflect  project=<this>  maxClusters={max_clusters}")
    print("    (LLM synthesis over graph clusters — expect ~40-60s) …")
    t0 = time.time()
    resp = _long.post("/agentmemory/mcp/call", json={
        "name": "memory_reflect",
        "arguments": {"project": PROJECT, "maxClusters": str(max_clusters)},
    })
    # Unwrap the MCP {content:[{type:text,text:<json>}]} envelope.
    data = resp.json()
    content = data.get("content", [])
    result = {}
    if content and content[0].get("type") == "text":
        try:
            result = json.loads(content[0]["text"])
        except Exception:
            result = {"raw": content[0]["text"]}
    dt = time.time() - t0
    print(f"    done in {dt:.1f}s")
    print(f"    clustersProcessed={result.get('clustersProcessed')}  "
          f"clustersSkipped={result.get('clustersSkipped')}")
    print(f"    newInsights={result.get('newInsights')}  "
          f"reinforced={result.get('reinforced')}  "
          f"usedFallback={result.get('usedFallback')}")
    return result


def list_insights(limit: int = 8, min_conf: float = 0.5) -> list[dict]:
    """
    List the synthesized insight tier, ranked by confidence.

    GET /agentmemory/insights?project=&minConfidence=&limit=

    Each Insight carries confidence, reinforcements (how many reflect passes
    re-confirmed it), and its provenance: sourceConceptCluster + sourceMemoryIds
    + sourceLessonIds + sourceCrystalIds. High reinforcements + high confidence =
    durable, trustworthy knowledge.
    """
    print(f"\n  GET {BASE_URL}/agentmemory/insights?minConfidence={min_conf}&limit={limit}")
    resp = call("GET", "/agentmemory/insights", params={
        "project": PROJECT, "minConfidence": min_conf, "limit": limit,
    })
    insights = resp.get("insights", [])
    print(f"    {len(insights)} insights (conf ≥ {min_conf}):\n")
    for ins in insights:
        conf = ins.get("confidence", "?")
        reinf = ins.get("reinforcements", 0)
        cluster = ins.get("sourceConceptCluster") or []
        title = (ins.get("title") or "")[:62]
        print(f"    conf={conf}  reinforced×{reinf}  cluster={cluster[:4]}")
        print(f"      “{title}”")
        print(f"      {(ins.get('content') or '')[:110]}")
    return insights


def search_insights(query: str, limit: int = 5) -> list[dict]:
    """
    Semantic search over the insight tier.

    POST /agentmemory/insights/search
    body: {query, project, minConfidence?, limit?}   → mem::insight-search

    One call to answer "what have we concluded about <topic> across the whole
    project's history" — pre-distilled, so far cheaper than re-deriving it from
    raw observations via smart-search. Results carry a relevance `score`.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/insights/search  query=\"{query}\"")
    resp = call("POST", "/agentmemory/insights/search", body={
        "query": query, "project": PROJECT, "limit": limit,
    })
    insights = resp.get("insights", [])
    print(f"    {len(insights)} ranked insights:\n")
    for ins in insights:
        score = ins.get("score", "?")
        conf = ins.get("confidence", "?")
        title = (ins.get("title") or ins.get("content") or "")[:64]
        print(f"    score={score}  conf={conf}  “{title}”")
    return insights


def run(run_reflect: bool = True) -> None:
    banner("Insight Synthesis & Memory Health Loop")
    print("""
  The maintenance loop a long-lived agent runs to keep memory trustworthy:
  diagnose health → preview heal → reflect (synthesize insights) → read & search
  the insight tier. Insights are the compounded wisdom injected at session start.
  """)
    check_health()

    # ── Step 1: diagnose ─────────────────────────────────────────────────────
    step(1, "Diagnose — integrity checks across the store")
    diagnose()

    # ── Step 2: heal dry-run ─────────────────────────────────────────────────
    step(2, "Heal (dry-run) — preview auto-repairs without applying")
    heal_dry_run()

    # ── Step 3: reflect ──────────────────────────────────────────────────────
    step(3, "Reflect — synthesize insights from graph concept clusters")
    print("""
  reflect is the system's only generative maintenance step. It needs the graph
  (GRAPH_EXTRACTION_ENABLED) and an LLM provider. Re-running strengthens existing
  insights (reinforced > 0) instead of duplicating them.
  """)
    if run_reflect:
        reflect(max_clusters=4)
    else:
        print("  [RUN_REFLECT=False — skipping the slow synthesis step]")

    # ── Step 4: list insights ────────────────────────────────────────────────
    step(4, "Insight tier — ranked by confidence, with provenance")
    list_insights()

    # ── Step 5: search insights ──────────────────────────────────────────────
    step(5, f"Insight search — \"{INSIGHT_QUERY}\"")
    search_insights(INSIGHT_QUERY)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  META-COGNITION SUMMARY")
    print(f"{'─' * 72}")
    print("  diagnostics       → integrity checks (fixable vs clean)")
    print("  diagnostics/heal  → dry-run preview of auto-repairs")
    print("  reflect           → LLM distills concept clusters into Insights")
    print("  insights(list)    → durable wisdom ranked by confidence×reinforcement")
    print("  insights/search   → one-call recall of conclusions about a topic")
    print(f"{'═' * 72}")
    print("  Cadence tip: run diagnose+heal often (cheap); run reflect sparingly")
    print("  (LLM cost) — e.g. at end of a sprint or after many new memories.")


if __name__ == "__main__":
    RUN_REFLECT = True
    run(run_reflect=RUN_REFLECT)
