"""
SCENARIO: Context Recovery Loop
================================
An agent encounters a familiar-looking error or problem. Instead of debugging
from scratch, it uses episodic memory via smart-search + progressive disclosure
to find how the same problem was solved in a previous session.

Pattern:
  1. smart-search(query)           → find top observation hits (scored)
  2. inspect obs IDs from results
  3. smart-search(expandIds=[...]) → expand to surrounding context window
  4. The expanded context reveals the prior solution / terminal command / fix

Key endpoint: POST /agentmemory/smart-search
  body (initial): {query: str, limit: int, format: "full"|"compact", project?: str}
  body (expand):  {query: str, expandIds: [obs_id, ...], limit: int, format: "full"}

  format="full"    → includes facts[], concepts[], files[] per observation
  format="compact" → minimal fields, lower token cost
  expandIds        → KV-fetch the 5 observations before+after each ID (context window)
                     This is the "progressive disclosure" technique: start narrow,
                     expand only the one observation that looks most relevant.

Run:
    python mcp/agentmemory/scenario_context_recovery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, mcp_call, pp, print_obs_summary, step

# ── queries that mimic what an agent would search for ───────────────────────
QUERIES = [
    "pose estimation error import",
    "SAM segmentation memory issue",
    "preprocessing pipeline dependency missing",
]


def run_initial_search(query: str) -> list[dict]:
    """
    Step 1 — Initial smart-search.

    POST /agentmemory/smart-search
    body: {query, limit, format: "full", project}

    Returns the top scored observations. Each has:
      id, sessionId, type, title, facts[], concepts[], files[], score
    Also includes a bundled `lessons` list in the response root.
    """
    body = {
        "query": query,
        "limit": 10,
        "format": "full",
        "project": PROJECT,
    }
    print(f"\n  Searching: \"{query}\"")
    print(f"  POST {BASE_URL}/agentmemory/smart-search")
    pp(body, "request body")

    resp = call("POST", "/agentmemory/smart-search", body=body)
    results = resp.get("results", [])
    lessons = resp.get("lessons", [])
    mode = resp.get("mode", "?")

    print(f"\n  search mode: {mode}")
    print_obs_summary(results, "observation hits")

    if lessons:
        print(f"\n  [bundled lessons]  ({len(lessons)} returned)")
        for l in lessons[:3]:
            conf = l.get("confidence", "?")
            content = (l.get("content") or "")[:80]
            print(f"    conf={conf}  \"{content}\"")

    return results


def run_progressive_disclosure(query: str, expand_ids: list[str]) -> list[dict]:
    """
    Step 3 — Progressive disclosure via expandIds.

    POST /agentmemory/smart-search
    body: {query, expandIds: [obs_id], limit, format: "full"}

    expandIds tells the engine to KV-fetch the 5 observations immediately
    before and after each ID in storage order — revealing the full context
    window around the matched observation (the terminal command before the
    error, the fix that came after, etc.).

    WHY: The initial search returns isolated observation hits. expandIds
    returns the narrative context around them without extra token cost for
    a second search.
    """
    body = {
        "query": query,
        "expandIds": expand_ids,
        "limit": 15,
        "format": "full",
        "project": PROJECT,
    }
    print(f"\n  Expanding context for obs IDs: {expand_ids}")
    print(f"  POST {BASE_URL}/agentmemory/smart-search  (with expandIds)")
    pp(body, "request body")

    resp = call("POST", "/agentmemory/smart-search", body=body)
    results = resp.get("results", [])
    print_obs_summary(results, "expanded context window")
    return results


def show_full_observation(obs: dict) -> None:
    """Print all fields of one observation for detailed inspection."""
    print("\n  [full observation detail]")
    print(f"    id:         {obs.get('id') or obs.get('obsId')}")
    print(f"    sessionId:  {obs.get('sessionId')}")
    print(f"    type:       {obs.get('type')}")
    print(f"    score:      {obs.get('score')}")
    print(f"    title:      {obs.get('title', '')[:100]}")
    facts = obs.get("facts", [])
    print(f"    facts ({len(facts)}):")
    for f in facts[:5]:
        print(f"      • {str(f)[:120]}")
    concepts = obs.get("concepts", [])
    if concepts:
        print(f"    concepts:   {concepts[:8]}")
    files = obs.get("files", [])
    if files:
        print(f"    files:      {files[:5]}")


def run() -> None:
    banner("Context Recovery Loop")
    print("""
  Pattern: agent hits a familiar error → search episodic memory →
  progressive disclosure to recover exact prior fix.
  """)
    check_health()

    query = QUERIES[0]  # change index to try different queries

    # ── Step 1: initial broad search ─────────────────────────────────────────
    step(1, f"Initial smart-search: \"{query}\"")
    results = run_initial_search(query)

    if not results:
        print("\n  [no results] — try a different query in QUERIES list")
        return

    # ── Step 2: inspect the best hit ─────────────────────────────────────────
    step(2, "Inspect top-scored observation (most relevant hit)")
    top = results[0]
    show_full_observation(top)

    top_id = top.get("id") or top.get("obsId")
    if not top_id:
        print("\n  [no obs ID available for expansion]")
        return

    # ── Step 3: progressive disclosure — expand the context window ────────────
    step(3, "Progressive disclosure — expand context around best hit")
    print("""
  expandIds fetches the 5 observations before + after the matched ID.
  This reveals what the agent was doing around the error, and what fix followed.
  """)
    expanded = run_progressive_disclosure(query, [top_id])

    # ── Step 4: show the recovered context ───────────────────────────────────
    step(4, "Recovered context — what the agent sees after expansion")
    if not expanded:
        print("\n  [no expanded context returned]")
    else:
        print(f"\n  Expanded from {len(results)} → {len(expanded)} observations")
        print("  The agent now has surrounding context (before/after) revealing the fix:\n")
        for obs in expanded[:6]:
            obs_type = obs.get("type", "?")
            title = (obs.get("title") or "")[:70]
            obs_id = (obs.get("id") or "")[:20]
            facts = obs.get("facts", [])
            fact_preview = facts[0][:80] if facts else "(no facts)"
            print(f"    [{obs_type:12}] {obs_id}  \"{title}\"")
            print(f"                   facts[0]: {fact_preview}")

    # ── Step 5: try a second query ───────────────────────────────────────────
    step(5, "Try second query — different error context")
    results2 = run_initial_search(QUERIES[1])
    if results2:
        show_full_observation(results2[0])


if __name__ == "__main__":
    run()
