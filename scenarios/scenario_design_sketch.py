"""
SCENARIO: Design Sketch — Speculative Exploration (promote vs discard)
=====================================================================
Not every plan deserves to live on the real action frontier. When an agent is
*exploring* — "what are the possible ways to port the ComfyUI LTX video node to
a standalone API?" — it wants a scratch space that is grouped, throwaway, and
auto-expiring, so half-baked ideas never pollute the committed work queue.

That scratch space is a **Sketch**: a TTL-bound cluster of *draft actions*. The
other scenarios deal in committed actions (frontier / lease / complete); this is
the only one that models the throwaway design phase before commitment, using the
sketch lifecycle none of them touch:

  1. POST /sketches          — create a sketch (1h TTL, status="active").
  2. POST /sketches/add       — add a candidate idea; the server materializes a
                               *draft action* (createdBy="sketch") under the
                               sketch's actionIds[]. These do NOT surface on the
                               real frontier yet.
  3. GET  /sketches           — review the cluster: actionCount, actionIds, the
                               expiresAt countdown.
  4. POST /sketches/promote   — promote the WINNER: its draft actions detach
                               (sketchId→None) and become first-class frontier
                               work. Returns promotedIds[].
  5. POST /sketches/discard   — discard a REJECTED approach: its draft actions
                               are cancelled in bulk (discardedCount).
  6. POST /sketches/gc        — garbage-collect any sketches past their TTL.

The decision gate — promote one approach, discard the other — is the whole
point: speculative work stays quarantined until you explicitly bless it.

Run:
    python mcp/agentmemory/scenario_design_sketch.py

The scenario cleans up after itself (cancels the actions it promotes) so it
does not leave demo work on the real frontier. Set CLEANUP = False to inspect
the promoted actions on the frontier afterwards.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, step

MARKER = "[sketch-demo]"


def create_sketch(title: str, description: str) -> str | None:
    """
    Create a speculative design sketch (a TTL-bound action cluster).

    POST /agentmemory/sketches
    body: {title, description, project}   → mem::sketch-create

    Returns {sketch: {id, actionIds: [], expiresAt, status: "active", ...}}.
    The default TTL is ~1 hour — sketches are meant to be short-lived scratch.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/sketches  \"{title}\"")
    resp = call("POST", "/agentmemory/sketches", body={
        "title": f"{MARKER} {title}",
        "description": description,
        "project": PROJECT,
    })
    sk = resp.get("sketch", resp)
    sid = sk.get("id")
    print(f"    sketch id={sid}  status={sk.get('status')}  expiresAt={(sk.get('expiresAt') or '')[:19]}")
    return sid


def add_idea(sketch_id: str, idea_title: str) -> str | None:
    """
    Add one candidate idea to a sketch — materializes a draft action.

    POST /agentmemory/sketches/add
    body: {sketchId, title}   → mem::sketch-add

    Server-side this CREATES a new action (createdBy="sketch") and appends it to
    the sketch's actionIds[]. The draft action is NOT on the real frontier — it
    is quarantined under the sketch until the sketch is promoted.
    """
    resp = call("POST", "/agentmemory/sketches/add", body={
        "sketchId": sketch_id,
        "title": idea_title,
    })
    act = resp.get("action", resp)
    aid = act.get("id")
    print(f"      + idea \"{idea_title[:46]}\"  → draft action {aid}  (status={act.get('status')})")
    return aid


def list_sketches() -> list[dict]:
    """
    Review every active sketch and its candidate cluster.

    GET /agentmemory/sketches?project=   → mem::sketch-list

    Each listed sketch reports actionCount + actionIds + expiresAt — the review
    surface before you decide promote vs discard.
    """
    resp = call("GET", "/agentmemory/sketches", params={"project": PROJECT})
    sketches = resp.get("sketches", [])
    mine = [s for s in sketches if MARKER in (s.get("title") or "")]
    print(f"\n  [active sketches]  ({len(sketches)} total, {len(mine)} from this demo)")
    for s in mine:
        title = (s.get("title") or "").replace(MARKER, "").strip()[:50]
        print(f"    {s.get('id')}  status={s.get('status'):9} "
              f"actions={s.get('actionCount', len(s.get('actionIds', [])))}  "
              f"expires={(s.get('expiresAt') or '')[:19]}")
        print(f"        \"{title}\"")
    return mine


def promote_sketch(sketch_id: str) -> list[str]:
    """
    Promote the winning sketch — its draft actions become real frontier work.

    POST /agentmemory/sketches/promote
    body: {sketchId, project}   → mem::sketch-promote

    Returns {promotedIds: [...]}. Each promoted action detaches from the sketch
    (sketchId → None) and is now a committed action eligible for the frontier /
    lease flow. This is the commitment gate.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/sketches/promote  sketchId={sketch_id}")
    resp = call("POST", "/agentmemory/sketches/promote", body={
        "sketchId": sketch_id, "project": PROJECT,
    })
    promoted = resp.get("promotedIds", [])
    print(f"    promoted {len(promoted)} draft action(s) onto the real frontier:")
    for aid in promoted:
        print(f"      → {aid}")
    return promoted


def discard_sketch(sketch_id: str) -> int:
    """
    Discard a rejected sketch — bulk-cancel its draft actions.

    POST /agentmemory/sketches/discard
    body: {sketchId}   → mem::sketch-discard

    Returns {discardedCount}. The rejected approach's draft actions are
    cancelled in one shot; nothing leaks onto the frontier.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/sketches/discard  sketchId={sketch_id}")
    resp = call("POST", "/agentmemory/sketches/discard", body={"sketchId": sketch_id})
    n = resp.get("discardedCount", 0)
    print(f"    discarded {n} draft action(s) — rejected approach left no residue")
    return n


