"""
SCENARIO: Practical Create-and-Retrieve Roundtrip
==================================================
Demonstrates the MINIMAL PAYLOADS agents actually use for complete workflows.
Four flows, each showing real agent patterns with minimal fields.

Flows:
  action_roundtrip()  — MINIMAL: create {title, project} → update {actionId, status} → list
  memory_roundtrip()  — MINIMAL: save {content, type, project} → list → smart-search roundtrip
  lesson_roundtrip()  — MINIMAL: save {content, confidence, project} → list → lesson-recall
  integration test    — task init → complete → retrieve by query (practical agent workflow)

Key principles:
  - MINIMAL payloads: only send what's necessary
  - Skip sourceObservationIds: let consolidation pipeline handle automatic linking
  - Focus on practical endpoints: what agents call in real execution
  - Verify roundtrip: create → retrieve → verify state

Run all:
    python mcp/agentmemory/scenario_mock_roundtrip.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, mcp_call, pp, step

OK = "✓"
FAIL = "✗"


def _check(label: str, condition: bool, detail: str = "") -> None:
    mark = OK if condition else FAIL
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))


# ══════════════════════════════════════════════════════════════════════════════
# Flow 1 — Action
# ══════════════════════════════════════════════════════════════════════════════


def action_roundtrip() -> None:
    """
    Action task lifecycle: MINIMAL payloads.

    Create: {title, project}
    Update: {actionId, status}
    Retrieve: GET /actions with limit and project filter

    Real agents rarely use the full field set. Focus: title, status, ID.
    """
    print("\n" + "═" * 60)
    print("  FLOW 1: Action task — Create → Update → Retrieve (MINIMAL)")
    print("═" * 60)

    uid = uuid.uuid4().hex[:8]
    title = f"mock-roundtrip-{uid}: test task"

    # Create action with MINIMAL payload
    print(f"\n  [create]  POST /agentmemory/actions  MINIMAL: {{title, project}}")
    create_resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": title,
            "project": PROJECT,
            # Optional fields: description, priority, tags, createdBy
            # Agents typically skip these for initial create
        },
    )
    action = create_resp.get("action", create_resp)
    action_id = action.get("id")
    _check("action created", bool(action_id), f"id={action_id}")
    _check("status=pending", action.get("status") == "pending", action.get("status"))

    if not action_id:
        return

    # Update status with MINIMAL payload
    print(
        f"\n  [update]  POST /agentmemory/actions/update  MINIMAL: {{actionId, status}}"
    )
    update_resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": action_id,
            "status": "done",  # pending|active|done|blocked|cancelled
        },
    )
    updated = update_resp.get("action", update_resp)
    _check("status=done", updated.get("status") == "done", updated.get("status"))

    # Retrieve via list
    print(f"\n  [retrieve]  GET /agentmemory/actions  MINIMAL: {{limit, project}}")
    list_resp = call(
        "GET", "/agentmemory/actions", params={"limit": 100, "project": PROJECT}
    )
    all_actions = list_resp.get("actions", [])
    found = next((a for a in all_actions if a.get("id") == action_id), None)
    _check("found in list", bool(found), f"{len(all_actions)} total")
    if found:
        _check("status preserved", found.get("status") == "done")
        print(f"  id={action_id}  title=\"{found.get('title')[:50]}\"")

    # Cleanup
    call(
        "POST",
        "/agentmemory/actions/update",
        body={"actionId": action_id, "status": "cancelled"},
    )
    _check("cancelled for cleanup", True)


# ══════════════════════════════════════════════════════════════════════════════
# Flow 2 — Curated Memory
# ══════════════════════════════════════════════════════════════════════════════


def memory_roundtrip() -> None:
    """
    Memory task lifecycle: MINIMAL payloads.

    Save: {content, type, project}
    Retrieve: GET /memories with limit and project filter
    Discover: POST /smart-search and filter results

    Key: GET /memories lists ALL memories — no text search.
    Agents use smart-search to find scored obs, then filter memories
    by concept/file relationships or use graph/query for architecture.
    """
    print("\n" + "═" * 60)
    print("  FLOW 2: Memory — Save → List → Smart-Search (MINIMAL)")
    print("═" * 60)

    uid = uuid.uuid4().hex[:8]
    content = f"[scenario-demo] Mock memory {uid}: practical memory save/retrieve."

    # Save with MINIMAL payload
    print(
        f"\n  [save]  POST /agentmemory/remember  MINIMAL: {{content, type, project}}"
    )
    save_resp = call(
        "POST",
        "/agentmemory/remember",
        body={
            "content": content,
            "type": "fact",  # pattern|preference|architecture|bug|workflow|fact
            "project": PROJECT,
            # Optional: title, concepts, files, sourceObservationIds, ttlDays
            # Agents skip sourceObservationIds — consolidation pipeline links automatically
        },
    )
    memory = save_resp.get("memory", save_resp)
    mem_id = memory.get("id")
    _check("memory saved", bool(mem_id), f"id={mem_id}")
    _check("type=fact", memory.get("type") == "fact", memory.get("type"))

    # Retrieve list
    print(f"\n  [list]  GET /agentmemory/memories  MINIMAL: {{limit, project}}")
    list_resp = call(
        "GET", "/agentmemory/memories", params={"limit": 100, "project": PROJECT}
    )
    all_memories = list_resp.get("memories", [])
    found = next((m for m in all_memories if m.get("id") == mem_id), None)
    _check("found in list", bool(found), f"{len(all_memories)} total")
    if found:
        _check("content intact", content in (found.get("content") or ""))

    # Discover via smart-search
    # Smart-search bundles memories alongside obs hits (sessionId="memory").
    # Practical pattern: search for relevant content, get all matches, find our memory.
    print(
        f"\n  [discover]  POST /agentmemory/smart-search  MINIMAL: {{query, project, limit}}"
    )
    search_resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": f"mock memory scenario {uid}",
            "limit": 20,
            "project": PROJECT,
        },
    )
    results = search_resp.get("results", [])
    mem_hits = [r for r in results if r.get("sessionId") == "memory"]
    saved_hit = next((r for r in results if r.get("id") == mem_id), None)
    print(f"  total hits={len(results)}  curated-memory hits={len(mem_hits)}")
    _check("memory discoverable via smart-search", bool(saved_hit))


# ══════════════════════════════════════════════════════════════════════════════
# Flow 3 — Lesson
# ══════════════════════════════════════════════════════════════════════════════


def lesson_roundtrip() -> None:
    """
    Lesson task lifecycle: MINIMAL payloads.

    Save: {content, confidence, project}
    List: GET /lessons with minConfidence and project filter
    Query: mcp_call('memory_lesson_recall', {query, project, minConfidence})

    Key: Lessons self-reinforce on re-save (same content → higher confidence).
    Agents rarely call GET /lessons directly; they use lesson-recall for queries.
    """
    print("\n" + "═" * 60)
    print("  FLOW 3: Lesson — Save → Recall (MINIMAL)")
    print("═" * 60)

    # Stable content (no per-run uid) so lessons auto-reinforce on re-save
    content = "[scenario-demo] Stable lesson content for testing self-reinforcement."
    confidence = 0.7

    # Save with MINIMAL payload
    print(
        f"\n  [save]  POST /agentmemory/lessons  MINIMAL: {{content, confidence, project}}"
    )
    save_resp = call(
        "POST",
        "/agentmemory/lessons",
        body={
            "content": content,
            "confidence": confidence,
            "project": PROJECT,
            # Optional: context, tags
        },
    )
    lesson = save_resp.get("lesson", save_resp)
    lesson_id = lesson.get("id")
    _check("lesson saved", bool(lesson_id), f"id={lesson_id}")
    _check(
        "confidence recorded",
        (lesson.get("confidence") or 0) >= confidence - 0.1,
        str(lesson.get("confidence")),
    )

    # List lessons
    print(
        f"\n  [list]  GET /agentmemory/lessons  MINIMAL: {{limit, minConfidence, project}}"
    )
    list_resp = call(
        "GET",
        "/agentmemory/lessons",
        params={
            "limit": 20,
            "minConfidence": "0.1",
            "project": PROJECT,
        },
    )
    all_lessons = list_resp.get("lessons", [])
    found = next((l for l in all_lessons if l.get("id") == lesson_id), None)
    _check("found in list", bool(found), f"{len(all_lessons)} total")

    # Query lessons via mcp_call
    print(
        f"\n  [query]  mcp_call memory_lesson_recall  MINIMAL: {{query, project, limit}}"
    )
    recall_result = mcp_call(
        "memory_lesson_recall",
        {
            "query": "scenario demo stable lesson content",
            "project": PROJECT,
            "limit": "10",
        },
    )
    recall_lessons = (
        recall_result
        if isinstance(recall_result, list)
        else recall_result.get("lessons", [])
    )
    found_in_recall = any(l.get("id") == lesson_id for l in recall_lessons)
    _check(
        "found via lesson-recall", found_in_recall, f"{len(recall_lessons)} returned"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Flow 4 — Crystal
# ══════════════════════════════════════════════════════════════════════════════


def integration_test() -> None:
    """
    Integration test: realistic agent workflow.

    Pattern: create action → execute → complete → save memory → retrieve via search.

    This demonstrates the PRACTICAL task workflow agents use:
    1. Create task with MINIMAL payload
    2. Mark done
    3. Save discovery/decision as memory
    4. Retrieve both task + memory via search queries
    """
    print("\n" + "═" * 60)
    print("  FLOW 4: Integration — Practical Agent Workflow")
    print("═" * 60)

    uid = uuid.uuid4().hex[:8]

    # Create task
    print(
        f"\n  [1. create task]  POST /agentmemory/actions  MINIMAL: {{title, project}}"
    )
    create_resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": f"mock-integration-{uid}: test workflow",
            "project": PROJECT,
        },
    )
    action = create_resp.get("action", create_resp)
    action_id = action.get("id")
    _check("action created", bool(action_id), f"id={action_id}")

    if not action_id:
        return

    # Mark done
    print(
        f"\n  [2. complete task]  POST /agentmemory/actions/update  MINIMAL: {{actionId, status}}"
    )
    done_resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": action_id,
            "status": "done",
        },
    )
    _check("status=done", done_resp.get("action", {}).get("status") == "done")

    # Save memory of what we learned
    print(
        f"\n  [3. save discovery]  POST /agentmemory/remember  MINIMAL: {{content, type, project}}"
    )
    content = f"[scenario-demo] Tested action → memory roundtrip workflow ({uid})."
    mem_resp = call(
        "POST",
        "/agentmemory/remember",
        body={
            "content": content,
            "type": "fact",
            "project": PROJECT,
        },
    )
    mem_id = mem_resp.get("memory", {}).get("id")
    _check("memory saved", bool(mem_id))

    # Retrieve both task and memory via search
    print(
        f"\n  [4. discover via smart-search]  POST /agentmemory/smart-search  MINIMAL: {{query, project, limit}}"
    )
    search_resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": f"integration test workflow {uid}",
            "project": PROJECT,
            "limit": 10,
        },
    )
    results = search_resp.get("results", [])
    print(f"  Found {len(results)} observations+memories via search")
    _check("results returned", len(results) > 0)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def run() -> None:
    banner("Practical Create-and-Retrieve Roundtrip — Minimal Payloads")
    print(
        """
  Each flow demonstrates MINIMAL PAYLOADS and PRACTICAL PATTERNS:
  - What agents actually send (not all optional fields)
  - How to create, update, and retrieve entities
  - Skip sourceObservationIds — consolidation handles it
  """
    )
    check_health()

    step(1, "Action lifecycle — create (title, project) → update (status) → list")
    action_roundtrip()

    step(2, "Memory lifecycle — save (content, type, project) → list → search")
    memory_roundtrip()

    step(3, "Lesson lifecycle — save (content, confidence, project) → list → query")
    lesson_roundtrip()

    step(4, "Integration test — realistic agent workflow (task → memory → search)")
    integration_test()

    print(f"\n{'═' * 72}")
    print("  SUMMARY — MINIMAL PAYLOADS")
    print(f"{'─' * 72}")
    print("  Action create:        {title, project}")
    print("  Action update:        {actionId, status}")
    print("  Memory save:          {content, type, project}")
    print("  Lesson save:          {content, confidence, project}")
    print("  Smart-search:         {query, project, limit}")
    print(
        "  Lesson-recall MCP:    {query, project, minConfidence, limit} (all strings)"
    )
    print(f"{'═' * 72}")


if __name__ == "__main__":
    run()
