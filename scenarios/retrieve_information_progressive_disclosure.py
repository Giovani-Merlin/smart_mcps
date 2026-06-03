"""
SCENARIO: Progressive Disclosure — Provenance Traversal
=========================================================
Demonstrates the NATURAL agent workflow for retrieving detailed context
progressively — starting from a search hit and expanding into full narrative,
timeline, then tracing back to the distilled fact.

Correct "reading order" (notebook-validated):
  PHASE 1 — SEED:      Create 3 mock observations + a memory linking them +
                        a lesson (for tombstone demo in phase 8).
  PHASE 2 — DISCOVER:  POST /smart-search with query → compact hits (the "needle").
  PHASE 3 — EXPAND:    POST /smart-search expandIds → full narrative (the "hay").
                        Skips BM25/vector entirely — direct KV lookup.
  PHASE 4 — TIMELINE:  GET /observations?sessionId → sort by timestamp → chronological order.
  PHASE 5 — INVERSE:   GET /memories → find memory → extract sourceObservationIds.
                        (Backward engineering: which raw events birthed this fact?)
  PHASE 6 — SYNTHESIS: Assemble: distilled fact ← source obs ← expanded content ← timeline.
  PHASE 7 — CLEANUP:   POST /forget {sessionId} + DELETE /governance/memories.
                        Scenarios must clean up what they create.
  PHASE 8 — LESSONS:   Create a lesson, list active lessons, explain tombstone pattern.
                        (Lessons CANNOT be manually hard-deleted — they auto-decay.)

Key findings from NotebookLM:
  - POST /observe triggers mem::observe: SHA-256 dedup within time window, synthetic
    compression, indexes into BM25+vector stores. Response: 201 with raw result.
  - expandIds skips query-based search entirely — direct KV lookups, records access
    for retention scoring. Up to 20 IDs per call.
  - GET /observations?sessionId lists ALL CompressedObservation objects for a session.
    Sort client-side by timestamp to get chronological reading order.
  - Lesson tombstone: deleted: true is set automatically by mem::lesson-decay-sweep.
    No manual delete endpoint exists. GET /export includes tombstoned records.
  - POST /forget {sessionId} removes obs from KV + BM25/vector indexes synchronously.
  - DELETE /governance/memories {memoryIds, reason} creates an audit trail entry.

Run:
    python scenarios/retrieve_information_progressive_disclosure.py
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
# Phase 1 — SEED: create obs + memory + lesson
# ══════════════════════════════════════════════════════════════════════════════


def phase_1_seed_data(uid: str) -> tuple[str, list[str], str, str]:
    """
    Create 3 mock observations in one session, a durable memory linking them,
    and a lesson (to have something for the tombstone demo in phase 8).

    POST /agentmemory/observe
    MINIMAL: {hookType, sessionId, project, cwd, timestamp, data}

    POST /agentmemory/remember
    MINIMAL: {content, type, project, concepts, files}
    NOTE: sourceObservationIds is optional — agents rarely provide it manually.
    The consolidation pipeline stamps it during background processing.
    We include it here explicitly only to demonstrate the progressive disclosure path.

    POST /agentmemory/lessons
    MINIMAL: {content, project, confidence, source}

    Returns (session_id, obs_ids, mem_id, lesson_id).
    """
    print(
        "\n  [PHASE 1: SEED]  POST /observe x3 → POST /remember → POST /lessons"
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

        # Response is 201: {"observationId": "obs_XXX"}
        # NOTE: key is 'observationId' (not 'obsId', not 'id', not 'observation.id')
        obs_id = resp.get("observationId")
        if obs_id:
            obs_ids.append(obs_id)
            print(f"  obs {i+1}: id={obs_id[:24]}  tool={event_data['tool_name']}")
        else:
            print(f"  obs {i+1}: created (no 'observationId' in response — dedup may have fired)")
            print(f"    resp keys: {list(resp.keys())[:6]}")

    # Save durable memory with explicit sourceObservationIds for PD demonstration
    print(
        f"\n  [save memory]  POST /remember  sourceObservationIds={obs_ids[:3]}"
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
            # sourceObservationIds: agents don't normally provide this.
            # Here we set it explicitly so phase 5 (inverse) can demonstrate
            # the backward pointer from distilled fact to raw events.
            "sourceObservationIds": obs_ids,
        },
    )
    memory = mem_resp.get("memory", mem_resp)
    mem_id = memory.get("id", "")
    print(f"  memory saved: id={mem_id[:30] if mem_id else '(no id returned)'}")

    # Save a lesson — needed for phase 8 tombstone demo
    print(f"\n  [save lesson]  POST /lessons  source=manual")
    try:
        lesson_resp = call(
            "POST",
            "/agentmemory/lessons",
            body={
                "content": f"[pd-scenario-{uid}] Always strip whitespace from Bearer tokens "
                "before JWT decode — silent failures are hard to trace.",
                "project": PROJECT,
                "confidence": 0.6,
                "source": "manual",
                "tags": ["jwt", "auth", "whitespace"],
                "context": "JWT middleware debugging",
            },
        )
        lesson = lesson_resp.get("lesson", lesson_resp)
        lesson_id = lesson.get("id", "")
        print(f"  lesson saved: id={lesson_id[:30] if lesson_id else '(no id returned)'}")
    except httpx.HTTPStatusError as e:
        print(f"  lesson save failed: HTTP {e.response.status_code}")
        lesson_id = ""

    return session_id, obs_ids, mem_id, lesson_id


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — DISCOVER: search for the needle
# ══════════════════════════════════════════════════════════════════════════════


def phase_2_discovery(uid: str) -> list[dict]:
    """
    Start with a semantic search — this is where a real agent begins.
    The agent doesn't know the obs IDs upfront; it searches for relevant content.

    POST /agentmemory/smart-search
    MINIMAL: {query, project, limit}

    Compact result shape: {obsId, score, sessionId, timestamp, title, type}
    NOTE: 'obsId' (not 'id'). Score is 'score' (not 'combinedScore').

    Returns {obsId, sessionId} pairs — both are needed for expandIds in phase 3.
    IMPORTANT: Only obs from EXISTING (compressed) sessions appear here.
    Obs created via /observe are compressed asynchronously — not immediately searchable.

    This is the CORRECT starting point — not retrieving IDs upfront.
    """
    print(f"\n  [PHASE 2: DISCOVER]  POST /smart-search {{query, project, limit}}")
    print("  Compact result: {obsId, score, sessionId, title, type}  (obsId, not id)")

    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": f"jwt bearer whitespace auth middleware {uid}",
            "project": PROJECT,
            "limit": 5,
        },
    )

    results = resp.get("results", [])
    print(f"  mode={resp.get('mode', '?')}  hits={len(results)}")

    # Collect {obsId, sessionId} pairs — both required for reliable expandIds
    hit_pairs: list[dict] = []
    for r in results[:5]:
        obs_id = r.get("obsId") or r.get("id") or ""
        session_id = r.get("sessionId") or ""
        score = r.get("score", 0)
        obs_type = r.get("type", "?")
        title = (r.get("title") or "")[:50]
        if obs_id and obs_id.startswith("obs_"):  # skip memory IDs (mem_...)
            hit_pairs.append({"obsId": obs_id, "sessionId": session_id})
        print(f"    [{obs_type:12}] score={score:.4f}  obsId={obs_id[:24]}")
        print(f'      title: "{title}"')

    print(f"\n  Collected {len(hit_pairs)} obs pairs → passing to phase 3 (expandIds)")
    return hit_pairs


# ══════════════════════════════════════════════════════════════════════════════
# Phase 3 — EXPAND: fetch full narrative for the hit IDs
# ══════════════════════════════════════════════════════════════════════════════


def phase_3_expand_observations(hit_pairs: list[dict]) -> list[dict]:
    """
    Use expandIds to KV-fetch full observation objects — no scoring overhead.

    POST /agentmemory/smart-search
    WITH expandIds: [{obsId, sessionId}, ...]  (objects, NOT plain strings)

    CRITICAL: expandIds must be objects {obsId, sessionId}, not plain strings.
    Passing plain strings returns 0 results or unreliable partial results.
    sessionId is the fast-path hint that makes the KV lookup reliable.

    When expandIds is provided, query-based search is SKIPPED ENTIRELY.
    Returns result shape: [{obsId, observation: CompressedObservation, sessionId}]
    Full content is nested under 'observation' key (not flattened).

    IMPORTANT: Only works with COMPRESSED obs IDs (from existing sessions).
    Obs created via /observe are compressed asynchronously — expandIds returns 0
    if called immediately after /observe. They appear in search after compression runs.
    Max 20 IDs per call.
    """
    if not hit_pairs:
        print("\n  [PHASE 3: EXPAND]  no hit pairs from phase 2 — skipping")
        return []

    print(
        f"\n  [PHASE 3: EXPAND]  POST /smart-search expandIds=[{{obsId, sessionId}}, ...]"
    )
    print("  CRITICAL: expandIds requires objects {obsId, sessionId}, not plain strings")
    print("  expandIds mode: skips BM25/vector entirely — direct KV lookup")

    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "expandIds": hit_pairs[:20],  # max 20 per call; must be {obsId, sessionId} objects
            "limit": 20,
        },
    )

    # Result shape: [{obsId, observation: {id, narrative, facts, concepts, ...}, sessionId}]
    raw_results = resp.get("results", [])
    print(f"  expanded {len(hit_pairs)} pairs → {len(raw_results)} full observation objects")
    expanded: list[dict] = []
    for item in raw_results[:4]:
        obs = item.get("observation") or item  # content is under 'observation' key
        obs_id = (item.get("obsId") or obs.get("id") or "")[:24]
        obs_type = obs.get("type", "?")
        session = (item.get("sessionId") or obs.get("sessionId") or "")[:24]
        title = (obs.get("title") or "")[:60]
        narrative = (obs.get("narrative") or "")[:80]
        facts = obs.get("facts", [])
        print(f"    [{obs_type:12}] obsId={obs_id}  session={session}")
        print(f'      title: "{title}"')
        if narrative:
            print(f'      narrative: "{narrative}"')
        if facts:
            print(f"      facts: {facts[:2]}")
        expanded.append(obs)  # store the inner observation for downstream use

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
    to get the chronological reading order. Reveals what the agent was doing
    before and after the key events — full context, not just the hits.

    Hit obs from phase 3 become bookmarks in this larger timeline.
    """
    # Determine which session to query
    query_session = session_id
    if not query_session and expanded_obs:
        query_session = expanded_obs[0].get("sessionId", "")

    print(
        f"\n  [PHASE 4: TIMELINE]  GET /observations?sessionId={query_session[:24]}..."
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

    # Mark which obs were our phase-3 expanded observations (bookmarks)
    # expanded_obs contains CompressedObservation objects (with 'id' field)
    source_ids = {o.get("id") for o in expanded_obs if o.get("id")}

    # NOTE: GET /observations returns RAW observation shape, NOT CompressedObservation!
    # Raw fields: {hookType, id, raw, sessionId, timestamp, toolInput, toolName, toolOutput}
    # There is NO 'type', 'title', 'narrative', 'facts' in raw observations.
    # Those fields only exist after mem::compress runs (async) on the raw record.
    print("  Chronological timeline — RAW obs shape (no type/title until compression runs):")
    print("  Fields: {id, hookType, timestamp, toolName, toolInput}")
    for obs in sorted_obs[:8]:
        obs_id = (obs.get("id") or "")[:24]
        ts = (obs.get("timestamp") or "")[:19]
        hook = obs.get("hookType", "?")
        tool = obs.get("toolName", obs.get("type", "?"))
        tool_input = (obs.get("toolInput") or obs.get("title") or "")[:40]
        marker = " ◀ BOOKMARKED" if obs.get("id") in source_ids else ""
        print(f"    {ts}  [{hook:18}] tool={tool}  input={tool_input}{marker}")

    return sorted_obs


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5 — INVERSE: walk backward from memories to source obs
# ══════════════════════════════════════════════════════════════════════════════


def phase_5_inverse_engineering(mem_id: str, obs_ids: list[str]) -> list[str]:
    """
    Walk backward: list all memories, find our fact, extract sourceObservationIds.

    GET /agentmemory/memories
    MINIMAL: {limit, project}

    This is "inverse engineering" provenance — starting from a known memory ID,
    find which raw observations were the source of that distilled fact.
    Those obs IDs can then be passed to expandIds for a targeted deep-read.

    Note: sourceObservationIds is stamped by the consolidation pipeline.
    If we saved the memory manually with explicit sourceObservationIds (as in phase 1),
    they appear here. In normal agent flows, this field is empty until consolidation runs.
    """
    print("\n  [PHASE 5: INVERSE]  GET /memories → extract sourceObservationIds")
    print("  (walks backward: distilled fact → raw events that created it)")

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
        print(f"  found memory id={mem_id[:30]}")
        print(f"  type={target.get('type')}  concepts={target.get('concepts', [])[:4]}")
        print(f"  files={target.get('files', [])[:3]}")
        print(f"  sourceObservationIds ({len(source_obs)}): {[s[:20] for s in source_obs[:3]]}")
        return source_obs
    else:
        # Fallback: search for any memory with sourceObservationIds
        linked = [m for m in all_memories if m.get("sourceObservationIds")]
        print(
            f"  target not found — scanning {len(linked)} memories with sourceObservationIds"
        )
        if linked:
            return linked[0].get("sourceObservationIds", [])
        print("  [no memories with sourceObservationIds — using seeded obs_ids]")
        return obs_ids


# ══════════════════════════════════════════════════════════════════════════════
# Phase 6 — SYNTHESIS: assemble the full provenance picture
# ══════════════════════════════════════════════════════════════════════════════


def phase_6_synthesis(
    mem_id: str,
    source_obs_ids: list[str],
    expanded_obs: list[dict],
    timeline: list[dict],
) -> None:
    """
    Print the assembled picture: search hit → expanded detail → timeline → memory.

    The full provenance chain runs:
      1. Discovery (phase 2): "jwt whitespace" → compact hit (obsId)
      2. Expansion (phase 3): expandIds → full narrative, facts, concepts
      3. Timeline (phase 4): sessionId → all obs chronologically sorted
      4. Inverse (phase 5): mem_id → sourceObservationIds (backward pointer)

    This is the correct reading order for progressive disclosure.
    """
    print("\n  [PHASE 6: SYNTHESIS]  Assembled provenance picture:")
    print("  ┌─ DISCOVERY HIT (phase 2)")
    print(f"  │  {len(expanded_obs)} hit IDs expanded")
    print(f"  ├─ EXPANDED CONTENT (phase 3)")
    all_facts = []
    all_concepts: set[str] = set()
    for obs in expanded_obs:
        all_facts.extend(obs.get("facts", []))
        all_concepts.update(obs.get("concepts", []))
    for fact in all_facts[:4]:
        print(f"  │    fact: {fact[:70]}")
    if all_concepts:
        print(f"  │    concepts: {sorted(all_concepts)[:6]}")
    print(f"  ├─ TIMELINE (phase 4): {len(timeline)} obs in session, sorted by timestamp")
    print(f"  ├─ INVERSE PROVENANCE (phase 5)")
    print(f"  │  memory_id: {mem_id[:40] if mem_id else '(not stored)'}")
    for oid in source_obs_ids[:3]:
        print(f"  │    → sourceObs: {oid[:30]}")
    print("  └─ READING ORDER")
    print()
    print("  search → expandIds → GET /observations?sessionId → GET /memories")
    print("  Cost: 1 smart-search + 1 expandIds + 1 GET /observations + 1 GET /memories")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 7 — CLEANUP: delete everything we created
# ══════════════════════════════════════════════════════════════════════════════


def phase_7_cleanup(uid: str, session_id: str, mem_id: str) -> None:
    """
    Clean up what we created in phase 1. Scenarios must not leave test data behind.

    POST /agentmemory/forget {sessionId}
      → removes all observations for the session from KV + BM25/vector indexes
      → synchronous (search indexes updated immediately)

    DELETE /agentmemory/governance/memories {memoryIds, reason}
      → hard-deletes memories with an audit trail entry
      → reason is required for governance audit log

    NOTE: Lessons CANNOT be manually deleted.
    They are tombstoned automatically by mem::lesson-decay-sweep when confidence
    drops below threshold via non-use. See phase 8 for tombstone demonstration.
    """
    print(f"\n  [PHASE 7: CLEANUP]  Removing test data created in phase 1")

    # 1. Forget all observations in the test session
    if session_id:
        print(f"  POST /forget  {{sessionId: {session_id[:30]}}}")
        try:
            forget_resp = call(
                "POST",
                "/agentmemory/forget",
                body={"sessionId": session_id},
            )
            removed = forget_resp.get("removed", forget_resp.get("deleted", "?"))
            print(f"  → session forgotten: {removed}")
        except httpx.HTTPStatusError as e:
            print(f"  → forget failed: HTTP {e.response.status_code}")
    else:
        print("  → no session_id to forget")

    # 2. Delete the memory (with audit trail)
    if mem_id:
        print(f"  DELETE /governance/memories  {{memoryIds: [{mem_id[:30]}], reason: ...}}")
        try:
            del_resp = call(
                "DELETE",
                "/agentmemory/governance/memories",
                body={
                    "memoryIds": [mem_id],
                    "reason": f"pd-scenario-{uid} cleanup — test data",
                },
            )
            deleted = del_resp.get("deleted", del_resp.get("success", "?"))
            print(f"  → memory deleted: {deleted}")
        except httpx.HTTPStatusError as e:
            print(f"  → memory delete failed: HTTP {e.response.status_code}")
    else:
        print("  → no mem_id to delete")

    print("  → NOTE: lesson created in phase 1 CANNOT be manually deleted.")
    print("    It will be tombstoned automatically when confidence decays to 0.")
    print("    See phase 8 for the tombstone pattern.")


# ══════════════════════════════════════════════════════════════════════════════
# Phase 8 — LESSONS: tombstone pattern
# ══════════════════════════════════════════════════════════════════════════════


def phase_8_lesson_tombstones(uid: str, lesson_id: str) -> None:
    """
    Demonstrate the lesson lifecycle — creation, active state, tombstone pattern.

    Lessons have a deleted?: boolean field. There is NO manual delete endpoint.
    They are tombstoned (deleted: true) automatically by mem::lesson-decay-sweep
    when confidence drops below threshold due to non-use.

    GET /agentmemory/lessons → active lessons only (deleted: true excluded)
    GET /agentmemory/export  → ALL records including tombstones

    We created a lesson in phase 1. Here we verify it's active and show how
    to audit tombstoned lessons that exist from past decay sweeps.
    """
    print("\n  [PHASE 8: LESSONS]  Active lessons + tombstone pattern via /export")
    print("  IMPORTANT: No manual delete endpoint for lessons.")
    print("  Lessons tombstone automatically when confidence decays via non-use.")

    # 1. Show the lesson we created (should be active)
    if lesson_id:
        print(f"\n  Looking for our lesson: id={lesson_id[:30]}")

    active_resp = call(
        "GET",
        "/agentmemory/lessons",
        params={"limit": 50, "project": PROJECT},
    )
    active = active_resp.get("lessons", [])
    print(f"\n  Active lessons total: {len(active)}")

    # Find our specific lesson
    our_lesson = next(
        (l for l in active if l.get("id") == lesson_id), None
    ) if lesson_id else None

    if our_lesson:
        conf = our_lesson.get("confidence", "?")
        content = (our_lesson.get("content") or "")[:60]
        print(f"  Our lesson (active): conf={conf}  tags={our_lesson.get('tags', [])}")
        print(f'    "{content}"')
    else:
        for l in active[:3]:
            print(f"    conf={l.get('confidence')}  \"{(l.get('content') or '')[:60]}\"")

    # 2. Scan export for tombstoned lessons
    print("\n  Scanning export for tombstoned (deleted: true) lessons...")
    try:
        export_resp = call(
            "GET",
            "/agentmemory/export",
            params={"maxSessions": 2},  # small slice for demo
        )
        export_lessons = export_resp.get("lessons", [])
        tombstoned = [l for l in export_lessons if l.get("deleted")]
        active_in_export = [l for l in export_lessons if not l.get("deleted")]
        print(
            f"  Export slice: {len(export_lessons)} lessons total  "
            f"({len(active_in_export)} active, {len(tombstoned)} tombstoned)"
        )
        for l in tombstoned[:3]:
            lid = (l.get("id") or "")[:20]
            content = (l.get("content") or "")[:60]
            conf = l.get("confidence", "?")
            print(f'  TOMBSTONE id={lid}  conf={conf}  "{content}"')
        if not tombstoned:
            print("  (no tombstoned lessons in this export slice)")
            print("  → lessons tombstone when confidence decays to 0 via non-use")
            print("  → run POST /agentmemory/lessons/strengthen to prevent decay")
    except httpx.HTTPStatusError as e:
        print(f"  export returned HTTP {e.response.status_code}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def run() -> None:
    banner("Progressive Disclosure — Natural Discovery Order")
    print(
        """
  CORRECT AGENT WORKFLOW (discovery-first, not inverse-first):
    Phase 1 — SEED:     Create observations + memory + lesson (test data)
    Phase 2 — DISCOVER: POST /smart-search with query → compact hits (the needle)
    Phase 3 — EXPAND:   POST /smart-search expandIds → full narrative (the hay)
    Phase 4 — TIMELINE: GET /observations?sessionId → chronological reading order
    Phase 5 — INVERSE:  GET /memories → extract sourceObservationIds (backward pointer)
    Phase 6 — SYNTHESIS: assemble full provenance picture
    Phase 7 — CLEANUP:  POST /forget + DELETE /governance/memories (test data removed)
    Phase 8 — LESSONS:  lesson lifecycle + tombstone pattern via /export

  Key: search FIRST, then expand, then trace provenance — not the other way around.
  Key: scenarios must clean up what they create (see phase 7).
  """
    )
    check_health()

    uid = uuid.uuid4().hex[:8]

    step(1, "Seed: create mock observations + memory + lesson")
    session_id, obs_ids, mem_id, lesson_id = phase_1_seed_data(uid)

    step(2, "Discover: POST /smart-search {query} → compact hits (the needle)")
    hit_pairs = phase_2_discovery(uid)

    step(3, "Expand: POST /smart-search {expandIds: [{obsId,sessionId}]} → full narrative")
    expanded_obs = phase_3_expand_observations(hit_pairs)

    step(4, "Timeline: GET /observations?sessionId → sort by timestamp")
    timeline = phase_4_timeline_reconstruction(session_id, expanded_obs)

    step(5, "Inverse: GET /memories → extract sourceObservationIds (backward pointer)")
    source_obs_ids = phase_5_inverse_engineering(mem_id, obs_ids)

    step(6, "Synthesis: assemble discovery → expansion → timeline → provenance")
    phase_6_synthesis(mem_id, source_obs_ids, expanded_obs, timeline)

    step(7, "Cleanup: POST /forget + DELETE /governance/memories")
    phase_7_cleanup(uid, session_id, mem_id)

    step(8, "Lessons: lifecycle + tombstone pattern (no manual delete)")
    phase_8_lesson_tombstones(uid, lesson_id)

    print(f"\n{'═' * 72}")
    print("  PROGRESSIVE DISCLOSURE SUMMARY")
    print(f"{'─' * 72}")
    print("  1. POST /observe x3        → create episodic events")
    print("  2. POST /remember          → save fact (with sourceObservationIds for demo)")
    print("  3. POST /lessons           → save lesson {content, confidence, source, tags}")
    print("  4. POST /smart-search      → {query, project, limit} → compact hits")
    print("  5. POST /smart-search      → {expandIds: [...], limit} → full narrative")
    print("  6. GET  /observations      → ?sessionId=... → sort by timestamp")
    print("  7. GET  /memories          → {project, limit} → find sourceObservationIds")
    print("  8. POST /forget            → {sessionId} → removes obs from KV + indexes")
    print("  9. DELETE /governance/memories → {memoryIds, reason} → audited hard-delete")
    print(" 10. GET  /lessons           → active lessons (deleted: true excluded)")
    print(" 11. GET  /export            → all records including tombstones")
    print(f"{'─' * 72}")
    print("  KEY PARAMETERS:")
    print("    smart-search query:  {query, project, limit, includeLessons?}")
    print("    smart-search expand: {expandIds: [{obsId, sessionId}, ...], limit}  (objects!)")
    print("    remember:            {content, type, project, concepts, files}")
    print("    lessons:             {content, project, confidence, source, tags}")
    print("    forget:              {sessionId} or {observationIds: [...]}")
    print("    governance/memories: {memoryIds: [...], reason}")
    print(f"{'─' * 72}")
    print("  LESSON LIFECYCLE:")
    print("    saved (conf=0.6) → reinforced via /lessons/strengthen → decays on non-use")
    print("    → tombstoned (deleted:true) automatically by mem::lesson-decay-sweep")
    print("    → visible only in GET /export, hidden from /lessons + smart-search")
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