def gc_sketches() -> dict:
    """
    Garbage-collect sketches past their TTL.

    POST /agentmemory/sketches/gc   → mem::sketch-gc

    Returns {collected}. Housekeeping: abandoned exploration that nobody
    promoted or discarded expires and gets swept here.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/sketches/gc")
    resp = call("POST", "/agentmemory/sketches/gc", body={})
    print(f"    collected {resp.get('collected', 0)} expired sketch(es)")
    return resp


def cancel_actions(action_ids: list[str]) -> None:
    """
    Cancel actions (cleanup) so the demo leaves nothing on the real frontier.

    POST /agentmemory/actions/update  body: {actionId, status: "cancelled"}
    """
    for aid in action_ids:
        call("POST", "/agentmemory/actions/update", body={"actionId": aid, "status": "cancelled"})
    if action_ids:
        print(f"    cancelled {len(action_ids)} promoted demo action(s): {action_ids}")


def run(cleanup: bool = True) -> None:
    banner("Design Sketch — Speculative Exploration")
    print("""
  Story: how should we port the ComfyUI LTX video node to a standalone API?
  We explore two approaches as throwaway sketches, review the candidates, then
  PROMOTE the winner (diffusers) onto the real frontier and DISCARD the loser
  (raw-torch) — speculative work stays quarantined until explicitly blessed.
  """)
    check_health()

    promoted_ids: list[str] = []

    # ── Step 1: sketch A — the approach we'll keep ───────────────────────────
    step(1, "Create sketch A — 'LTX standalone port via diffusers'")
    sk_a = create_sketch(
        "LTX standalone port via diffusers",
        "Wrap LTX in a diffusers pipeline; reuse scheduler + VAE plumbing.",
    )

    # ── Step 2: populate sketch A with candidate ideas ───────────────────────
    step(2, "Add candidate ideas to sketch A (draft actions, off-frontier)")
    print("""
  Each /sketches/add creates a DRAFT action under the sketch. Drafts are not on
  the frontier — memory_next would not hand them to a worker yet.
  """)
    if sk_a:
        add_idea(sk_a, "Port LTXVideoTransformer3D weights to diffusers format")
        add_idea(sk_a, "Adapt the LTX scheduler to diffusers SchedulerMixin")
        add_idea(sk_a, "Write a smoke test: 17-frame 512x512 render")

    # ── Step 3: review the cluster ───────────────────────────────────────────
    step(3, "Review sketches before deciding")
    list_sketches()

    # ── Step 4: sketch B — the approach we'll reject ─────────────────────────
    step(4, "Create sketch B — 'LTX standalone port via raw torch' (rival)")
    sk_b = create_sketch(
        "LTX standalone port via raw torch",
        "Reimplement the sampling loop by hand in raw torch — more control, more code.",
    )
    if sk_b:
        add_idea(sk_b, "Hand-roll the denoising loop in raw torch")
        add_idea(sk_b, "Manually manage CUDA memory for the VAE decode")

    # ── Step 5: decision gate — promote the winner ───────────────────────────
    step(5, "Decision: PROMOTE sketch A (diffusers wins — less custom code)")
    print("""
  Promotion is the commitment gate: draft actions detach from the sketch and
  become real frontier work, eligible for the lease/complete flow.
  """)
    if sk_a:
        promoted_ids = promote_sketch(sk_a)

    # ── Step 6: decision gate — discard the loser ────────────────────────────
    step(6, "Decision: DISCARD sketch B (raw-torch rejected — too much surface)")
    if sk_b:
        discard_sketch(sk_b)

    # ── Step 7: gc ───────────────────────────────────────────────────────────
    step(7, "Garbage-collect expired sketches")
    gc_sketches()

    # ── Cleanup ──────────────────────────────────────────────────────────────
    step(8, "Cleanup — keep the demo off the real frontier")
    if cleanup:
        cancel_actions(promoted_ids)
        # also discard sketch A so re-runs stay clean (its actions are cancelled)
        if sk_a:
            discard_sketch(sk_a)
    else:
        print(f"  [cleanup disabled — promoted actions left on frontier: {promoted_ids}]")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  DESIGN SKETCH SUMMARY")
    print(f"{'─' * 72}")
    print("  sketches(create)  → TTL-bound scratch cluster, status=active")
    print("  sketches/add      → candidate ideas as DRAFT actions (off-frontier)")
    print("  sketches(list)    → review actionCount / expiresAt before deciding")
    print("  sketches/promote  → winner's drafts become committed frontier work")
    print("  sketches/discard  → loser's drafts bulk-cancelled, no residue")
    print("  sketches/gc       → sweep abandoned, expired exploration")
    print(f"{'═' * 72}")
    print("  Contrast with scenario_task_init/complete: those run COMMITTED work;")
    print("  this one is the quarantined design phase that precedes commitment.")


if __name__ == "__main__":
    CLEANUP = True
    run(cleanup=CLEANUP)
