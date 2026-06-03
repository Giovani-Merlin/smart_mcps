"""
SCENARIO: Blocked Handoff Loop
================================
An agent cannot proceed (waiting for CI, human approval, external dependency).
Instead of spinning in a loop, it:
  1. Creates a sentinel that listens for the external trigger
  2. Blocks its current action (waiting for sentinel)
  3. Emits a signal to notify collaborators (human or other agents)
  4. Picks up a different unblocked task from the frontier while waiting

Key endpoints:
  MCP  memory_sentinel_create   → create gated sentinel (mem::sentinel-create)
                                   args: {name, type, gatedActionIds?, expiresInMs?}
                                   type: "webhook" | "time" | "condition"
  MCP  memory_sentinel_trigger  → manually trigger a sentinel (mem::sentinel-trigger)
                                   args: {sentinelId, result?}
  MCP  memory_signal_emit       → broadcast event to signal subscribers (mem::signal-emit)
                                   args: {event (string), payload? (JSON string)}
  POST /agentmemory/actions/update  → set status="blocked"
  GET  /agentmemory/frontier        → pick next unblocked task after blocking current

Sentinel types:
  "webhook"    — waits for a specific HTTP callback (e.g. GitHub Actions success)
  "time"       — waits until a timestamp or duration (expiresInMs)
  "condition"  — waits for a logical condition to be asserted

Sentinel gating:
  gatedActionIds  — action IDs that are auto-unblocked when the sentinel fires
  When the sentinel triggers, all gated actions move from "blocked" → "pending"
  and reappear on the frontier.

Signal events:
  Free-form string events (e.g. "ci.passed", "pr.reviewed", "deploy.done")
  payload: any JSON-serializable data as a string (JSON-encoded)
  Useful for: notifying humans, coordinating between parallel agents,
  marking external milestones.

Run:
    python mcp/agentmemory/scenario_blocked_handoff.py
"""

from __future__ import annotations

import json
import sys
import uuid
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


def create_action(title: str, description: str = "") -> str | None:
    """
    Create an action. Returns action_id or None.

    POST /agentmemory/actions
    Full body schema:
        title (required)
        description
        priority: 1-10, 10=highest
        createdBy: agent identity string
        parentId: parent action ID for hierarchy; None = top-level action
        tags: list of string tags
        project: project scope slug
        sourceMemoryIds: memory IDs that inform / motivate this task
        sourceObservationIds: episodic obs IDs that led to creating this task
        edges: dependency DAG edges — added when creating dependent tasks
               format: [{type: str, targetActionId: str}]
               edge types: requires | unlocks | spawned_by | gated_by | conflicts_with
               "requires" A→B: when B.status=done → propagateCompletion auto-unblocks A
    """
    resp = call(
        "POST",
        "/agentmemory/actions",
        body={
            "title": title,
            "description": description,
            "priority": 7,
            "createdBy": "gionodes/agent",  # agent creating this task
            "parentId": None,  # None = top-level action
            "tags": ["type:test", "scenario:blocked_handoff"],
            "project": PROJECT,
            "sourceMemoryIds": [],  # memory IDs informing this task
            "sourceObservationIds": [],  # obs IDs that led to creating this task
            "edges": [],  # leave empty — edges added when creating dependent tasks
            # format: [{type: "requires"|"unlocks", targetActionId: "..."}]
        },
    )
    action = resp.get("action", resp)
    action_id = action.get("id")
    print(f'  Created action: id={action_id}  "{title[:60]}"')
    return action_id


def create_sentinel(
    name: str, sentinel_type: str, linked_ids: list[str], expires_ms: int = 60000
) -> dict:
    """
    Create a sentinel that gates blocked actions until an external event fires.

    MCP function: memory_sentinel_create → mem::sentinel-create
    Endpoint: POST /agentmemory/mcp/call
    args:
        name:            human-readable sentinel name
        type:            "webhook" | "timer" | "threshold" | "pattern" | "approval" | "custom"
        linkedActionIds: comma-sep action IDs to auto-unblock on trigger
        expiresInMs:     sentinel lifetime in ms (default ~1 hour; set low for demos)

    Sentinel-type configs (verified against v0.9.24):
      - "approval" / "custom"  → need NO extra config (used here).
      - "webhook"   → requires config.path (an HTTP callback path).
      - "timer"     → requires config.durationMs.
      - "threshold" → requires config.{metric, operator, value}.
    The gating param is `linkedActionIds` (older docs called it gatedActionIds —
    that key is ignored by this build, leaving actions ungated).

    WHY: rather than polling, the agent creates a sentinel and moves on.
    When the external event fires (CI passes, approval lands), all linked
    actions automatically return to the frontier.
    """
    linked_str = ",".join(linked_ids)
    print(f"\n  mcp_call memory_sentinel_create  name={name}  type={sentinel_type}")
    result = mcp_call(
        "memory_sentinel_create",
        {
            "name": name,
            "type": sentinel_type,
            "linkedActionIds": linked_str,
            "expiresInMs": str(expires_ms),
        },
    )
    return result


