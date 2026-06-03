"""
SCENARIO: Facet Tagging & Governance Hygiene
============================================
The memory store grows without bound unless someone curates it. This scenario
is the *governance* loop: annotate memories with dimensional metadata (facets),
slice the store by those dimensions, and safely preview a bulk cleanup — using
the facet + governance endpoints no other scenario touches.

A **Facet** is a (dimension, value) tag attached to any action / memory /
observation — e.g. dimension="sensitivity" value="high", or
dimension="review-status" value="needs-review". Unlike concepts (which are
extracted), facets are *imposed* by a curator for filtering and policy.

  1. mcp memory_facet_tag      — attach governance dimensions to real memories.
  2. GET  /facets?targetId      — read all facets on one entity.
  3. GET  /facets/stats         — dimensional histogram across the whole store
                                 (how many memories carry each dimension/value).
  4. POST /facets/query         — slice the store by facet: matchAll (AND across
                                 dimensions) or matchAny (OR). This is the
                                 "show me everything tagged needs-review" view.
  5. POST /governance/bulk-delete (dryRun) — preview a policy-driven purge: a
                                 GovernanceFilter (type / dateFrom-To / project /
                                 qualityBelow) reports `wouldDelete` + the exact
                                 ids WITHOUT deleting anything while dryRun=true.

Everything destructive here is dry-run, and every facet the scenario adds is
removed again at the end — so it is safe to run against a live store.

Run:
    python mcp/agentmemory/scenario_facet_governance.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, mcp_call, step


def pick_memories(n: int = 3) -> list[dict]:
    """Fetch a few real curated memories to annotate (read-only)."""
    resp = call("GET", "/agentmemory/memories", params={"limit": 50, "project": PROJECT})
    mems = resp.get("memories", [])[:n]
    print(f"\n  picked {len(mems)} memories to annotate:")
    for m in mems:
        print(f"    {m.get('id')}  [{m.get('type')}]  \"{(m.get('content') or m.get('title') or '')[:54]}\"")
    return mems


def facet_tag(target_id: str, dimension: str, value: str) -> tuple[str, str]:
    """
    Attach a governance facet to an entity.

    mcp memory_facet_tag → mem::facet-tag (POST /agentmemory/mcp/call)
    args: {targetId, targetType, dimension, value}

    Returns the created Facet. We return (targetId, dimension) so the caller can
    untag it during cleanup (POST /facets/remove keys on targetId+dimension).
    """
    result = mcp_call("memory_facet_tag", {
        "targetId": target_id,
        "targetType": "memory",
        "dimension": dimension,
        "value": value,
    })
    fid = result.get("facet", {}).get("id") if isinstance(result, dict) else "?"
    print(f"    tagged {target_id[:22]}  {dimension}={value}  (facet {fid})")
    return (target_id, dimension)


def get_facets(target_id: str) -> dict:
    """
    Read all facets on one entity.

    GET /agentmemory/facets?targetId=   → mem::facet-get
    Returns {dimensions: [{dimension, values: [...]}]}.
    """
    print(f"\n  GET {BASE_URL}/agentmemory/facets?targetId={target_id[:22]}")
    resp = call("GET", "/agentmemory/facets", params={"targetId": target_id})
    for d in resp.get("dimensions", []):
        print(f"    {d.get('dimension')}: {d.get('values')}")
    return resp


def facet_stats() -> dict:
    """
    Dimensional histogram across all memory facets.

    GET /agentmemory/facets/stats?targetType=memory   → mem::facet-stats
    Returns {dimensions: [{dimension, values: [{value, count}]}], totalFacets}.

    This is the governance dashboard: at a glance, how much of the store is
    flagged high-sensitivity, how big the needs-review queue is, etc.
    """
    print(f"\n  GET {BASE_URL}/agentmemory/facets/stats?targetType=memory")
    resp = call("GET", "/agentmemory/facets/stats", params={"targetType": "memory"})
    print(f"    totalFacets={resp.get('totalFacets', '?')}")
    for d in resp.get("dimensions", []):
        pairs = ", ".join(f"{v.get('value')}×{v.get('count')}" for v in d.get("values", []))
        print(f"    {d.get('dimension'):16} → {pairs}")
    return resp


def facet_query(filt: dict, label: str) -> list[dict]:
    """
    Slice the store by facet filter.

    POST /agentmemory/facets/query   → mem::facet-query
    body: {matchAll?: {dim: value, ...}, matchAny?: {dim: value, ...}, targetType?}

    The filter object maps dimension → value (NOT an array of pairs — that
    silently returns nothing). Returns {results: [{targetId, targetType,
    matchedFacets}]}.

    OBSERVED (v0.9.24): matchAll behaves loosely — it returns the candidate set
    of targets that carry the queried dimensions rather than a strict AND
    intersection, and `matchedFacets` comes back empty. Treat the result as
    "entities in scope for these dimensions" and confirm exact values via
    GET /facets?targetId if you depend on strict matching.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/facets/query  [{label}]  filter={filt}")
    resp = call("POST", "/agentmemory/facets/query", body=filt)
    results = resp.get("results", [])
    print(f"    {len(results)} matching entities:")
    for r in results[:8]:
        print(f"      {r.get('targetType','?'):11} {r.get('targetId','?')}")
    return results


