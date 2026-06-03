"""
SCENARIO: Related Session Discovery
=====================================
Find and summarize sessions that are semantically related to a topic —
NOT just the most recent sessions, but the ones whose content matches a query.

Pattern:
  PHASE 1 — SEARCH:    POST /search {query, format: "compact"} → obs hits with sessionIds
  PHASE 2 — METADATA:  GET /sessions → filter for matched sessionIds → summary, firstPrompt
  PHASE 3 — CRYSTALS:  GET /crystals?project=... → filter by sessionId → full narrative
                        (only populated if crystallization has been triggered)
  PHASE 4 — SUMMARY:   Assemble session-centric view (not raw obs list)

Key differences from /context (the recency-based briefer):
  - /context packs recent sessions regardless of topic
  - This pattern finds sessions by semantic RELEVANCE to a specific query
  - Use this when you want "what did we do last time we worked on X?"
    not "what happened most recently?"

Response shape quick-reference:
  /search compact result: {obsId, score, sessionId, timestamp, title, type}
  Session object:         {id, project, cwd, startedAt, endedAt?, status,
                           observationCount, summary?, firstPrompt?, tags?}
  Crystal object:         {id, narrative, keyOutcomes, filesAffected,
                           lessons, sourceActionIds, sessionId?, project?, createdAt}

Run:
    python scenarios/retrieve_related_sessions.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import PROJECT, banner, call, check_health, step


def phase_1_search(query: str) -> list[dict]:
    """
    Find observation hits semantically related to the query.
    Extract unique sessionIds — these are the sessions to investigate.

    POST /agentmemory/search
    MINIMAL: {query, project, format: "compact", limit}

    NOTE: Use /search (not /smart-search) for session discovery.
    /search returns sessionId anchors; /smart-search is for episodic recall.
    /search compact result score is BM25 raw (higher = better, scale varies).
    /smart-search score is RRF-normalized (0.0–0.1 range typically).
    """
    print(f"\n  [PHASE 1: SEARCH]  POST /search {{query='{query}', format=compact}}")
    print("  Collects sessionId anchors from obs hits — not the same as /context")

    resp = call(
        "POST",
        "/agentmemory/search",
        body={
            "query": query,
            "project": PROJECT,
            "format": "compact",
            "limit": 20,
        },
    )

    results = resp.get("results", [])
    print(f"  hits: {len(results)}  tokens_used: {resp.get('tokens_used', '?')}")

    # Collect {sessionId: [hits]} — group hits by session
    session_hits: dict[str, list[dict]] = {}
    for r in results:
        sid = r.get("sessionId")
        if sid:
            session_hits.setdefault(sid, []).append(r)

    print(f"  unique sessions: {len(session_hits)}")
    for sid, hits in list(session_hits.items())[:5]:
        best = max(hits, key=lambda h: h.get("score", 0))
        print(f"    {sid[:32]}  {len(hits)} hits  best: \"{(best.get('title') or '')[:40]}\"")

    return [{"sessionId": sid, "hits": hits} for sid, hits in session_hits.items()]


def phase_2_metadata(session_groups: list[dict]) -> list[dict]:
    """
    Fetch session metadata for the matched sessionIds.

    GET /agentmemory/sessions
    MINIMAL: {limit}

    Session object has:
      summary   — LLM-generated title (≤80 chars), set after session ends if
                  CONSOLIDATION_ENABLED=true and LLM provider configured
      firstPrompt — the first user message in the session
      status    — "active" | "completed" | "abandoned"
      cwd       — working directory
      observationCount — total obs captured
    """
    print("\n  [PHASE 2: METADATA]  GET /sessions → filter for matched sessionIds")

    session_ids = {g["sessionId"] for g in session_groups}
    resp = call("GET", "/agentmemory/sessions", params={"limit": 100})
    all_sessions = resp.get("sessions", [])

    matched: list[dict] = []
    for s in all_sessions:
        if s.get("id") in session_ids:
            matched.append(s)

    print(f"  total sessions in store: {len(all_sessions)}  matched: {len(matched)}")
    for s in matched[:5]:
        sid = (s.get("id") or "")[:32]
        summary = (s.get("summary") or "(no summary yet)")[:60]
        first = (s.get("firstPrompt") or "")[:40]
        status = s.get("status", "?")
        obs_count = s.get("observationCount", "?")
        print(f"  [{status:9}] {sid}")
        print(f"    summary:     \"{summary}\"")
        if first:
            print(f"    firstPrompt: \"{first}\"")
        print(f"    obs: {obs_count}")

    if not matched:
        print("  → No matched sessions found in store.")
        print("    This happens when sessionIds from search don't match stored sessions.")
        print("    Sessions may have been purged or stored under a different project.")

    return matched


def phase_3_crystals(session_groups: list[dict]) -> list[dict]:
    """
    Fetch crystal narratives for the matched sessions.
    Crystals are outcome summaries produced by crystallization — they contain
    the FULL STORY of what happened in a session (narrative, keyOutcomes, filesAffected).

    GET /agentmemory/crystals
    PARAMS: {project}  (sessionId filter not confirmed; filter client-side)

    Crystal object:
      narrative     — story-like description of accomplishments
      keyOutcomes   — main results as string[]
      filesAffected — file-level impact
      lessons       — direct lessons extracted (string[])
      sessionId     — links back to source session (optional)

    NOTE: Crystal store is empty if crystallization has never been triggered.
    Trigger with: POST /agentmemory/crystals/create {actionIds: [...]}
    or:           POST /agentmemory/crystals/auto (automated batch)
    """
    print("\n  [PHASE 3: CRYSTALS]  GET /crystals → session narratives")
    print("  NOTE: only populated after crystallization runs (POST /crystals/auto)")

    try:
        resp = call("GET", "/agentmemory/crystals", params={"project": PROJECT})
    except Exception as e:
        print(f"  crystals error: {e}")
        return []

    all_crystals = resp.get("crystals", [])
    print(f"  total crystals: {len(all_crystals)}")

    if not all_crystals:
        print("  (empty — trigger with: POST /agentmemory/crystals/auto)")
        print("  Crystal shape when populated:")
        print("    {id, narrative, keyOutcomes: [], filesAffected: [], lessons: [], sessionId?}")
        return []

    # Filter crystals by matched session IDs
    session_ids = {g["sessionId"] for g in session_groups}
    matched_crystals = [c for c in all_crystals if c.get("sessionId") in session_ids]
    other_crystals = [c for c in all_crystals if c not in matched_crystals]

    print(f"  crystals for matched sessions: {len(matched_crystals)}")
    print(f"  other crystals: {len(other_crystals)}")

    for crystal in (matched_crystals or all_crystals)[:3]:
        cid = (crystal.get("id") or "")[:20]
        sid = (crystal.get("sessionId") or "")[:20]
        narrative = (crystal.get("narrative") or "")[:100]
        outcomes = crystal.get("keyOutcomes", [])[:2]
        files = crystal.get("filesAffected", [])[:3]
        print(f"\n  Crystal {cid}  session={sid}")
        print(f"    narrative: \"{narrative}\"")
        if outcomes:
            print(f"    keyOutcomes: {outcomes}")
        if files:
            print(f"    filesAffected: {files}")

    return matched_crystals or all_crystals


def phase_4_summary(session_groups: list[dict], sessions: list[dict], crystals: list[dict]) -> None:
    """
    Assemble the session-centric view for the agent.

    This is the "which sessions are relevant to topic X?" answer.
    Output: for each matched session, show what happened (summary + narrative).
    """
    print("\n  [PHASE 4: SUMMARY]  Session-centric view (not raw obs list)")

    crystal_by_session = {c.get("sessionId"): c for c in crystals if c.get("sessionId")}
    session_by_id = {s.get("id"): s for s in sessions}

    for group in session_groups[:5]:
        sid = group["sessionId"]
        hits = group["hits"]
        session = session_by_id.get(sid)
        crystal = crystal_by_session.get(sid)

        print(f"\n  Session: {sid[:32]}")
        print(f"  relevance: {len(hits)} matching obs")
        if session:
            summary = session.get("summary") or session.get("firstPrompt") or "(no summary)"
            print(f"  summary: \"{summary[:70]}\"")
            print(f"  status: {session.get('status')}  obs: {session.get('observationCount')}")
        else:
            print("  (session metadata not found in store)")
        if crystal:
            print(f"  narrative: \"{(crystal.get('narrative') or '')[:80]}\"")
            if crystal.get("keyOutcomes"):
                print(f"  outcomes: {crystal['keyOutcomes'][:2]}")
        else:
            top_hit = max(hits, key=lambda h: h.get("score", 0))
            print(f"  best obs: [{top_hit.get('type')}] \"{(top_hit.get('title') or '')[:60]}\"")


def run() -> None:
    query = "agentmemory skill mcp endpoint"

    banner("Related Session Discovery — Semantic, Not Recency")
    print(
        f"""
  Finds sessions related to: "{query}"
  This is NOT the same as POST /context (recency-based).
  This is for "what did we do last time we worked on X?"

  Pattern:
    POST /search format=compact → sessionId anchors from obs hits
    GET  /sessions              → session metadata (summary, firstPrompt)
    GET  /crystals              → outcome narratives (if crystallization run)
    → assemble session-centric view
    """
    )
    check_health()

    step(1, "Search: find obs hits and extract sessionIds")
    session_groups = phase_1_search(query)

    step(2, "Metadata: GET /sessions → filter for matched sessionIds")
    matched_sessions = phase_2_metadata(session_groups)

    step(3, "Crystals: GET /crystals → outcome narratives")
    crystals = phase_3_crystals(session_groups)

    step(4, "Summary: assemble session-centric view")
    phase_4_summary(session_groups, matched_sessions, crystals)

    print(f"\n{'═' * 72}")
    print("  RELATED SESSIONS SUMMARY")
    print(f"{'─' * 72}")
    print("  1. POST /search       {query, project, format: 'compact', limit: 20}")
    print("     → {results: [{obsId, score, sessionId, title, type}], tokens_used}")
    print("     → extract unique sessionIds from results")
    print("  2. GET  /sessions     {limit: 100}")
    print("     → {sessions: [{id, project, status, summary?, firstPrompt?, ...}]}")
    print("     → filter client-side for matched sessionIds")
    print("  3. GET  /crystals     {project}")
    print("     → {crystals: [{id, narrative, keyOutcomes, filesAffected, sessionId?}]}")
    print("     → filter client-side by sessionId")
    print("     → EMPTY if POST /crystals/auto has not been run")
    print(f"{'─' * 72}")
    print("  KEY INSIGHT: /search score ≠ /smart-search score")
    print("    /search compact: BM25 raw score (19.65, higher=better)")
    print("    /smart-search:   RRF-normalized (0.016, 0-1 range)")
    print("  KEY INSIGHT: session.summary populated only if CONSOLIDATION_ENABLED=true")
    print("    and LLM provider is configured. Otherwise use firstPrompt as fallback.")
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
