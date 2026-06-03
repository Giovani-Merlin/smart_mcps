"""
SCENARIO: Progressive Disclosure — Provenance Traversal (Facts → Raw Events)
=============================================================================
Builds a complete provenance chain from scratch, then walks it backward:

  PHASE 1 — SEED:     Create 3 mock observations (POST /observe), save a memory
                       with explicit sourceObservationIds linking them together.
  PHASE 2 — INVERSE:  List memories → find our fact → extract sourceObservationIds.
                       This is the backward pointer from distilled fact to raw event.
  PHASE 3 — EXPAND:   POST /smart-search with expandIds → KV direct lookup (no
                       scoring overhead, no BM25/vector round-trips).
  PHASE 4 — TIMELINE: GET /observations?sessionId → all obs for session, sorted by
                       timestamp — reconstruct the chronological reading order.
  PHASE 5 — SYNTHESIS: Assemble: distilled fact → source obs → full session timeline.
  BONUS   — DELETED:   Show tombstoned lessons via GET /export (deleted: true).

NotebookLM Findings (queried before implementing this scenario):
  - POST /observe triggers mem::observe: SHA-256 dedup within time window, synthetic
    compression, indexes into BM25+vector stores. Response: 201 with raw iii result.
  - expandIds skips query-based search entirely — direct KV lookups, records access
    for retention scoring. Up to 20 IDs per call.
  - GET /observations?sessionId lists ALL CompressedObservation objects for a session.
    Sort client-side by timestamp to get chronological reading order.
  - Deleted lessons use tombstone pattern: deleted: true, ignored by recall/context.
    GET /agentmemory/export includes ALL records — filter for deleted: true.

Run:
    python mcp/agentmemory/retrieve_information_progressive_disclosure.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _client import PROJECT, banner, call, check_health, step  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# Phase 1 — SEED: create obs + memory
# ══════════════════════════════════════════════════════════════════════════════


def phase_1_seed_data(uid: str) -> tuple[str, list[str], str]:
    """
    Create 3 mock observations in one session and link them to a durable memory.

    POST /agentmemory/observe
    MINIMAL: {hookType, sessionId, project, cwd, timestamp, data}

    POST /agentmemory/remember
    WITH sourceObservationIds: {content, type, project, concepts, files, sourceObservationIds}

    Returns (session_id, obs_ids, mem_id).
    """
    print(
        "\n  [PHASE 1: SEED]  POST /observe x3 → POST /remember with sourceObservationIds"
    )
    session_id = f"mock-pd-{uid}"
    base_ts = datetime.now(timezone.utc) - timedelta(minutes=5)

    # Three distinct mock tool events — spaced 30s apart to avoid SHA-256 dedup
    events = [
        {
            "tool_name": "Read",
            "tool_input": "src/auth/jwt.py",
            "tool_output": "class JWTValidator:\n    def validate(self, token: str) -> dict:",
        },
        {
            "tool_name": "Bash",
            "tool_input": "grep -r 'Bearer' src/auth/",
            "tool_output": "src/auth/middleware.py:18: if not header.startswith('Bearer '):",
        },
        {
            "tool_name": "Edit",
            "tool_input": "src/auth/middleware.py",
            "tool_output": "Stripped leading/trailing whitespace from Bearer token before decode.",
        },
    ]

    obs_ids: list[str] = []
    for i, event_data in enumerate(events):
        ts = (base_ts + timedelta(seconds=i * 30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resp = call(
                "POST",
                "/agentmemory/observe",
                body={
                    "hookType": "post_tool_use",
                    "sessionId": session_id,
                    "project": PROJECT,
                    "cwd": "/workspaces/gionodes",
                    "timestamp": ts,
                    "data": event_data,
                },
            )
        except httpx.HTTPStatusError as e:
            print(f"  [obs {i+1}] failed: HTTP {e.response.status_code}")
            continue

        # Response is 201 with raw iii function result — extract obs ID defensively
        obs_id = (
            resp.get("obsId")
            or (resp.get("observation") or {}).get("id")
            or resp.get("id")
        )
        if obs_id:
            obs_ids.append(obs_id)
            print(f"  obs {i+1}: id={obs_id[:24]}  tool={event_data['tool_name']}")
        else:
            print(f"  obs {i+1}: created (no id in response — dedup may have fired)")
            print(f"    resp keys: {list(resp.keys())[:6]}")

    if not obs_ids:
        print(
            "  [no obs IDs captured — observations may exist but response format differs]"
        )

    # Save durable memory with explicit sourceObservationIds
    print(
        f"\n  [save memory]  POST /agentmemory/remember  sourceObservationIds={obs_ids[:3]}"
    )
    mem_resp = call(
        "POST",
        "/agentmemory/remember",
        body={
            "content": f"[pd-scenario-{uid}] JWT middleware must strip whitespace from Bearer "
            "token before decoding. Validation fails silently on padded tokens.",
            "type": "bug",
            "project": PROJECT,
            "concepts": ["jwt", "bearer-token", "auth-middleware", "whitespace"],
            "files": ["src/auth/jwt.py", "src/auth/middleware.py"],
            "sourceObservationIds": obs_ids,
        },
    )
    memory = mem_resp.get("memory", mem_resp)
    mem_id = memory.get("id")
    stored_source_ids = memory.get("sourceObservationIds", [])
    print(f"  memory saved: id={mem_id}  sourceObservationIds={stored_source_ids}")

    return session_id, obs_ids, mem_id or ""


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — INVERSE: list memories → extract sourceObservationIds
# ══════════════════════════════════════════════════════════════════════════════


def phase_2_inverse_engineering(mem_id: str, obs_ids: list[str]) -> list[str]:
    """
    Walk backward: list all memories, find our fact, extract sourceObservationIds.

    GET /agentmemory/memories
    MINIMAL: {limit, project}

    This is the standard "inverse engineering" pattern:
    - Agent has a context string mentioning a memory ID
    - It lists memories and finds which observations seeded that fact
    - Those obs IDs become the pointers for PHASE 3 (expandIds)
    """
    print("\n  [PHASE 2: INVERSE]  GET /memories → extract sourceObservationIds")

    resp = call(
        "GET",
        "/agentmemory/memories",
        params={"limit": 100, "project": PROJECT},
    )
    all_memories = resp.get("memories", [])
    print(f"  total memories in store: {len(all_memories)}")

    # Find our planted memory
    target = None
    if mem_id:
        target = next((m for m in all_memories if m.get("id") == mem_id), None)

    if target:
        source_obs = target.get("sourceObservationIds") or []
        print(f"  found memory id={mem_id[:24]}")
        print(f"  type={target.get('type')}  concepts={target.get('concepts', [])[:4]}")
        print(f"  sourceObservationIds ({len(source_obs)}): {source_obs[:3]}")
        return source_obs
    else:
        # Fallback: search for any memory with sourceObservationIds
        linked = [m for m in all_memories if m.get("sourceObservationIds")]
        print(
            f"  target not found — scanning {len(linked)} memories with sourceObservationIds"
        )
        for m in linked[:2]:
            print(
                f"    mem id={m.get('id')[:20]}  sources={m.get('sourceObservationIds', [])[:2]}"
            )
        if linked:
            return linked[0].get("sourceObservationIds", [])
        print("  [no memories with sourceObservationIds found — using seeded obs_ids]")
        return obs_ids


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — EXPAND: smart-search with expandIds
# ══════════════════════════════════════════════════════════════════════════════


def phase_3_expand_observations(source_obs_ids: list[str]) -> list[dict]:
    """
    Use expandIds to KV-fetch the full observation objects — no scoring overhead.

    POST /agentmemory/smart-search
    WITH expandIds: {expandIds: [obs_id_1, obs_id_2], limit: 10}

    When expandIds is provided, query-based search is SKIPPED ENTIRELY.
    The system does direct KV lookups, records access for retention scoring,
    and returns full CompressedObservation objects (narrative, facts, concepts).

    This is the correct pattern after finding sourceObservationIds:
    - 1 call instead of re-running BM25+vector
    - Returns full structured data, not just hits
    """
    if not source_obs_ids:
        print("\n  [PHASE 3: EXPAND]  no source obs IDs — trying smart-search by query")
        resp = call(
            "POST",
            "/agentmemory/smart-search",
            body={
                "query": "jwt bearer whitespace auth middleware",
                "project": PROJECT,
                "limit": 5,
            },
        )
        return resp.get("results", [])

    print(
        f"\n  [PHASE 3: EXPAND]  POST /smart-search with expandIds={source_obs_ids[:3]}"
    )
    print("  (expandIds mode: skips BM25/vector entirely — direct KV lookup)")

    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "expandIds": source_obs_ids[:20],  # max 20 per call
            "limit": 20,
        },
    )

    expanded = resp.get("results", [])
    print(
        f"  expanded {len(source_obs_ids)} IDs → {len(expanded)} full observation objects"
    )
    for obs in expanded[:4]:
        obsid = (obs.get("obsId") or obs.get("id") or "")[:20]
        obs_type = obs.get("type", "?")
        title = (obs.get("title") or "")[:60]
        facts = obs.get("facts", [])
        session = (obs.get("sessionId") or "")[:16]
        print(f"    [{obs_type:12}] id={obsid}  session={session}")
        print(f'      title: "{title}"')
        if facts:
            print(f"      facts: {facts[:2]}")

    return expanded


# ══════════════════════════════════════════════════════════════════════════════
# Phase 4 — TIMELINE: reconstruct session timeline
# ══════════════════════════════════════════════════════════════════════════════


def phase_4_timeline_reconstruction(
    session_id: str, expanded_obs: list[dict]
) -> list[dict]:
    """
    Reconstruct the full session timeline by fetching all obs for the session.

    GET /agentmemory/observations?sessionId=...
    MINIMAL: {sessionId}

    Returns ALL CompressedObservation objects for the session. Sort by timestamp
    to get the chronological reading order. This reveals what the agent was doing
    before and after the key events, not just the events themselves.

    Source obs (from phase 3) become bookmarks in the larger timeline.
    """
    # Determine which session to query
    query_session = session_id
    if not query_session and expanded_obs:
        query_session = expanded_obs[0].get("sessionId", "")

    print(
        f"\n  [PHASE 4: TIMELINE]  GET /observations?sessionId={query_session[:20]}..."
    )

    if not query_session:
        print("  [no sessionId available — skipping timeline reconstruction]")
        return []

    try:
        resp = call(
            "GET",
            "/agentmemory/observations",
            params={"sessionId": query_session},
        )
    except httpx.HTTPStatusError:
        print("  [timeline fetch failed — session may not exist yet]")
        return []

    all_obs = resp.get("observations", [])
    print(f"  total obs in session: {len(all_obs)}")

    # Sort by timestamp — this is the chronological reading order
    sorted_obs = sorted(
        all_obs,
        key=lambda o: o.get("timestamp") or "",
    )

    # Mark which obs were our "source" observations
    source_ids = {o.get("obsId") or o.get("id") for o in expanded_obs}

    print(f"\n  Chronological timeline (▶ = source observation):")
    for obs in sorted_obs[:8]:
        obsid = (obs.get("obsId") or obs.get("id") or "")[:16]
        ts = (obs.get("timestamp") or "")[:19]
        obs_type = obs.get("type", "?")
        title = (obs.get("title") or "")[:50]
        marker = (
            " ◀ SOURCE" if (obs.get("obsId") or obs.get("id")) in source_ids else ""
        )
        print(f"    {ts}  [{obs_type:12}] {title}{marker}")

    return sorted_obs


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — SYNTHESIS: assemble the full picture
# ══════════════════════════════════════════════════════════════════════════════


def phase_5_synthesis(
    mem_id: str,
    source_obs_ids: list[str],
    expanded_obs: list[dict],
    timeline: list[dict],
) -> None:
    """
    Print the assembled picture: distilled fact → source observations → full timeline.

    At this point the agent has walked the full provenance chain:
    1. Memory (distilled fact): "JWT middleware must strip whitespace from Bearer..."
    2. Source observations: the raw events that produced that fact
    3. Full expanded content: narrative, facts[], concepts[] from those events
    4. Timeline: chronological context of the whole session
    """
    print("\n  [PHASE 5: SYNTHESIS]  Assembled provenance picture:")
    print("  ┌─ DISTILLED FACT")
    print(f"  │  memory_id: {mem_id[:30] if mem_id else '(not stored)'}")
    print(f"  ├─ SOURCE OBSERVATIONS ({len(source_obs_ids)} obs)")
    for oid in source_obs_ids[:3]:
        print(f"  │    → {oid[:30]}")
    print(f"  ├─ EXPANDED CONTENT ({len(expanded_obs)} obs fetched via expandIds)")
    all_facts = []
    all_concepts: set[str] = set()
    for obs in expanded_obs:
        all_facts.extend(obs.get("facts", []))
        all_concepts.update(obs.get("concepts", []))
    for fact in all_facts[:4]:
        print(f"  │    fact: {fact[:70]}")
    if all_concepts:
        print(f"  │    concepts: {sorted(all_concepts)[:6]}")
    print(f"  └─ FULL TIMELINE ({len(timeline)} obs in session, sorted by timestamp)")
    print()
    print("  Reading order: memory → sourceObservationIds → expandIds → timeline sort")
    print(
        "  Cost: 1 observe x3 + 1 remember + 1 GET /memories + 1 expandIds + 1 GET /observations"
    )
    print("  NO full-context dump needed — retrieve only what provenance points to")


# ══════════════════════════════════════════════════════════════════════════════
# Bonus — DELETED LESSONS: tombstone pattern via /export
# ══════════════════════════════════════════════════════════════════════════════


def bonus_deleted_lessons() -> None:
    """
    Demonstrate the lesson tombstone pattern.

    Lessons have a deleted?: boolean field. When deleted, they use a tombstone
    pattern — the record stays in KV but is ignored by recall/context paths.

    GET /agentmemory/lessons lists only ACTIVE lessons (no deleted: true).
    GET /agentmemory/export includes ALL records — filter client-side for deleted: true.

    This shows how to audit what lessons have been tombstoned.
    """
    print("\n  [BONUS: DELETED LESSONS]  Tombstone pattern — GET /export")
    print("  Active lessons: GET /agentmemory/lessons (deleted lessons excluded)")
    print(
        "  All lessons incl. tombstones: GET /agentmemory/export → filter deleted:true"
    )

    # 1. List active lessons
    active_resp = call(
        "GET",
        "/agentmemory/lessons",
        params={"limit": 50, "project": PROJECT},
    )
    active = active_resp.get("lessons", [])
    print(f"\n  Active lessons: {len(active)}")
    for l in active[:3]:
        conf = l.get("confidence", "?")
        content = (l.get("content") or "")[:60]
        print(f'    conf={conf}  "{content}"')

    # 2. Export to find tombstones
    print("\n  Checking export for tombstoned (deleted: true) lessons...")
    try:
        export_resp = call(
            "GET",
            "/agentmemory/export",
            params={"maxSessions": 1},  # small export for demo
        )
        export_lessons = export_resp.get("lessons", [])
        tombstoned = [l for l in export_lessons if l.get("deleted")]
        active_in_export = [l for l in export_lessons if not l.get("deleted")]
        print(
            f"  Export: {len(export_lessons)} total lessons  "
            f"({len(active_in_export)} active, {len(tombstoned)} tombstoned)"
        )
        for l in tombstoned[:3]:
            lid = (l.get("id") or "")[:20]
            content = (l.get("content") or "")[:60]
            print(f'    TOMBSTONE id={lid}  "{content}"')
        if not tombstoned:
            print("  (no tombstoned lessons in this export slice)")
            print(
                "  Note: lessons become deleted: true via lesson lifecycle decay or explicit delete"
            )
    except httpx.HTTPStatusError as e:
        print(f"  export returned HTTP {e.response.status_code}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def run() -> None:
    banner("Progressive Disclosure — Provenance Traversal")
    print(
        """
  This scenario walks the FULL provenance chain from distilled facts to raw events.
  Unlike retrieve_information_basic.py (direct queries), this demonstrates HOW facts
  connect back to the episodic observations that created them.

  Key insight: smart-search expandIds skips BM25/vector entirely — it's the correct
  tool AFTER you have obs IDs from provenance, not for fresh search.
  """
    )
    check_health()

    uid = uuid.uuid4().hex[:8]

    step(1, "Seed: create mock observations + link them to a memory")
    session_id, obs_ids, mem_id = phase_1_seed_data(uid)

    step(2, "Inverse: list memories → extract sourceObservationIds (backward pointer)")
    source_obs_ids = phase_2_inverse_engineering(mem_id, obs_ids)

    step(3, "Expand: expandIds → KV direct lookup (no scoring, no embedding)")
    expanded_obs = phase_3_expand_observations(source_obs_ids)

    step(4, "Timeline: GET /observations?sessionId → sort by timestamp")
    timeline = phase_4_timeline_reconstruction(session_id, expanded_obs)

    step(5, "Synthesis: assemble distilled fact → source obs → full timeline")
    phase_5_synthesis(mem_id, source_obs_ids, expanded_obs, timeline)

    step(6, "Bonus: deleted lessons — tombstone pattern via /export")
    bonus_deleted_lessons()

    print(f"\n{'═' * 72}")
    print("  PROGRESSIVE DISCLOSURE SUMMARY")
    print(f"{'─' * 72}")
    print("  1. POST /observe x3           → create episodic events (with sessionId)")
    print("  2. POST /remember              → save fact with sourceObservationIds")
    print("  3. GET  /memories              → find fact → extract sourceObservationIds")
    print("  4. POST /smart-search expandIds → KV direct fetch (NO scoring overhead)")
    print("  5. GET  /observations?sessionId → full chronological timeline")
    print(
        "  6. GET  /export                → all lessons incl. tombstoned (deleted:true)"
    )
    print(f"{'═' * 72}")
    print("  PAYLOADS:")
    print("    observe:        {hookType, sessionId, project, cwd, timestamp, data}")
    print(
        "    remember:       {content, type, project, concepts, files, sourceObservationIds}"
    )
    print("    memories:       {limit, project}")
    print("    expandIds:      {expandIds: [obs_id, ...], limit}")
    print("    observations:   ?sessionId=...")
    print("    export:         ?maxSessions=1")
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
