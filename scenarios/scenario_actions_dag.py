"""
SCENARIO: Actions DAG — Orchestrator Send → Worker Execute → Closure
======================================================================
Models a 3-node directed acyclic graph of tasks across 3 actor roles:

  dag_setup()              — ORCHESTRATOR creates parent + 2 children with
                             edges ({type: "requires", targetActionId}) and
                             rich metadata (priority, tags, createdBy, description).

  worker_receive_and_update() — WORKER queries frontier (only unblocked tasks
                             appear), acquires a lease, marks active, updates
                             description with progress notes.

  orchestrator_closure()   — ORCHESTRATOR marks the parent done (with result),
                             verifies the child cascades onto the frontier,
                             saves a durable memory of what was learned.

Key insight (from NotebookLM): The frontier is NOT the full DAG — it's only
the READY tasks (all `requires` edges point to done actions). Agents must call
GET /agentmemory/actions and cross-reference edge data client-side to see the
full graph. The frontier blockers[] field shows remaining unsatisfied deps.

DAG shape used in this scenario:
    [parent: setup auth base]  ←── required by ──→  [child: jwt middleware]
                                                            │
                                                     required by
                                                            ▼
                                                  [grandchild: write tests]

At start: only parent is on frontier (child and grandchild are blocked).
After parent done: child appears on frontier. After child done: grandchild.

NotebookLM Findings (queried before implementing this scenario):
  - POST /actions supports: title, description, priority, project, tags,
    createdBy, parentId, edges: [{type, targetActionId}],
    sourceObservationIds, sourceMemoryIds, metadata
  - POST /agentmemory/lease: {actionId, agentId, operation: "acquire", ttlMs}
    prevents agent collision. Release on completion.
  - FrontierItem.blockers[] lists unsatisfied dep IDs for each action.
  - Closure pattern: mark done (with result) → save memory with concepts.
    Including sourceObservationIds in the memory maintains provenance for
    memory_verify.

Run:
    python mcp/agentmemory/scenario_actions_dag.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import TypedDict

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from _client import (  # noqa: E402
    PROJECT,
    banner,
    call,
    check_health,
    print_frontier,
    step,
)

AGENT_ID = "worker-dag-demo"

OK = "✓"
FAIL = "✗"


def _check(label: str, condition: bool, detail: str = "") -> None:
    mark = OK if condition else FAIL
    print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))


class DagIds(TypedDict):
    uid: str
    parent_id: str
    child_id: str
    grandchild_id: str


# ══════════════════════════════════════════════════════════════════════════════
# Role 1 — ORCHESTRATOR SETUP
# ══════════════════════════════════════════════════════════════════════════════


def dag_setup() -> DagIds:
    """
    Orchestrator creates a 3-node DAG with rich metadata and edge declarations.

    POST /agentmemory/actions
    FULL PAYLOAD (beyond minimal):
      {title, description, priority, project, tags, createdBy, parentId, edges}

    edges: [{type: "requires", targetActionId}] declares blocking dependency.
    Valid edge types: "requires" | "unlocks" | "spawned_by" | "gated_by" | "conflicts_with"

    After creation:
      - parent:      no deps    → immediately on frontier
      - child:       requires parent → blocked (not on frontier)
      - grandchild:  requires child  → blocked (not on frontier)
    """
    print("\n  [ORCHESTRATOR]  Building 3-node DAG with edges")
    uid = uuid.uuid4().hex[:8]

    # ── Node 1: Parent (prerequisite, no deps) ─────────────────────────────
    print(
        "\n  [1/3]  POST /agentmemory/actions  "
        "PAYLOAD: {title, description, priority, tags, createdBy}"
    )
    parent_resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": f"dag-{uid}: setup auth base",
            "description": "Configure JWT key rotation, env vars, and signing secrets.",
            "priority": 9,
            "project": PROJECT,
            "tags": ["security", "auth", f"dag-{uid}"],
            "createdBy": "orchestrator-agent",
        },
    )
    parent = parent_resp.get("action", parent_resp)
    parent_id = parent.get("id")
    _check("parent created", bool(parent_id), f"id={parent_id}")

    if not parent_id:
        raise RuntimeError("parent action not created")

    # ── Node 2: Child (requires parent) ────────────────────────────────────
    print(
        "\n  [2/3]  POST /agentmemory/actions  "
        "PAYLOAD: {title, parentId, edges: [{type:'requires', targetActionId}]}"
    )
    child_resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": f"dag-{uid}: implement jwt middleware",
            "description": "Add Bearer token validation. Strip whitespace before decode.",
            "priority": 7,
            "project": PROJECT,
            "tags": ["security", "auth", "middleware", f"dag-{uid}"],
            "createdBy": "orchestrator-agent",
            "parentId": parent_id,
            "edges": [{"type": "requires", "targetActionId": parent_id}],
        },
    )
    child = child_resp.get("action", child_resp)
    child_id = child.get("id")
    _check("child created (requires parent)", bool(child_id), f"id={child_id}")

    # ── Node 3: Grandchild (requires child) ────────────────────────────────
    print(
        "\n  [3/3]  POST /agentmemory/actions  "
        "PAYLOAD: {title, parentId, edges: [{type:'requires', targetActionId}]}"
    )
    grandchild_resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": f"dag-{uid}: write auth integration tests",
            "description": "Unit + integration tests for JWT edge cases (expired, malformed, padded).",
            "priority": 5,
            "project": PROJECT,
            "tags": ["testing", "auth", f"dag-{uid}"],
            "createdBy": "orchestrator-agent",
            "parentId": child_id,
            "edges": [{"type": "requires", "targetActionId": child_id}],
        },
    )
    grandchild = grandchild_resp.get("action", grandchild_resp)
    grandchild_id = grandchild.get("id")
    _check(
        "grandchild created (requires child)",
        bool(grandchild_id),
        f"id={grandchild_id}",
    )

    # ── Verify DAG state: only parent on frontier ──────────────────────────
    print("\n  [verify]  GET /agentmemory/frontier  — only parent should appear")
    frontier = call(
        "GET",
        "/agentmemory/frontier",
        params={"project": PROJECT, "limit": 20},
    )
    frontier_items = frontier.get("frontier", [])
    our_items = [
        e
        for e in frontier_items
        if f"dag-{uid}" in (e.get("action") or {}).get("title", "")
    ]
    our_ids = {(e.get("action") or {}).get("id") for e in our_items}
    _check(
        "parent on frontier",
        parent_id in our_ids,
        f"{len(our_items)} dag-{uid} actions visible",
    )
    _check("child NOT on frontier (blocked)", child_id not in our_ids)
    _check("grandchild NOT on frontier (blocked)", grandchild_id not in our_ids)

    print(f"\n  DAG created:  uid={uid}")
    print(f"    parent    (prio=9, no deps):     {parent_id}")
    print(f"    child     (prio=7, req parent):  {child_id}")
    print(f"    grandchild(prio=5, req child):   {grandchild_id}")

    return DagIds(
        uid=uid,
        parent_id=parent_id,
        child_id=child_id or "",
        grandchild_id=grandchild_id or "",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Role 2 — WORKER: receive from frontier, lease, work, update
# ══════════════════════════════════════════════════════════════════════════════


def worker_receive_and_update(dag_ids: DagIds) -> None:
    """
    Worker queries the frontier, claims the top action, does work, marks active.

    GET  /agentmemory/frontier       discover unblocked tasks
    POST /agentmemory/lease          acquire exclusive lease (prevent collision)
    POST /agentmemory/actions/update mark active + update description with progress

    The lease TTL (ttlMs) is the safety net for crashed workers — expired leases
    can be healed via memory_heal, unblocking the task for re-assignment.
    """
    parent_id = dag_ids["parent_id"]
    uid = dag_ids["uid"]

    print(f"\n  [WORKER]  Querying frontier for dag-{uid}")

    # Step A: Get frontier — find our parent action
    frontier_resp = call(
        "GET",
        "/agentmemory/frontier",
        params={"project": PROJECT, "limit": 10},
    )
    frontier_items = frontier_resp.get("frontier", [])
    our_entry = next(
        (e for e in frontier_items if (e.get("action") or {}).get("id") == parent_id),
        None,
    )

    if our_entry:
        score = our_entry.get("score", "?")
        blockers = our_entry.get("blockers", [])
        print(f"  frontier hit: score={score}  blockers={blockers}")
        _check("parent is on frontier", True)
    else:
        print(f"  [parent {parent_id} not found on frontier]")
        _check("parent is on frontier", False)

    # Step B: Acquire exclusive lease (prevents another agent from claiming it)
    print(
        "\n  [lease]  POST /agentmemory/lease  "
        "PAYLOAD: {actionId, agentId, operation:'acquire', ttlMs}"
    )
    try:
        lease_resp = call(
            "POST",
            "/agentmemory/lease",
            body={
                "actionId": parent_id,
                "agentId": AGENT_ID,
                "operation": "acquire",
                "ttlMs": 300000,  # 5 min TTL
            },
        )
        lease = lease_resp.get("lease", lease_resp)
        lease_id = lease.get("id")
        _check("lease acquired", bool(lease_id), f"leaseId={lease_id}")
    except httpx.HTTPStatusError as e:
        print(
            f"  [lease failed: HTTP {e.response.status_code} — may already be leased]"
        )

    # Step C: Mark active + update description with progress notes
    print(
        "\n  [active]  POST /agentmemory/actions/update  "
        "PAYLOAD: {actionId, status:'active', description}"
    )
    update_resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": parent_id,
            "status": "active",
            "description": "Configuring JWT key rotation. Fetching secrets from vault...",
        },
    )
    updated = update_resp.get("action", update_resp)
    _check("status=active", updated.get("status") == "active", updated.get("status"))
    print(f"  description updated with progress notes")


# ══════════════════════════════════════════════════════════════════════════════
# Role 3 — ORCHESTRATOR CLOSURE
# ══════════════════════════════════════════════════════════════════════════════


def orchestrator_closure(dag_ids: DagIds) -> None:
    """
    Mark parent done → verify child cascades to frontier → save durable memory.

    POST /agentmemory/actions/update  {actionId, status:'done', result}
    GET  /agentmemory/frontier        verify child now unblocked
    POST /agentmemory/remember        save what was learned (with concepts)

    The 'result' field captures the outcome for future reference.
    Saving a memory closes the loop: action → episodic work → semantic fact.

    Optionally provide sourceObservationIds to maintain provenance for
    memory_verify (lets agents trace back why we believe this fact).
    """
    parent_id = dag_ids["parent_id"]
    child_id = dag_ids["child_id"]
    uid = dag_ids["uid"]

    # Step A: Mark parent done with result
    print(
        "\n  [CLOSURE 1/3]  POST /agentmemory/actions/update  "
        "PAYLOAD: {actionId, status:'done', result}"
    )
    done_resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": parent_id,
            "status": "done",
            "result": "JWT key rotation configured. Secrets stored in vault/jwt-signing.pem.",
        },
    )
    done_action = done_resp.get("action", done_resp)
    _check(
        "parent status=done",
        done_action.get("status") == "done",
        done_action.get("status"),
    )

    # Step B: Verify child cascades to frontier (blockers resolved)
    print(
        "\n  [CLOSURE 2/3]  GET /agentmemory/frontier  "
        "— verify child is now unblocked"
    )
    frontier2 = call(
        "GET",
        "/agentmemory/frontier",
        params={"project": PROJECT, "limit": 20},
    )
    items2 = frontier2.get("frontier", [])
    our_items2 = [
        e for e in items2 if f"dag-{uid}" in (e.get("action") or {}).get("title", "")
    ]
    our_ids2 = {(e.get("action") or {}).get("id") for e in our_items2}

    _check("parent no longer on frontier (done)", parent_id not in our_ids2)
    _check("child now on frontier (unblocked)", child_id in our_ids2)

    if our_items2:
        print(f"\n  Current frontier for dag-{uid}:")
        print_frontier(our_items2)

    # Step C: Save durable memory of what was learned
    print(
        "\n  [CLOSURE 3/3]  POST /agentmemory/remember  "
        "PAYLOAD: {content, type, project, concepts}"
    )
    mem_resp = call(
        "POST",
        "/agentmemory/remember",
        body={
            "content": (
                f"[dag-{uid}] JWT key rotation requires vault/jwt-signing.pem to be "
                "pre-provisioned. Auth middleware reads it at startup; missing file causes "
                "silent 401 cascade. Always verify vault path before deploying middleware."
            ),
            "type": "bug",
            "project": PROJECT,
            "concepts": ["jwt", "key-rotation", "vault", "auth-middleware", "401"],
            "files": ["src/auth/jwt.py", "src/auth/middleware.py"],
            # sourceObservationIds not provided (no real obs in this demo)
            # in a real session, include obs IDs from the tool calls above
        },
    )
    memory = mem_resp.get("memory", mem_resp)
    mem_id = memory.get("id")
    _check("memory saved", bool(mem_id), f"id={mem_id}")
    print(f"  concepts: {memory.get('concepts', [])[:5]}")
    print(f"  type: {memory.get('type')}")


# ══════════════════════════════════════════════════════════════════════════════
# Cleanup
# ══════════════════════════════════════════════════════════════════════════════


def _cleanup(dag_ids: DagIds) -> None:
    """Cancel all DAG actions to leave system clean."""
    for key in ("parent_id", "child_id", "grandchild_id"):
        action_id = dag_ids.get(key)
        if action_id:
            try:
                call(
                    "POST",
                    "/agentmemory/actions/update",
                    body={"actionId": action_id, "status": "cancelled"},
                )
            except httpx.HTTPStatusError:
                pass
    print(f"  [cleanup] All 3 dag-{dag_ids['uid']} actions cancelled")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def run() -> None:
    banner("Actions DAG — Orchestrator → Worker → Closure")
    print(
        """
  Demonstrates a 3-role actions DAG workflow with full metadata and edge declarations.

  DAG shape:
    [parent: setup auth base]  ──requires──▶  [child: jwt middleware]
                                                      │
                                                  requires
                                                      ▼
                                         [grandchild: write tests]

  Role 1 — ORCHESTRATOR SETUP:  creates nodes, declares edges, verifies frontier
  Role 2 — WORKER RECEIVE:      claims frontier action, leases it, marks active
  Role 3 — ORCHESTRATOR CLOSE:  marks done, verifies cascade, saves memory
  """
    )
    check_health()

    step(1, "Orchestrator: Build DAG with edges and rich metadata")
    dag_ids = dag_setup()

    step(2, "Worker: Receive from frontier, lease, mark active")
    worker_receive_and_update(dag_ids)

    step(3, "Orchestrator: Close task — done + cascade + memory save")
    orchestrator_closure(dag_ids)

    print(f"\n{'═' * 72}")
    print("  SUMMARY — ACTIONS DAG PAYLOADS")
    print(f"{'─' * 72}")
    print(
        "  Create (rich):   {title, description, priority, tags, createdBy, parentId, edges}"
    )
    print("  Create (edge):   edges: [{type:'requires', targetActionId}]")
    print("  Frontier:        {project, limit}  → only unblocked actions")
    print("  Lease:           {actionId, agentId, operation:'acquire', ttlMs}")
    print("  Update active:   {actionId, status:'active', description}")
    print("  Close:           {actionId, status:'done', result}")
    print("  Memory save:     {content, type, project, concepts, files}")
    print(f"{'─' * 72}")
    print("  Edge types:  requires | unlocks | spawned_by | gated_by | conflicts_with")
    print("  Cascade:     parent done → child unblocked → child appears on frontier")
    print(f"{'═' * 72}")

    _cleanup(dag_ids)


if __name__ == "__main__":
    run()