def trigger_sentinel(sentinel_id: str, result_data: dict | None = None) -> dict:
    """
    Manually trigger a sentinel (simulate the external event arriving).

    MCP function: memory_sentinel_trigger → mem::sentinel-trigger
    Endpoint: POST /agentmemory/mcp/call
    args: {sentinelId, result? (JSON string)}

    In production: webhooks call this automatically.
    In tests/demos: call manually to simulate the external trigger.
    On trigger: all gatedActionIds are unblocked and reappear on frontier.
    """
    print(f"\n  mcp_call memory_sentinel_trigger  sentinelId={sentinel_id}")
    args: dict[str, str] = {"sentinelId": sentinel_id}
    if result_data:
        args["result"] = json.dumps(result_data)
    return mcp_call("memory_sentinel_trigger", args)


def send_signal(
    from_agent: str, content: str, sig_type: str = "info", to: str | None = None
) -> dict:
    """
    Send a signal — a cross-agent message (broadcast or directed).

    MCP function: memory_signal_send → mem::signal-send
    Endpoint: POST /agentmemory/mcp/call
    args (required): {from, content}; optional {to, type, replyTo}
        type: "info" | "request" | "response" | "alert" | "handoff"
        to:   target agentId for a directed message; omit to broadcast.

    NOTE (v0.9.24): the signal model is message-based (from/to/content/type),
    NOT the older event/payload shape. The tool is `memory_signal_send`
    (`memory_signal_emit` does not exist in this build).

    WHY: signals decouple producers from consumers. An agent broadcasts a
    "handoff" message and any reader (human, CI agent, review agent) picks it
    up via memory_signal_read without the sender knowing who listens.
    """
    print(
        f"\n  mcp_call memory_signal_send  from={from_agent}  type={sig_type}  to={to or '(broadcast)'}"
    )
    args = {"from": from_agent, "content": content, "type": sig_type}
    if to:
        args["to"] = to
    return mcp_call("memory_signal_send", args)


def read_signals(agent_id: str, limit: int = 5) -> list[dict]:
    """
    Read messages addressed to an agent (and mark them delivered).

    MCP function: memory_signal_read → mem::signal-read
    args (required): {agentId}; optional {limit, threadId, unreadOnly}

    Demonstrates the consumer half of the handoff: the agent that picks up the
    work reads the inbox to learn why the action was blocked and what to wait on.
    """
    print(f"\n  mcp_call memory_signal_read  agentId={agent_id}  limit={limit}")
    result = mcp_call("memory_signal_read", {"agentId": agent_id, "limit": str(limit)})
    signals = (
        result
        if isinstance(result, list)
        else result.get("signals", result.get("messages", []))
    )
    return signals if isinstance(signals, list) else []


def block_action(action_id: str) -> dict:
    """
    Mark an action as blocked.

    POST /agentmemory/actions/update
    body: {actionId, status: "blocked"}

    A blocked action does NOT appear on the frontier until unblocked.
    Unblocking happens either:
      - Automatically when a sentinel fires (mem::sentinel-trigger)
      - Manually via status update to "pending"
    """
    print(f"\n  POST {BASE_URL}/agentmemory/actions/update  status=blocked")
    resp = call(
        "POST",
        "/agentmemory/actions/update",
        body={
            "actionId": action_id,
            "status": "blocked",
        },
    )
    action = resp.get("action", resp)
    print(f"  status is now: {action.get('status', '?')}")
    return resp