def governance_dryrun(filt: dict, label: str) -> dict:
    """
    Preview a policy-driven bulk purge WITHOUT deleting anything.

    POST /agentmemory/governance/bulk-delete
    body: GovernanceFilter + {dryRun: true}
      GovernanceFilter: {type?: [...], dateFrom?, dateTo?, project?, qualityBelow?}

    With dryRun=true the response is {dryRun: true, wouldDelete: N, ids: [...]}
    — the exact set a real purge would remove. ALWAYS dry-run a governance
    delete first; the live version is irreversible.
    """
    body = {**filt, "dryRun": True}
    print(f"\n  POST {BASE_URL}/agentmemory/governance/bulk-delete  [{label}]  (dryRun)")
    print(f"    filter={filt}")
    resp = call("POST", "/agentmemory/governance/bulk-delete", body=body)
    print(f"    dryRun={resp.get('dryRun')}  wouldDelete={resp.get('wouldDelete')}")
    ids = resp.get("ids", [])
    for i in ids[:6]:
        print(f"      would remove: {i}")
    if len(ids) > 6:
        print(f"      … +{len(ids) - 6} more")
    return resp


def untag(pairs: list[tuple[str, str]]) -> None:
    """
    Remove the facets this scenario added (cleanup).

    POST /agentmemory/facets/remove  body: {targetId, dimension}  → mem::facet-untag
    """
    removed = 0
    for target_id, dimension in pairs:
        resp = call("POST", "/agentmemory/facets/remove", body={
            "targetId": target_id, "dimension": dimension,
        })
        removed += resp.get("removed", 0)
    print(f"    removed {removed} demo facet(s)")


def run() -> None:
    banner("Facet Tagging & Governance Hygiene")
    print("""
  Story: a curator imposes governance dimensions on memories (sensitivity,
  review-status, lifecycle), slices the store by those tags, then SAFELY previews
  a policy-driven cleanup. Every write is reverted; the purge is dry-run only.
  """)
    check_health()

    tagged: list[tuple[str, str]] = []

    # ── Step 1: tag memories with governance dimensions ──────────────────────
    step(1, "Tag memories with governance facets")
    print("""
  Facets are imposed metadata (unlike auto-extracted concepts). We annotate
  three real memories along sensitivity / review-status / lifecycle dimensions.
  """)
    mems = pick_memories(3)
    if len(mems) >= 3:
        a, b, c_ = mems[0]["id"], mems[1]["id"], mems[2]["id"]
        print()
        tagged.append(facet_tag(a, "sensitivity", "high"))
        tagged.append(facet_tag(a, "lifecycle", "keep"))
        tagged.append(facet_tag(b, "review-status", "needs-review"))
        tagged.append(facet_tag(b, "lifecycle", "stale"))
        tagged.append(facet_tag(c_, "review-status", "needs-review"))
    else:
        print("  [need at least 3 memories to demo facets — store too small]")
        return

    # ── Step 2: read facets on one entity ────────────────────────────────────
    step(2, "Read all facets on one memory")
    get_facets(a)

    # ── Step 3: dimensional stats ────────────────────────────────────────────
    step(3, "Facet stats — the governance dashboard")
    facet_stats()

    # ── Step 4: query by facet ───────────────────────────────────────────────
    step(4, "Query the store by facet (the review queue)")
    print("""
  matchAny needs-review pulls the whole review queue. matchAll is *intended* to
  narrow to memories matching every dimension — but see the OBSERVED note in
  facet_query(): this build returns the candidate set loosely, so verify values
  via GET /facets?targetId when strictness matters.
  """)
    facet_query({"matchAny": {"review-status": "needs-review"}, "targetType": "memory"},
                "matchAny review-status=needs-review")
    facet_query({"matchAll": {"review-status": "needs-review", "lifecycle": "stale"},
                 "targetType": "memory"},
                "matchAll needs-review AND stale")

    # ── Step 5: governance dry-run ───────────────────────────────────────────
    step(5, "Governance bulk-delete — DRY RUN (preview a purge)")
    print("""
  A GovernanceFilter selects by type / date / project / qualityBelow. With
  dryRun=true nothing is deleted — you get wouldDelete + the exact id set, so a
  human can review the blast radius before any irreversible purge.
  """)
    # Quality threshold so low that (hopefully) nothing qualifies — proves the
    # preview is safe and scoped, not a blanket match.
    governance_dryrun({"qualityBelow": 0.05, "project": PROJECT}, "qualityBelow=0.05")
    governance_dryrun({"type": ["bug"], "project": PROJECT}, "type=bug")

    # ── Cleanup ──────────────────────────────────────────────────────────────
    step(6, "Cleanup — remove the demo facets")
    untag(tagged)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  GOVERNANCE SUMMARY")
    print(f"{'─' * 72}")
    print("  facet_tag           → impose governance dimensions on memories")
    print("  facets?targetId     → read one entity's facets")
    print("  facets/stats        → dimensional histogram (governance dashboard)")
    print("  facets/query        → slice by matchAll (AND) / matchAny (OR)")
    print("  governance(dryRun)  → preview wouldDelete set before any real purge")
    print(f"{'═' * 72}")
    print("  Filter shape gotcha: facets/query matchAll/matchAny take a")
    print("  {dimension: value} OBJECT — an array of {dimension,value} returns [].")


if __name__ == "__main__":
    run()
