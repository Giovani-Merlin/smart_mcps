"""
SCENARIO: Temporal Forensics Loop ("why is the code like this?")
================================================================
An agent inherits a decision it doesn't understand — the repo migrated to a dev
container and the vendored packages moved around. Instead of guessing, it
reconstructs *what happened, when, and which operations touched memory* using
the three temporal endpoints the other scenarios never call:

  1. POST /timeline   — chronological observations around an anchor (a concept
                        name OR an ISO date). Returns a window of `before`
                        observations, the anchor, and `after` observations,
                        each tagged with its owning sessionId.
  2. GET  /sessions   — resolve the sessionIds from the timeline window into
                        human-readable session summaries (the client-side join
                        the engineering reference prescribes — there is no
                        server-side session text search).
  3. GET  /audit      — the operation ledger. Every mutation (action_create,
                        evolve, consolidate, delete, lesson_save, share, …) is
                        an AuditEntry with operation, targetIds, details,
                        timestamp. Filter by operation to answer "who changed
                        what".
  4. GET  /commits  +  GET /session/by-commit
                      — map sessions to the git commits they produced, closing
                        the loop from "memory event" to "code change".

The forensic chain is: anchor → timeline window → owning sessions → audit ops in
that era → linked commits. That is how you answer "why is this code here?"
without a single guess.

Run:
    python mcp/agentmemory/scenario_temporal_forensics.py

Edit ANCHOR / FOCUS_OPERATION at the bottom to investigate a different event.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, step

# The event under investigation. An anchor can be a concept/keyword (matched
# against observation content) or an ISO timestamp.
ANCHOR = "devcontainer"
# Which audit operation to drill into. One of: action_create, action_update,
# evolve, delete, consolidate, lesson_save, lesson_recall, share, relation_create…
FOCUS_OPERATION = "consolidate"


def timeline_window(anchor: str, before: int = 4, after: int = 4) -> list[dict]:
    """
    Reconstruct the chronological observation window around an anchor.

    POST /agentmemory/timeline
    body: {anchor, project, before, after}   → mem::timeline

    Response: {anchorIndex, entries: [{observation, relativePosition, sessionId}]}.
    `anchorIndex` is the position of the matched anchor in `entries`; positions
    below it happened *before*, above it *after*. We print the sequence with a
    marker on the anchor so the lead-up and aftermath are both visible.
    """
    print(f"\n  POST {BASE_URL}/agentmemory/timeline  anchor=\"{anchor}\"  ±{before}/{after}")
    resp = call("POST", "/agentmemory/timeline", body={
        "anchor": anchor,
        "project": PROJECT,
        "before": before,
        "after": after,
    })
    entries = resp.get("entries", [])
    anchor_idx = resp.get("anchorIndex", -1)
    print(f"    anchorIndex={anchor_idx}  window={len(entries)} observations\n")
    for i, e in enumerate(entries):
        obs = e.get("observation", {})
        rel = e.get("relativePosition", i - anchor_idx)
        otype = obs.get("type", "?")
        title = (obs.get("title") or "")[:54]
        ts = (obs.get("timestamp") or "")[:19]
        sid = (e.get("sessionId") or obs.get("sessionId") or "")[:18]
        marker = "  ◀── ANCHOR" if i == anchor_idx else ""
        sign = f"{rel:+d}" if isinstance(rel, int) else str(rel)
        print(f"    [{sign:>3}] {ts}  [{otype:11}] \"{title}\"  sess={sid}{marker}")
    return entries


def resolve_sessions(entries: list[dict]) -> dict[str, dict]:
    """
    Join the timeline's sessionIds to readable session records.

    GET /agentmemory/sessions   → {sessions: Session[]}

    There is no server-side session text search, so the documented pattern is:
    fetch the flat session list and client-side map id → {summary, firstPrompt,
    startedAt, commitShas}. We only resolve the sessions that actually appear in
    the timeline window — those are the ones that produced the era we care about.
    """
    window_sids = {
        (e.get("sessionId") or e.get("observation", {}).get("sessionId"))
        for e in entries
    }
    window_sids.discard(None)

    resp = call("GET", "/agentmemory/sessions", params={"limit": 200})
    by_id = {s.get("id"): s for s in resp.get("sessions", [])}

    print(f"\n  [sessions behind the window]  ({len(window_sids)} distinct)")
    resolved: dict[str, dict] = {}
    for sid in window_sids:
        s = by_id.get(sid, {})
        resolved[sid] = s
        summary = (s.get("summary") or s.get("firstPrompt") or "(no summary)")[:60]
        started = (s.get("startedAt") or "")[:19]
        commits = s.get("commitShas") or []
        print(f"    {sid[:20]}  {started}  commits={len(commits)}")
        print(f"        \"{summary}\"")
    return resolved


def audit_histogram(limit: int = 300) -> Counter:
    """
    Build a histogram of every memory operation in the ledger.

    GET /agentmemory/audit?limit=N   → {entries: AuditEntry[]}

    The histogram answers "what kind of work has this project's memory seen?" —
    a quick fingerprint of activity (lots of consolidate = heavy summarisation;
    lots of evolve = churny decisions; lots of delete = active governance).
    """
    resp = call("GET", "/agentmemory/audit", params={"limit": limit})
    entries = resp.get("entries", [])
    hist = Counter(e.get("operation", "?") for e in entries)
    print(f"\n  [audit operation histogram]  ({len(entries)} entries scanned)")
    for op, n in hist.most_common():
        bar = "█" * min(n, 40)
        print(f"    {op:18} {n:4}  {bar}")
    return hist


def audit_drilldown(operation: str, limit: int = 12) -> list[dict]:
    """
    Drill into a single operation type — the "who changed what" view.

    GET /agentmemory/audit?operation=<op>&limit=N

    Each AuditEntry carries `targetIds` (what was touched), `functionId` (the
    mem:: function that ran), and `details` (operation-specific payload, e.g.
    {oldId, newId} for evolve, {deleted, reason} for delete).
    """
    print(f"\n  GET {BASE_URL}/agentmemory/audit?operation={operation}")
    resp = call("GET", "/agentmemory/audit", params={"operation": operation, "limit": limit})
    entries = resp.get("entries", [])
    print(f"    {len(entries)} '{operation}' entries:")
    for e in entries[:limit]:
        ts = (e.get("timestamp") or "")[:19]
        fn = e.get("functionId", "?")
        targets = e.get("targetIds") or []
        details = str(e.get("details") or {})[:70]
        print(f"    {ts}  fn={fn}  targets={len(targets)}")
        print(f"        details={details}")
    return entries


def commit_linkage(resolved_sessions: dict[str, dict]) -> None:
    """
    Close the loop: memory era → git commits.

    GET /agentmemory/commits                 → all session-linked commits
    GET /agentmemory/session/by-commit?sha=  → sessions that produced a commit

    If consolidation/commit-linking is configured, the sessions from the
    timeline window carry commitShas; we resolve the first one back to confirm
    the round-trip. When no commits are linked yet (fresh project), this
    degrades gracefully and just reports the empty ledger.
    """
    resp = call("GET", "/agentmemory/commits", params={"limit": 20})
    commits = resp.get("commits", [])
    print(f"\n  [commit ledger]  ({len(commits)} linked commits)")
    for c in commits[:5]:
        sha = (c.get("sha") or "")[:10]
        msg = (c.get("message") or "")[:50]
        print(f"    {sha}  \"{msg}\"")

    # Try the reverse lookup for any session in the window that has a commit.
    sha = next(
        (s["commitShas"][0] for s in resolved_sessions.values()
         if s.get("commitShas")),
        None,
    )
    if not sha and commits:
        sha = commits[0].get("sha")
    if sha:
        print(f"\n  GET {BASE_URL}/agentmemory/session/by-commit?sha={sha[:10]}…")
        bc = call("GET", "/agentmemory/session/by-commit", params={"sha": sha})
        sessions = bc.get("sessions", [])
        print(f"    commit {sha[:10]} ← produced by {len(sessions)} session(s)")
    else:
        print("\n  [no commit SHAs linked to these sessions — commit linking not "
              "populated for this era]")


def run() -> None:
    banner("Temporal Forensics — why is the code like this?")
    print(f"""
  Under investigation: "{ANCHOR}"
  Chain: anchor → timeline window → owning sessions → audit ops → commits.
  Goal: explain a past change with evidence, not guesswork.
  """)
    check_health()

    # ── Step 1: reconstruct the timeline window ──────────────────────────────
    step(1, f"Timeline window around anchor: \"{ANCHOR}\"")
    print("""
  The anchor matches observation content semantically. relativePosition is
  negative for the lead-up, 0 at the anchor, positive for the aftermath.
  """)
    entries = timeline_window(ANCHOR, before=4, after=4)

    # ── Step 2: resolve owning sessions ──────────────────────────────────────
    step(2, "Resolve owning sessions (client-side id → summary join)")
    print("""
  /sessions has no text-search param, so we fetch the flat list and join the
  sessionIds that appeared in the window to their LLM-generated summaries.
  """)
    resolved = resolve_sessions(entries) if entries else {}

    # ── Step 3: audit histogram ──────────────────────────────────────────────
    step(3, "Audit histogram — fingerprint of memory activity")
    audit_histogram()

    # ── Step 4: audit drill-down ─────────────────────────────────────────────
    step(4, f"Audit drill-down — operation=\"{FOCUS_OPERATION}\"")
    print("""
  Each entry's targetIds + details say exactly what was touched. For evolve
  you'd see {oldId,newId}; for delete {deleted,reason}; for consolidate the
  tier and counts. This is the audit trail you cite when explaining a change.
  """)
    audit_drilldown(FOCUS_OPERATION)

    # ── Step 5: commit linkage ───────────────────────────────────────────────
    step(5, "Commit linkage — memory era → git history")
    commit_linkage(resolved)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  FORENSICS SUMMARY")
    print(f"{'─' * 72}")
    print("  timeline        → chronological window (lead-up / anchor / aftermath)")
    print("  sessions(join)  → who was working then (summaries, not UUIDs)")
    print("  audit(histogram)→ what KINDS of memory ops dominated the era")
    print("  audit(drill)    → exact targetIds + details for one operation")
    print("  commits         → tie the memory era back to git changes")
    print(f"{'═' * 72}")
    print("  This is the read-only counterpart to scenario_memory_evolution:")
    print("  that one *makes* the change; this one *explains* a past change.")


if __name__ == "__main__":
    run()
