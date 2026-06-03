"""
SCENARIO: Task Completion and Distillation Loop
=================================================
An agent successfully completes a task. It must:
  1. Set the action status to "done" (auto-unblocks dependent actions)
  2. Crystallize the completed action chain → compact LLM digest
  3. Save a lesson for recurring patterns found during the task
  4. Save a curated memory for one-off architectural facts

Key endpoints:
  POST /agentmemory/actions/update   → set status="done", attach result
                                        auto-unblocks downstream dependents
  MCP  memory_crystallize            → compress observations into crystal
                                        mem::crystallize
                                        args: {actionIds (comma-sep), project?, sessionId?}
                                        returns: {narrative, keyOutcomes, filesAffected}
  POST /agentmemory/lessons          → save confidence-scored lesson (accumulates!)
                                        body: {content, confidence, context?, project?, tags?}
  POST /agentmemory/remember         → save immutable curated memory
                                        body: {content, type, title?, concepts?, files?, project?}
  GET  /agentmemory/crystals         → list existing crystal digests
  GET  /agentmemory/lessons          → list lessons (verify save)
  GET  /agentmemory/memories         → list curated memories (verify save)

Two-store distinction:
  /remember  → Curated memories. One-off facts. Immutable. Link-followed via observations.
               Use for: trade-offs, discovered constraints, bugs fixed, API shapes.
  /lessons   → Lessons. Confidence 0-1. Self-strengthen when re-saved. Decays if unused.
               Use for: recurring patterns, gotchas that keep biting, procedural rules.

Crystal:
  A crystal is a LLM-generated digest of a completed action chain.
  It contains narrative + keyOutcomes + filesAffected and becomes
  retrievable future context without raw observation noise.

  Note: POST /agentmemory/crystallize does NOT exist (404).
  Crystallize is only available via POST /agentmemory/mcp/call.

Run:
    python mcp/agentmemory/scenario_task_complete.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, mcp_call, pp, step


def create_test_action(title: str, description: str = "") -> dict:
    """
    Create an action for this scenario.

    POST /agentmemory/actions
    body: {title, description, priority, createdBy, parentId, tags, project,
           sourceMemoryIds, sourceObservationIds, edges}

    All fields:
      title:                 required
      description:           optional prose description
      priority:              optional 1-10 (10=highest urgency)
      createdBy:             optional agent identity for audit trail
      parentId:              optional parent action ID for sub-task hierarchy
      tags:                  optional list of string slugs
      project:               optional stable project slug
      sourceMemoryIds:       optional memory IDs informing this task
                             (engine smart-links on create)
      sourceObservationIds:  optional obs IDs that led to creating this task
      edges:                 optional [{type, targetActionId}] for dependency DAG
                             valid types: requires|unlocks|spawned_by|gated_by|conflicts_with

    Returns {action: {id, title, status, ...}}
    """
    print(f"\n  POST {BASE_URL}/agentmemory/actions")
    resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": title,
            "description": description,
            "priority": 5,
            "createdBy": "gionodes/agent",  # agent identity for audit trail
            "parentId": None,  # None = top-level action
            "tags": ["type:test", "scenario:task_complete"],
            "project": PROJECT,
            "sourceMemoryIds": [],  # memory IDs informing this task (engine smart-links on create)
            "sourceObservationIds": [],  # obs IDs that led to creating this task
            "edges": [],  # [{type, targetActionId}] for dependency DAG
        },
    )
    action = resp.get("action", resp)
    print(f"  Created: id={action.get('id')}  status={action.get('status')}")
    return action


def complete_action(action_id: str, result: str) -> dict:
    """
    Mark an action as done with a result summary.

    POST /agentmemory/actions/update
    body: {actionId, status: "done", result}

    WHY status="done" matters: the engine auto-unblocks any downstream actions
    that listed this action_id in their `edges` as a dependency.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/actions/update  status=done")
    resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": action_id,
            "status": "done",
            "result": result,
        },
    )
    action = resp.get("action", resp)
    print(f"  status is now: {action.get('status', '?')}")
    return resp


