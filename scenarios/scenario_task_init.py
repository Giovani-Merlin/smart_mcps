"""
SCENARIO: Task Initialization with Session Context
====================================================
Agent session starts. Before executing any code, it needs:
  1. Get next unblocked task from frontier
  2. Claim the task (set status=active)
  3. Recall lessons/gotchas about the task area
  4. Get file-scoped history for target files
  5. Retrieve action context via smart-search

Pattern: Minimal payloads throughout. Skip unnecessary optional fields.

Key endpoints (MINIMAL PAYLOADS):
  GET /frontier               MINIMAL: {limit, project}
  POST /actions/update        MINIMAL: {actionId, status}
  MCP memory_lesson_recall    MINIMAL: {query, project, minConfidence, limit}
  MCP memory_file_history     MINIMAL: {files}
  POST /smart-search          MINIMAL: {query, project, limit}

Run:
    python mcp/agentmemory/scenario_task_init.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import (
    BASE_URL,
    PROJECT,
    banner,
    call,
    check_health,
    mcp_call,
    pp,
    print_frontier,
    step,
)


def get_frontier(limit: int = 5) -> list[dict]:
    """
    Fetch the action frontier — highest priority unblocked actions.

    GET /agentmemory/frontier
    MINIMAL PAYLOAD: {limit, project}

    Returns frontier: [{action: {...}, score, leased, blockers}]
    Entries sorted by score descending (priority × urgency).
    """
    print(
        f"\n  [1. GET frontier]  GET /agentmemory/frontier  MINIMAL: {{limit, project}}"
    )
    resp = call(
        "GET", "/agentmemory/frontier", params={"limit": limit, "project": PROJECT}
    )
    frontier = resp.get("frontier", [])
    print(f"  frontier size: {len(frontier)} actions")
    return frontier


def claim_action(action_id: str) -> dict:
    """
    Claim an action by setting status=active.

    POST /agentmemory/actions/update
    MINIMAL PAYLOAD: {actionId, status}

    Valid statuses: pending | active | done | blocked | cancelled
    """
    print(
        f"\n  [2. claim]  POST /agentmemory/actions/update  MINIMAL: {{actionId, status}}"
    )
    resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": action_id,
            "status": "active",
        },
    )
    action = resp.get("action", resp)
    print(f"  status: {action.get('status')}")
    return resp


def recall_lessons(query: str) -> list[dict]:
    """
    Recall lessons (procedural rules) about the task area.

    MCP memory_lesson_recall
    MINIMAL PAYLOAD: {query, project, minConfidence, limit} (all strings)

    Returns lessons scored by confidence × recency. These are procedural rules
    ("always check X", "Y is outdated", etc.). Higher confidence = more reliable.
    """
    print(
        f"\n  [3. lessons]  MCP memory_lesson_recall  MINIMAL: {{query, project, minConfidence, limit}}"
    )
    result = mcp_call(
        "memory_lesson_recall",
        {
            "query": query,
            "project": PROJECT,
            "minConfidence": "0.3",
            "limit": "5",
        },
    )
    lessons = result if isinstance(result, list) else result.get("lessons", [])
    print(f"  lessons: {len(lessons)}")
    for l in lessons[:3]:
        conf = l.get("confidence", "?")
        content = (l.get("content") or "")[:70]
        print(f'    conf={conf}  "{content}"')
    return lessons


def get_file_history(files: list[str]) -> dict:
    """
    Get episodic history for specific files.

    MCP memory_file_history
    MINIMAL PAYLOAD: {files} (comma-separated paths)

    Returns formatted narrative of prior agent observations about these files.
    Useful to see unresolved technical debt from prior sessions.
    """
    print(f"\n  [4. file history]  MCP memory_file_history  MINIMAL: {{files}}")
    files_str = ",".join(files[:2])  # limit to 2 files for demo
    result = mcp_call("memory_file_history", {"files": files_str})
    context = result.get("context") if isinstance(result, dict) else str(result)
    if context and context != "No history found.":
        for line in str(context)[:300].splitlines()[:4]:
            print(f"    {line}")
    else:
        print("  (no history found)")
    return result


def run() -> None:
    banner("Task Initialization — Minimal Payloads")
    print(
        """
  Agent session starts. Steps:
  1. Get next task from frontier (GET /frontier)
  2. Claim it (POST /actions/update with status=active)
  3. Recall lessons (MCP memory_lesson_recall)
  4. Check file history (MCP memory_file_history)
  5. Retrieve action context (POST /smart-search)
  """
    )
    check_health()

    step(1, "Get frontier")
    frontier = get_frontier(limit=5)

    if not frontier:
        print("\n  [empty — create actions first]")
        return

    print_frontier(frontier)

    # Pick first unblocked action
    target = next((e for e in frontier if not e.get("leased")), frontier[0])
    action = target.get("action", {})
    action_id = action.get("id")
    title = action.get("title", "")
    description = action.get("description", "")

    print(f"\n  selected:  {title[:50]}")
    if not action_id:
        return

    step(2, "Claim the action")
    claim_action(action_id)

    step(3, "Recall lessons")
    lesson_query = f"{title} {description[:60]}".strip()
    recall_lessons(lesson_query)

    step(4, "Get file history")
    candidate_files = ["mcp/agentmemory_mcp_proxy.py", "tests/mcp/test_agentmemory.py"]
    get_file_history(candidate_files)

    step(5, "Retrieve context via smart-search")
    print(
        f"\n  [5. context]  POST /agentmemory/smart-search  MINIMAL: {{query, project, limit}}"
    )
    resp = call(
        "POST",
        "/agentmemory/smart-search",
        body={
            "query": title,
            "project": PROJECT,
            "limit": 5,
        },
    )
    results = resp.get("results", [])
    print(f"  observations: {len(results)}")
    for r in results[:2]:
        print(f"    [{r.get('type', '?'):12}] {(r.get('title') or '')[:60]}")

    print(f"\n{'═' * 72}")
    print("  SUMMARY — TASK INIT WITH MINIMAL PAYLOADS")
    print(f"{'─' * 72}")
    print("  1. frontier:        {limit, project}")
    print("  2. claim:           {actionId, status}")
    print("  3. lessons:         {query, project, minConfidence, limit} (strings)")
    print("  4. file_history:    {files}")
    print("  5. smart-search:    {query, project, limit}")
    print(f"{'═' * 72}")

    # Cleanup
    call(
        "POST",
        "/agentmemory/actions/update",
        body={"actionId": action_id, "status": "pending"},
    )
    print("  [cleanup] Reset status to pending")


if __name__ == "__main__":
    run()
