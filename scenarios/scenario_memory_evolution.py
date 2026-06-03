"""
SCENARIO: Memory Evolution & Provenance Loop
=============================================
Curated memories are *immutable* once saved (POST /remember). But knowledge
goes stale: an architectural decision recorded six months ago ("pose3d runs
through the vendored rtmpose3d package") gets overturned by a refactor ("pose3d
now calls the standalone rtmlib API"). You don't delete the old record — you
*evolve* it, creating a new version that supersedes the old one while keeping
the full lineage for provenance.

This scenario walks the full version-and-provenance lifecycle that none of the
other scenarios touch:

  1. POST /remember          — save v1 of an architecture memory (the soon-to-be
                               stale decision) and a second memory that depends
                               on it.
  2. POST /relations         — record a `depends_on` edge: the dependent memory
                               points at v1.
  3. GET  /memories          — locate v1, inspect version / isLatest / strength.
  4. POST /evolve            — supersede v1 with v2 (new content, bumped version).
  5. POST /verify            — trace v2's citation chain back to source
                               observations + confidence (provenance).
  6. POST /cascade-update    — propagate the supersession to dependents that
                               still point at the now-stale v1.
  7. GET  /relations         — show the resulting edge graph (depends_on +
                               the supersedes link evolve created).
  8. DELETE /governance/memories — clean up the demo memories we created, with
                               an audit reason (keeps the store tidy on re-run).

Why these endpoints matter
  - /evolve is the *only* sanctioned way to change a curated memory. It writes a
    new Memory row with version+1, sets `supersedes: [oldId]` and `parentId`,
    and flips the old row's `isLatest` to false — so smart-search keeps
    returning the current truth while history stays auditable.
  - /verify answers "can I trust this?" — it walks sourceObservationIds back to
    the sessions that produced them and returns a confidence score. Critical
    before acting on a high-stakes memory.
  - /cascade-update stops dangling references: when v1 is superseded, anything
    that linked to v1 should learn about v2.

Run:
    python mcp/agentmemory/scenario_memory_evolution.py

Set CLEANUP = False at the bottom to leave the demo memories in place and
inspect them in the viewer.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, pp, step

# A marker so every memory this scenario creates is trivially identifiable and
# safe to garbage-collect at the end (and obvious in the viewer if cleanup off).
MARKER = "[evolve-demo]"


def remember(content: str, mtype: str, concepts: list[str], files: list[str]) -> str | None:
    """
    Save an immutable curated memory and return its ID.

    POST /agentmemory/remember
    body: {content, type, concepts, files, project}

    `type` is one of pattern|preference|architecture|bug|workflow|fact. We use
    "architecture" for the decision record. The server returns 201 with the
    created Memory; we pull the id from a couple of likely envelope shapes
    because the proxy and the raw API wrap it differently.
    """
    resp = call("POST", "/agentmemory/remember", body={
        "content": content,
        "type": mtype,
        "concepts": concepts,
        "files": files,
        "project": PROJECT,
    })
    # The memory may be returned bare, under "memory", or under "result".
    mem = resp.get("memory") or resp.get("result") or resp
    mem_id = mem.get("id") if isinstance(mem, dict) else None
    version = mem.get("version") if isinstance(mem, dict) else "?"
    print(f"    saved [{mtype}] id={mem_id} version={version}")
    print(f"           \"{content[:80]}\"")
    return mem_id


def relate(source_id: str, target_id: str, rel_type: str) -> dict:
    """
    Create a typed relation edge between two memories.

    POST /agentmemory/relations
    body: {sourceId, targetId, type}   → mem::relate

    Relation types mirror the graph edge vocabulary: depends_on, related_to,
    supersedes, extends, contradicts, etc. Here the dependent pipeline memory
    `depends_on` the pose3d backend decision.
    """
    print(f"\n  POST /agentmemory/relations  {source_id} --{rel_type}--> {target_id}")
    resp = call("POST", "/agentmemory/relations", body={
        "sourceId": source_id,
        "targetId": target_id,
        "type": rel_type,
    })
    ok = resp.get("success", "relation" in resp)
    print(f"    created={ok}")
    return resp


def find_memory(mem_id: str) -> dict | None:
    """
    Locate a memory by ID in the full curated list and print its version state.

    GET /agentmemory/memories  (q= is ignored server-side — always full list)

    We inspect version / isLatest / strength / supersedes so the supersession
    is visible before and after /evolve.
    """
    resp = call("GET", "/agentmemory/memories", params={"limit": 200, "project": PROJECT})
    for m in resp.get("memories", []):
        if m.get("id") == mem_id:
            print(f"    id={mem_id}")
            print(f"      version={m.get('version')}  isLatest={m.get('isLatest')}  "
                  f"strength={m.get('strength')}")
            print(f"      supersedes={m.get('supersedes')}  parentId={m.get('parentId')}")
            print(f"      content=\"{(m.get('content') or '')[:80]}\"")
            return m
    print(f"    [memory {mem_id} not found in list]")
    return None


def evolve(mem_id: str, new_content: str, new_title: str | None = None) -> str | None:
    """
    Supersede a memory with a new version.

    POST /agentmemory/evolve
    body: {memoryId, newContent, newTitle?}   → mem::evolve

    Effect: writes a new Memory with version+1, `parentId = mem_id`,
    `supersedes: [mem_id]`, `isLatest = true`; the old row's isLatest → false.
    The returned object is the *new* canonical version — capture its id.
    """
    print(f"\n  POST /agentmemory/evolve  memoryId={mem_id}")
    resp = call("POST", "/agentmemory/evolve", body={
        "memoryId": mem_id,
        "newContent": new_content,
        **({"newTitle": new_title} if new_title else {}),
    })
    if not resp.get("success", True) or resp.get("error"):
        print(f"    [evolve failed: {resp.get('error')}]")
        return None
    new_mem = resp.get("memory") or resp.get("result") or resp
    new_id = new_mem.get("id") if isinstance(new_mem, dict) else None
    print(f"    new canonical id={new_id} version={new_mem.get('version') if isinstance(new_mem, dict) else '?'}")
    print(f"      supersedes={new_mem.get('supersedes') if isinstance(new_mem, dict) else '?'}")
    return new_id


def verify(mem_id: str) -> dict:
    """
    Trace a memory's provenance — citation chain back to source observations.

    POST /agentmemory/verify
    body: {id}   → mem::verify

    Returns confidence + the observation/session chain that produced the memory.
    Use this before trusting a high-stakes record: a memory with no source
    observations and low confidence is a guess, not a fact.
    """
    print(f"\n  POST /agentmemory/verify  id={mem_id}")
    resp = call("POST", "/agentmemory/verify", body={"id": mem_id})
    if resp.get("error"):
        print(f"    [verify: {resp.get('error')}]  (fresh memory has no observation chain yet)")
        return resp
    conf = resp.get("confidence", resp.get("verification", {}).get("confidence", "?"))
    chain = resp.get("citationChain") or resp.get("sources") or resp.get("sourceObservationIds") or []
    print(f"    confidence={conf}  citation_chain_len={len(chain)}")
    pp(resp, label="verify result", truncate=600)
    return resp


def cascade_update(superseded_id: str) -> dict:
    """
    Propagate a supersession to everything that referenced the stale memory.

    POST /agentmemory/cascade-update
    body: {supersededMemoryId}   → mem::cascade-update

    When v1 is superseded by v2, dependents that linked to v1 (our pipeline
    memory's depends_on edge) should be repointed / flagged so future recall
    surfaces v2 instead of the stale decision.
    """
    print(f"\n  POST /agentmemory/cascade-update  supersededMemoryId={superseded_id}")
    resp = call("POST", "/agentmemory/cascade-update", body={"supersededMemoryId": superseded_id})
    if resp.get("error"):
        print(f"    [cascade: {resp.get('error')}]")
        return resp
    # mem::cascade-update reports how many linked entities it flagged for review
    # under `flagged: {nodes, edges, siblingMemories}` plus a `total` count.
    flagged = resp.get("flagged", {})
    total = resp.get("total", resp.get("updated", "?"))
    print(f"    flagged total={total}  "
          f"(siblingMemories={flagged.get('siblingMemories', 0)}, "
          f"graphNodes={flagged.get('nodes', 0)}, graphEdges={flagged.get('edges', 0)})")
    return resp


def list_relations() -> list[dict]:
    """
    Dump all memory relation edges (the project's relation graph).

    GET /agentmemory/relations   → lists every MemoryRelation row.

    After evolve + cascade we expect to see our depends_on edge plus whatever
    supersedes/parent linkage the evolve created.
    """
    resp = call("GET", "/agentmemory/relations")
    rels = resp.get("relations", [])
    print(f"\n  [relation edges]  ({len(rels)} total)")
    for r in rels[:12]:
        print(f"    {r.get('sourceId','?')[:18]} --{r.get('type','?')}--> {r.get('targetId','?')[:18]}")
    return rels


def governance_delete(mem_ids: list[str], reason: str) -> dict:
    """
    Hard-delete specific memories with an audit trail (cleanup).

    DELETE /agentmemory/governance/memories
    body: {memoryIds, reason}   → mem::governance-delete

    This is the governed delete path — every deletion is logged as an
    AuditEntry with operation="delete" and the reason string. We use it only on
    IDs this scenario created, so it is safe to run repeatedly.
    """
    if not mem_ids:
        return {}
    print(f"\n  DELETE /agentmemory/governance/memories  ({len(mem_ids)} ids)  reason=\"{reason}\"")
    resp = call("DELETE", "/agentmemory/governance/memories", body={
        "memoryIds": mem_ids,
        "reason": reason,
    })
    deleted = resp.get("deleted", resp.get("deletedCount", "?"))
    print(f"    deleted={deleted}")
    return resp


def run(cleanup: bool = True) -> None:
    banner("Memory Evolution & Provenance Loop")
    print(f"""
  Story: an architecture memory recorded that 3D pose runs through the vendored
  rtmpose3d package. A later refactor moved it to the standalone rtmlib API.
  We evolve the stale decision into a new version, verify its provenance, and
  cascade the change to a dependent memory — then clean up.
  """)
    check_health()

    created: list[str] = []

    # ── Step 1: save v1 (the decision that will go stale) ────────────────────
    step(1, "Save v1 — the soon-to-be-stale architecture decision")
    v1_id = remember(
        content=f"{MARKER} 3D pose estimation runs through the vendored rtmpose3d "
                f"package under vendor/rtmpose3d; imported directly in pose3d.py.",
        mtype="architecture",
        concepts=["rtmpose3d", "pose3d", "vendor", "3d-pose"],
        files=["delice_gen/preprocessing/pose3d.py"],
    )
    if v1_id:
        created.append(v1_id)

    # ── Step 2: save a dependent memory + relation ───────────────────────────
    step(2, "Save a dependent memory and link it with depends_on")
    print("""
  The preprocessing pipeline memory references the pose3d backend decision.
  We record an explicit depends_on edge so the cascade has something to update.
  """)
    dep_id = remember(
        content=f"{MARKER} The preprocessing pipeline wires pose3d as the 3D stage; "
                f"any change to the pose3d backend ripples into pipeline assembly.",
        mtype="architecture",
        concepts=["preprocessing", "pipeline", "pose3d"],
        files=["delice_gen/preprocessing/pipeline.py"],
    )
    if dep_id:
        created.append(dep_id)
    if dep_id and v1_id:
        relate(dep_id, v1_id, "depends_on")

    # ── Step 3: inspect v1 before evolution ──────────────────────────────────
    step(3, "Inspect v1 version state BEFORE evolution")
    if v1_id:
        find_memory(v1_id)

    # ── Step 4: evolve v1 → v2 ───────────────────────────────────────────────
    step(4, "Evolve v1 → v2 (refactor superseded the vendored package)")
    print("""
  /evolve writes a NEW memory (version+1) with supersedes=[v1] and flips v1's
  isLatest to false. smart-search will now surface v2; v1 stays for history.
  """)
    v2_id = None
    if v1_id:
        v2_id = evolve(
            v1_id,
            new_content=f"{MARKER} 3D pose estimation now calls the standalone rtmlib API "
                        f"(pip package); the vendored rtmpose3d tree was removed in the "
                        f"dev-container migration.",
        )
        if v2_id:
            created.append(v2_id)

    # ── Step 5: confirm supersession + verify provenance ─────────────────────
    step(5, "Confirm supersession & verify v2 provenance")
    if v1_id:
        print("\n  v1 AFTER evolve (expect isLatest=false):")
        find_memory(v1_id)
    if v2_id:
        print("\n  v2 (expect isLatest=true, supersedes=[v1]):")
        find_memory(v2_id)
        verify(v2_id)

    # ── Step 6: cascade the supersession to dependents ───────────────────────
    step(6, "Cascade the supersession to dependents")
    if v1_id:
        cascade_update(v1_id)

    # ── Step 7: inspect the resulting relation graph ─────────────────────────
    step(7, "Inspect the relation graph after evolve + cascade")
    list_relations()

    # ── Step 8: cleanup ──────────────────────────────────────────────────────
    step(8, "Governed cleanup of demo memories")
    if cleanup:
        governance_delete(created, reason="evolve-demo scenario cleanup")
    else:
        print(f"  [cleanup disabled — leaving {len(created)} demo memories: {created}]")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  EVOLUTION SUMMARY")
    print(f"{'─' * 72}")
    print("  remember        → immutable v1 + dependent memory")
    print("  relations(POST) → depends_on edge dependent → v1")
    print("  evolve          → v2 supersedes v1; v1.isLatest=false")
    print("  verify          → provenance/citation chain + confidence for v2")
    print("  cascade-update  → dependents repointed off the stale v1")
    print("  governance-del  → audited cleanup of the demo set")
    print(f"{'═' * 72}")
    print("  Takeaway: never delete a curated memory to 'fix' it — evolve it,")
    print("  so the lineage (parentId / supersedes) survives for provenance.")


if __name__ == "__main__":
    CLEANUP = True
    run(cleanup=CLEANUP)