def crystallize(action_ids: list[str]) -> dict:
    """
    Compress completed action chain into an LLM crystal digest.

    MCP function: memory_crystallize → mem::crystallize
    Endpoint: POST /agentmemory/mcp/call
    args: {actionIds (comma-sep), project?, sessionId?}

    Returns:
        narrative:     prose summary of what was accomplished
        keyOutcomes:   bullet list of concrete outcomes
        filesAffected: files changed during the chain

    WHY via mcp/call: POST /agentmemory/crystallize returns 404.
    Crystallize is only exposed as a mem:: function through the MCP call protocol.

    Note: the LLM needs enough observations from these actions to generate
    a meaningful crystal. If the actions have very few observations, the
    crystal may be sparse or the call may fail gracefully.
    """
    ids_str = ",".join(action_ids)
    print(f"\n  mcp_call memory_crystallize  actionIds={ids_str[:80]}")
    result = mcp_call(
        "memory_crystallize",
        {
            "actionIds": ids_str,
            "project": PROJECT,
        },
    )
    return result


def save_lesson(content: str, context: str, confidence: float, tags: list[str]) -> dict:
    """
    Save a confidence-scored lesson.

    POST /agentmemory/lessons
    body: {content, confidence, context, project, tags}

    confidence: 0.0–1.0. Auto-strengthens if the same insight is re-saved.
    context:    where/when the lesson applies.
    tags:       list of strings for filtering (not comma-sep here — actual list).

    Lessons are the right store for:
      "always check migration files before using user schema docs"
      "rtmpose3d requires CUDA 11.8 specifically"
      "the /crystallize REST endpoint returns 404 — use mcp/call"
    """
    print(f"\n  POST {BASE_URL}/agentmemory/lessons")
    resp = call(
        "POST",
        "/agentmemory/lessons",
        body={
            "content": content,
            "confidence": confidence,
            "context": context,
            "project": PROJECT,
            "tags": tags,
        },
    )
    lesson = resp.get("lesson", resp)
    print(
        f"  Saved lesson: id={lesson.get('id', '?')}  confidence={lesson.get('confidence', confidence)}"
    )
    return resp