def run() -> None:
    banner("Blocked Handoff Loop")
    print(
        """
  Pattern: agent opens a PR → CI is pending → instead of spinning,
  create a sentinel to gate the action, block it, emit a signal,
  then pick up a different unblocked task from the frontier.
  """
    )
    check_health()

    uid = uuid.uuid4().hex[:8]

    # ── Step 1: create the task that will get blocked ──────────────────────
    step(1, "Create the action that will be blocked (waiting for CI)")
    blocked_id = create_action(
        title=f"scenario-blocked-{uid}: Deploy pose3d model to staging",
        description="Requires CI pipeline to pass first. PR #42 opened.",
    )
    if not blocked_id:
        print("  [ERROR] Could not create action")
        return

    # ── Step 2: create sentinel to gate the blocked action ────────────────
    step(2, "Create sentinel — gate action until CI/approval lands")
    print(
        """
  An "approval" sentinel gates the action until an external approval fires
  (it needs no extra config, unlike webhook/timer/threshold types).
  In production: a GitHub Actions success or a human approval fires the trigger.
  linkedActionIds: these action IDs auto-unblock when the sentinel fires.
  expiresInMs: 60000 = sentinel expires in 60 seconds (short for demo).
  """
    )
    sentinel_result = create_sentinel(
        name=f"ci-pass-sentinel-{uid}",
        sentinel_type="approval",
        linked_ids=[blocked_id],
        expires_ms=60_000,
    )
    pp(sentinel_result, "sentinel created")
    sentinel_id = None
    if isinstance(sentinel_result, dict):
        sentinel_id = (
            sentinel_result.get("sentinel", {}).get("id")
            or sentinel_result.get("id")
            or sentinel_result.get("sentinelId")
        )
    print(f"\n  sentinel_id: {sentinel_id}")

    # ── Step 3: block the action ───────────────────────────────────────────
    step(3, "Block the action (status=blocked — removed from frontier)")
    print(
        """
  A blocked action disappears from the frontier.
  It reappears when: (a) sentinel fires, or (b) manually set back to pending.
  """
    )
    block_action(blocked_id)

    # ── Step 4: emit a signal to notify collaborators ─────────────────────
    step(4, "Send a handoff signal — hand the blocked work to a reviewer agent")
    print(
        """
  The deployer agent sends a directed "handoff" message to a reviewer agent,
  then the reviewer reads its inbox (memory_signal_read) to learn what's blocked
  and why. This is the cross-agent message half of the coordination model.
  """
    )
    from_agent = "gionodes/deployer"
    to_agent = "gionodes/reviewer"
    signal_result = send_signal(
        from_agent=from_agent,
        to=to_agent,
        sig_type="handoff",
        content=(
            f"Deploy action {blocked_id} (PR #42) is blocked on CI; gated by "
            f"sentinel {sentinel_id}. Please watch and approve when green."
        ),
    )
    pp(signal_result, "signal sent")

    # The reviewer picks up the handoff from its inbox.
    inbox = read_signals(to_agent, limit=5)
    print(f"\n  [{to_agent} inbox]  ({len(inbox)} message(s))")
    for m in inbox[:3]:
        print(
            f"    [{m.get('type','?'):8}] from={m.get('from','?')}  \"{(m.get('content') or '')[:60]}\""
        )

    # ── Step 5: pick up a different task ──────────────────────────────────
    step(5, "Pick next unblocked task from frontier (non-blocked work)")
    print(
        """
  The agent doesn't idle. It calls GET /frontier to find the next
  unblocked, highest-priority task and starts that instead.
  The blocked deploy action will reappear automatically when CI passes.
  """
    )
    frontier_resp = call(
        "GET", "/agentmemory/frontier", params={"limit": 5, "project": PROJECT}
    )
    frontier = frontier_resp.get("frontier", [])
    print_frontier(frontier)

    # Filter out the blocked action we just created
    available = [e for e in frontier if e.get("action", {}).get("id") != blocked_id]
    if available:
        next_action = available[0].get("action", {})
        print(f"\n  Next task picked up: \"{next_action.get('title', '?')}\"")
        print(f"  Agent starts working on this while CI runs in background.")
    else:
        print("\n  [no other unblocked actions available]")

    # ── Step 6: simulate CI passing — trigger the sentinel ────────────────
    step(6, "Simulate CI passing — trigger sentinel to unblock deploy action")
    print(
        """
  In production this is fired by a GitHub webhook.
  Here we manually trigger to demonstrate that gated actions are unblocked.
  """
    )
    if sentinel_id:
        trigger_result = trigger_sentinel(
            sentinel_id, result_data={"ci_status": "passed", "pr": "42"}
        )
        pp(trigger_result, "sentinel trigger result")

        # Verify the action is back on frontier
        print(f"\n  Checking frontier — deploy action should be unblocked now:")
        frontier_after = call(
            "GET", "/agentmemory/frontier", params={"limit": 10, "project": PROJECT}
        )
        frontier_list = frontier_after.get("frontier", [])
        found = any(e.get("action", {}).get("id") == blocked_id for e in frontier_list)
        print(f"  Action {blocked_id} back on frontier: {found}")
        if not found:
            print("  (may take a moment for the engine to process the trigger)")
    else:
        print("  [sentinel_id not available — skipping trigger]")

    # ── Cleanup ───────────────────────────────────────────────────────────
    # Cancel the test action AND the sentinel so the demo leaves no residue.
    # Sentinels are not auto-deleted on trigger, and a dangling sentinel would
    # be flagged by diagnose — POST /sentinels/cancel removes it cleanly.
    call(
        "POST",
        "/agentmemory/actions/update",
        body={"actionId": blocked_id, "status": "cancelled"},
    )
    print(f"\n  [cleanup] Cancelled test action {blocked_id}")
    if sentinel_id:
        call("POST", "/agentmemory/sentinels/cancel", body={"sentinelId": sentinel_id})
        print(f"  [cleanup] Cancelled sentinel {sentinel_id}")
    # Note: emitted signals expire on their own (no delete endpoint); the
    # "pr.opened" signal above is short-lived and needs no cleanup.


if __name__ == "__main__":
    run()