def save_memory(
    content: str,
    memory_type: str,
    title: str,
    concepts: list[str],
    files: list[str],
    source_obs_ids: list[str] | None = None,
) -> dict:
    """
    Save an immutable curated memory with full provenance.

    POST /agentmemory/remember
    body: {content, type, title, concepts, files, project, agentId,
           sourceObservationIds, ttlDays}

    Fields:
      content:              the fact/decision to record
      type:                 CLOSED ENUM: pattern|preference|architecture|bug|workflow|fact
                            anything else silently coerced to "fact"
      title:                short label for future linkage
      concepts:             string slugs for KG concept nodes
                            keep consistent: "agentmemory" not "AgentMemory"/"agent-memory"
      files:                file paths → KG file nodes (architectural anchors)
      project:              stable project slug
      agentId:              agent identity (falls back to AGENT_ID env var if omitted)
      sourceObservationIds: PROVENANCE — obs IDs that support this fact
                            memory_verify traces citation chain through these
      ttlDays:              None = permanent; set e.g. 30 for provisional memories

    Provenance pattern (citation-first):
      Before saving, search for related obs IDs via smart-search (format="compact",
      sessionId != "memory"). Pass those as sourceObservationIds so memory_verify
      can trace the citation chain back to episodic evidence.

    Curated memories are:
      - Immutable after save (no update endpoint)
      - Reached via link-following from scored observations
        (GET /memories?q= is ignored server-side; use smart-search for semantic search)
      - Good for: API shapes, architectural constraints, one-off decisions

    WHY not /lessons: lessons self-strengthen; memories are permanent records.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/remember  type={memory_type}")
    resp = call(
        "POST",
        "/agentmemory/remember",
        body={
            "content": content,
            "type": memory_type,  # CLOSED ENUM: pattern|preference|architecture|bug|workflow|fact
            # anything else silently coerced to "fact"
            "title": title,
            "concepts": concepts,  # string slugs for KG concept nodes
            # keep consistent: "agentmemory" not "AgentMemory"/"agent-memory"
            "files": files,  # file paths → KG file nodes (architectural anchors)
            "project": PROJECT,
            "agentId": "gionodes/agent",  # agent identity (falls back to AGENT_ID env var if omitted)
            "sourceObservationIds": source_obs_ids
            or [],  # PROVENANCE: obs that support this fact
            # memory_verify traces citation chain through these
            "ttlDays": None,  # None = permanent; set e.g. 30 for provisional memories
        },
    )
    memory = resp.get("memory", resp)
    print(
        f"  Saved memory: id={memory.get('id', '?')}  type={memory.get('type', memory_type)}"
    )
    return resp


def list_crystals(limit: int = 5) -> list[dict]:
    """
    List recent crystal digests.

    GET /agentmemory/crystals
    params: {limit, project}

    Each crystal: {id, sessionId, narrative, keyOutcomes, filesAffected}
    """
    resp = call(
        "GET", "/agentmemory/crystals", params={"limit": limit, "project": PROJECT}
    )
    return resp.get("crystals", [])


def run() -> None:
    banner("Task Completion and Distillation Loop")
    print(
        """
  Pattern: push code → tests pass → mark done → crystallize → lesson + memory.
  The crystal distills noisy observations into a compact searchable digest.
  """
    )
    check_health()

    uid = uuid.uuid4().hex[:8]
    task_title = f"scenario-complete-{uid}: Implement memory proxy lesson_save tool"
    task_desc = "Add memory_lesson_save tool to agentmemory_mcp_proxy.py. Endpoint: POST /agentmemory/lessons."

    # ── Step 1: create test action ─────────────────────────────────────────
    step(1, "Create a test action to work with")
    action = create_test_action(task_title, task_desc)
    action_id = action.get("id")
    if not action_id:
        print("  [ERROR] No action ID returned")
        return
    print(f"\n  action_id: {action_id}")

    # ── Step 2: complete the action ────────────────────────────────────────
    step(2, "Mark action as done (status=done auto-unblocks dependents)")
    print(
        """
  POST /agentmemory/actions/update with status="done".
  The engine propagates unblock signals to any downstream dependents automatically.
  """
    )
    complete_action(
        action_id,
        result=f"Added memory_lesson_save tool to proxy. Tested in test_mcp_proxy.py.",
    )

    # ── Step 3: crystallize the completed action chain ─────────────────────
    step(3, "Crystallize — compress action chain into LLM digest")
    print(
        """
  mem::crystallize triggers an LLM to read all observations from these actions
  and produce: narrative (prose), keyOutcomes (bullets), filesAffected (paths).
  The crystal is indexed and becomes retrievable context for future agents.

  Route: POST /agentmemory/mcp/call  (NOT /crystallize — that returns 404)
  """
    )
    crystal = crystallize([action_id])
    if crystal.get("error") or crystal.get("raw"):
        print(f"\n  [crystallize result — may be sparse with few observations]")
        pp(crystal, "raw result")
    else:
        print(f"\n  [crystal produced]")
        print(f"    narrative:     {str(crystal.get('narrative', ''))[:120]}")
        key_outcomes = crystal.get("keyOutcomes", [])
        print(f"    keyOutcomes:   {key_outcomes[:3]}")
        files_affected = crystal.get("filesAffected", [])
        print(f"    filesAffected: {files_affected[:5]}")

    # ── Step 4: save a lesson ──────────────────────────────────────────────
    step(4, "Save lesson — recurring pattern discovered during task")
    print(
        """
  Use POST /agentmemory/lessons for things that recur across sessions.
  If you save the same lesson again later, its confidence auto-increases.
  This makes high-frequency lessons surface more reliably in future searches.
  """
    )
    lesson_content = (
        "POST /agentmemory/crystallize returns 404. "
        "Crystallize is only accessible via POST /agentmemory/mcp/call "
        "with name=memory_crystallize and arguments={actionIds: comma-sep}."
    )
    lesson_resp = save_lesson(
        content=lesson_content,
        context="agentmemory MCP proxy development",
        confidence=0.9,
        tags=["source:codebase", "type:constraint", "area:mcp"],
    )

    # ── Step 5: save a curated memory ─────────────────────────────────────
    step(5, "Save curated memory — one-off architectural fact")
    print(
        """
  Use POST /agentmemory/remember for immutable facts: API shapes, trade-offs,
  architectural decisions that shouldn't change. Unlike lessons, memories don't
  strengthen — they're permanent records linked to specific observations.
  """
    )
    memory_content = (
        "agentmemory has two distinct memory stores: "
        "(1) Curated memories via POST /remember — immutable, link-followed via observations. "
        "(2) Lessons via POST /lessons — confidence 0-1, self-strengthen on reinforcement. "
        "Always choose the right store: memories for one-off facts, lessons for recurring wisdom."
    )
    memory_resp = save_memory(
        content=memory_content,
        memory_type="architecture",
        title="agentmemory two-store distinction",
        concepts=[
            "agentmemory",
            "lessons",
            "curated memories",
            "POST /remember",
            "POST /lessons",
        ],
        files=["mcp/agentmemory_mcp_proxy.py", "mcp/README.md"],
    )

    # ── Step 6: verify both are retrievable ───────────────────────────────
    step(6, "Verify — retrieve crystals, lessons, memories")

    print(f"\n  [recent crystals]")
    crystals = list_crystals(5)
    for c in crystals[:3]:
        print(
            f"    id={c.get('id', '?')[:24]}  narrative={str(c.get('narrative',''))[:80]}"
        )

    print(f"\n  [recent lessons — GET /agentmemory/lessons]")
    lessons_resp = call(
        "GET", "/agentmemory/lessons", params={"limit": 5, "project": PROJECT}
    )
    for l in lessons_resp.get("lessons", [])[:3]:
        conf = l.get("confidence", "?")
        content = (l.get("content") or "")[:80]
        print(f'    conf={conf}  "{content}"')

    print(f"\n  [memories with matching concepts — GET /agentmemory/memories]")
    memories_resp = call(
        "GET", "/agentmemory/memories", params={"limit": 60, "project": PROJECT}
    )
    all_mems = memories_resp.get("memories", [])
    # find the one we just saved
    saved_id = (memory_resp.get("memory", {}) or {}).get("id")
    saved_mem = next((m for m in all_mems if m.get("id") == saved_id), None)
    if saved_mem:
        print(
            f"  Found saved memory: id={saved_id}  strength={saved_mem.get('strength')}"
        )
        print(f"  content: {(saved_mem.get('content') or '')[:120]}")
    else:
        print(
            f"  Saved memory id={saved_id} — searching in {len(all_mems)} total memories"
        )

    print(f"\n  [DONE] Action completed, crystallized, lesson + memory saved.")
    print("  Future agents will find the lesson via smart-search.")
    print("  The curated memory is reachable via link-following from observation hits.")

    # ── Cleanup ───────────────────────────────────────────────────────────
    # Cancel the test action and governed-delete the demo memory so re-runs
    # don't accumulate one curated memory per run (/remember is immutable and
    # does NOT auto-dedup, unlike /lessons). The lesson above uses stable
    # content, so it auto-strengthens a single lesson instead of multiplying —
    # and lessons have no delete endpoint anyway, so idempotency is the only
    # way to keep them from piling up.
    call(
        "POST",
        "/agentmemory/actions/update",
        body={"actionId": action_id, "status": "cancelled"},
    )
    print(f"\n  [cleanup] Cancelled test action {action_id}")
    saved_mem_id = (memory_resp.get("memory", {}) or {}).get("id")
    if saved_mem_id:
        call(
            "DELETE",
            "/agentmemory/governance/memories",
            body={
                "memoryIds": [saved_mem_id],
                "reason": "scenario_task_complete cleanup",
            },
        )
        print(f"  [cleanup] Governed-deleted demo memory {saved_mem_id}")


if __name__ == "__main__":
    run()
