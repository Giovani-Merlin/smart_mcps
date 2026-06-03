"""
SCENARIO: Bug / Error Recovery Loop
======================================
An agent hits a library import error during preprocessing. Instead of
debugging from scratch, it queries episodic memory for prior encounters
with the same error, recovering the fix from a past session.

Simulated error:
    ModuleNotFoundError: No module named 'rtmpose3d'
    (occurs when trying to import rtmpose3d for 3D pose estimation)

Recovery strategy:
  1. smart-search with the exact error message
  2. progressive disclosure: expand the top bug/error obs to get context window
  3. lesson-recall: check if we recorded a procedural fix as a lesson
  4. file-scoped enrich: check if the affected files have prior bug history
  5. filter memories by error type and related concepts

Key observation types to look for:
  type="bug"   — explicitly tagged bug observations
  type="error" — error observations (traceback, import failure, etc.)
  type="decision" — may document workarounds
  type="pattern"  — established fix patterns

Key endpoints:
  POST /agentmemory/smart-search    — find matching obs by error text
  POST /agentmemory/smart-search    — expand best match with expandIds
  MCP  memory_lesson_recall         — search lessons for fix patterns
  POST /agentmemory/enrich          — file-scoped bug candidates
  GET  /agentmemory/memories        — filter for bug/constraint type memories

Run:
    python mcp/agentmemory/scenario_bug_recovery.py

Edit SIMULATED_ERROR at the bottom to try different error messages.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _client import BASE_URL, PROJECT, banner, call, check_health, get_latest_session_id, mcp_call, pp, step

# ── The simulated error the agent encountered ───────────────────────────────
SIMULATED_ERROR = "ModuleNotFoundError: No module named 'rtmpose3d'"
AFFECTED_FILE = "delice_gen/preprocessing/pose3d.py"
SECONDARY_QUERIES = [
    "rtmpose3d install dependency CUDA",
    "vendor package missing import",
    "pose 3d estimation python module",
]


def search_for_error(error_msg: str, limit: int = 10) -> list[dict]:
    """
    Search episodic memory for observations matching the error message.

    POST /agentmemory/smart-search
    body: {query, limit, format: "full", project}

    Look for obs with type "bug" or "error" in the results — these are
    explicitly tagged by the agent or agentmemory's summarization pipeline.
    Also check obs with the error terms in their facts[].

    Observation types to watch for:
      "error"    — traceback/error observations
      "bug"      — agent-tagged bug observations
      "decision" — may document a workaround
      "other"    — catch-all that includes misc useful context
    """
    print(f"\n  Searching for: \"{error_msg}\"")
    print(f"  POST {BASE_URL}/agentmemory/smart-search")
    resp = call("POST", "/agentmemory/smart-search", body={
        "query": error_msg,
        "limit": limit,
        "format": "full",
        "project": PROJECT,
    })
    results = resp.get("results", [])
    lessons = resp.get("lessons", [])
    mode = resp.get("mode", "?")

    print(f"\n  mode={mode}  obs_hits={len(results)}  bundled_lessons={len(lessons)}")
    print(f"\n  [observations — looking for type=bug/error]")
    for i, r in enumerate(results[:8]):
        obs_type = r.get("type", "?")
        score = r.get("score", 0)
        title = (r.get("title") or "")[:70]
        obs_id = (r.get("id") or r.get("obsId") or "")[:20]
        marker = " ← BUG/ERROR" if obs_type in ("bug", "error") else ""
        print(f"    {i+1:2}. [{obs_type:12}] score={score:.3f}  {obs_id}  \"{title}\"{marker}")
        facts = r.get("facts", [])
        if facts:
            print(f"         facts[0]: {str(facts[0])[:100]}")

    if lessons:
        print(f"\n  [bundled lessons — may contain fix patterns]")
        for l in lessons[:3]:
            conf = l.get("confidence", "?")
            content = (l.get("content") or "")[:90]
            print(f"    conf={conf}  \"{content}\"")

    return results


def expand_best_bug_hit(query: str, results: list[dict]) -> list[dict]:
    """
    Progressive disclosure — expand the best bug/error observation.

    POST /agentmemory/smart-search with expandIds
    body: {query, expandIds: [obs_id], limit, format: "full"}

    The expanded context window shows what the agent was doing when the error
    occurred and what happened next (the fix, the workaround, the decision).

    Strategy: prefer obs with type="bug" or "error"; fall back to highest score.
    """
    # Prefer bug/error type observations
    bug_obs = [r for r in results if r.get("type") in ("bug", "error")]
    target = bug_obs[0] if bug_obs else (results[0] if results else None)

    if not target:
        print("\n  [no observations to expand]")
        return []

    target_id = target.get("id") or target.get("obsId")
    obs_type = target.get("type")
    title = (target.get("title") or "")[:70]
    print(f"\n  Expanding best hit: [{obs_type}] \"{title}\"  id={target_id}")

    resp = call("POST", "/agentmemory/smart-search", body={
        "query": query,
        "expandIds": [target_id],
        "limit": 12,
        "format": "full",
        "project": PROJECT,
    })
    expanded = resp.get("results", [])
    print(f"\n  Expanded {1} obs → {len(expanded)} in context window")
    print("  [context window — what the agent was doing around the error]")
    for obs in expanded[:6]:
        obs_type = obs.get("type", "?")
        title = (obs.get("title") or "")[:70]
        facts = obs.get("facts", [])
        fact_preview = str(facts[0])[:90] if facts else "(no facts — tombstoned)"
        is_target = (obs.get("id") or obs.get("obsId")) == target_id
        marker = " ← ERROR" if is_target else ""
        print(f"    [{obs_type:12}] \"{title}\"{marker}")
        print(f"                   {fact_preview}")
    return expanded


def recall_fix_lessons(error_msg: str) -> list[dict]:
    """
    Search lessons for documented fix patterns about this error.

    MCP function: memory_lesson_recall → mem::lesson-recall
    args: {query, project, minConfidence, limit}

    Lessons with high confidence have been reinforced many times —
    a lesson like "rtmpose3d requires CUDA 11.8" with conf=0.95 means
    it bit us more than once and should be trusted.
    """
    # Extract key terms from error message
    query = " ".join([
        "import error",
        "missing module",
        "dependency",
        error_msg.split("'")[1] if "'" in error_msg else error_msg.split()[-1],
    ])
    print(f"\n  mcp_call memory_lesson_recall  query=\"{query[:80]}\"")
    result = mcp_call("memory_lesson_recall", {
        "query": query,
        "project": PROJECT,
        "minConfidence": "0.3",
        "limit": "8",
    })
    lessons = result if isinstance(result, list) else result.get("lessons", [])
    print(f"\n  [lessons about this error type]  ({len(lessons)} found)")
    for l in lessons[:5]:
        conf = l.get("confidence", "?")
        content = (l.get("content") or "")[:100]
        ctx = (l.get("context") or "")[:50]
        print(f"    conf={conf}  ctx=\"{ctx}\"")
        print(f"    FIX: \"{content}\"")
    return lessons


def check_file_history(file_path: str) -> dict:
    """
    Get episodic history for the affected file — prior bugs, fixes, decisions.

    MCP function: memory_file_history → mem::file-context
    Endpoint: POST /agentmemory/mcp/call
    args: {files (comma-sep paths)}

    Shows prior sessions that touched this file, including any bugs or
    decisions that were recorded. Useful to see if the file has a history
    of import issues.
    """
    print(f"\n  mcp_call memory_file_history  files={file_path}")
    result = mcp_call("memory_file_history", {"files": file_path})
    context = result.get("context") if isinstance(result, dict) else str(result)
    if context and context != "No history found.":
        print(f"\n  [file history for {file_path}]")
        for line in str(context)[:500].splitlines()[:12]:
            print(f"    {line}")
    else:
        print(f"  [no history found for {file_path}]")
    return result


def file_scoped_enrich(file_path: str) -> dict:
    """
    File-scoped enrichment — find bug candidates and bridging memories.

    POST /agentmemory/enrich
    body: {sessionId, files: [path], project, terms?: [str]}

    Returns:
      enrichedContext:   observations about this file from all sessions
      bugCandidates:     observations flagged as potential bugs/errors
      bridgingMemories:  memories that link across sessions for this file

    WHY: /enrich gives a cross-session view of a file's bug history,
    more targeted than a text search. Terms can narrow it to error keywords.
    """
    session_id = get_latest_session_id()
    if not session_id:
        print("  [no sessions found — cannot enrich]")
        return {}

    print(f"\n  POST {BASE_URL}/agentmemory/enrich  file={file_path}")
    resp = call("POST", "/agentmemory/enrich", body={
        "sessionId": session_id,
        "files": [file_path],
        "project": PROJECT,
        "terms": ["import", "ModuleNotFoundError", "rtmpose3d", "vendor"],
    })

    enriched = resp.get("enrichedContext", [])
    bugs = resp.get("bugCandidates", [])
    bridging = resp.get("bridgingMemories", [])

    print(f"\n  enrichedContext: {len(enriched)}  bugCandidates: {len(bugs)}  bridgingMemories: {len(bridging)}")

    if bugs:
        print(f"\n  [bug candidates — prior errors in this file]")
        for b in bugs[:3]:
            btype = b.get("type", "?")
            title = (b.get("title") or "")[:70]
            facts = b.get("facts", [])
            print(f"    [{btype:12}] \"{title}\"")
            if facts:
                print(f"                 {str(facts[0])[:90]}")

    if enriched:
        print(f"\n  [enriched context — prior observations for this file]")
        for e in enriched[:3]:
            print(f"    [{e.get('type','?'):12}] {(e.get('title') or '')[:70]}")

    if bridging:
        print(f"\n  [bridging memories — cross-session memories for this file]")
        for m in bridging[:3]:
            print(f"    [{m.get('type','?'):14}] {(m.get('title') or m.get('content') or '')[:70]}")

    return resp


def find_bug_memories() -> list[dict]:
    """
    Find curated memories of type bug or constraint in this project.

    GET /agentmemory/memories  (always returns full list)
    Client-side filter by type in ["bug", "constraint", "fact"].

    These are the permanent records of prior bugs fixed — curated by agents
    who worked on the project previously. High-value reference.
    """
    resp = call("GET", "/agentmemory/memories", params={"limit": 100, "project": PROJECT})
    all_memories = resp.get("memories", [])
    bug_memories = [
        m for m in all_memories
        if m.get("type") in ("bug", "constraint")
    ]
    error_keywords = {"import", "module", "error", "install", "cuda", "dependency", "vendor"}
    relevant = [
        m for m in bug_memories
        if any(
            k in " ".join([
                m.get("content") or "",
                " ".join(m.get("concepts", [])),
            ]).lower()
            for k in error_keywords
        )
    ]
    print(f"\n  [bug/constraint memories]  {len(relevant)}/{len(bug_memories)} match error keywords")
    for m in relevant[:4]:
        strength = m.get("strength", "?")
        mtype = m.get("type", "?")
        content = (m.get("content") or "")[:90]
        print(f"    [{mtype:12}] strength={strength}  \"{content}\"")
    return relevant


def run() -> None:
    banner("Bug / Error Recovery Loop")
    print(f"""
  Simulated error: {SIMULATED_ERROR}
  Affected file:   {AFFECTED_FILE}

  Pattern: hit error → search episodic memory → expand context window
  → recall fix lessons → check file history → find bug memories.
  Goal: recover the fix without starting from scratch.
  """)
    check_health()

    # ── Step 1: search for the exact error ────────────────────────────────
    step(1, f"Search for exact error: {SIMULATED_ERROR[:60]}")
    results = search_for_error(SIMULATED_ERROR)

    # ── Step 2: progressive disclosure ────────────────────────────────────
    step(2, "Progressive disclosure — expand best bug/error observation")
    print("""
  expandIds returns the 5 observations before+after the matched ID.
  This reveals: what the agent tried before the error, and what fixed it.
  """)
    expand_best_bug_hit(SIMULATED_ERROR, results)

    # ── Step 3: secondary query — installation/fix terms ─────────────────
    step(3, f"Secondary search — installation/fix terms")
    print(f"  Trying: \"{SECONDARY_QUERIES[0]}\"")
    results2 = search_for_error(SECONDARY_QUERIES[0], limit=6)

    # ── Step 4: lesson recall — documented fixes ──────────────────────────
    step(4, "Lesson recall — check if a fix pattern was documented")
    print("""
  Lessons are the first-class store for recurring fixes.
  If someone previously fixed this and ran memory_lesson_save("rtmpose3d requires..."),
  it surfaces here with high confidence. No lesson = first time hitting this bug.
  """)
    recall_fix_lessons(SIMULATED_ERROR)

    # ── Step 5: file history ───────────────────────────────────────────────
    step(5, f"File history — prior bugs in {AFFECTED_FILE}")
    print("""
  mem::file-context returns a narrative of all prior agent observations
  about this file. Shows if the import error appeared before and how it was fixed.
  """)
    check_file_history(AFFECTED_FILE)

    # ── Step 6: file-scoped enrich ─────────────────────────────────────────
    step(6, f"File enrichment — cross-session bug candidates for {AFFECTED_FILE}")
    print("""
  POST /agentmemory/enrich gives bugCandidates: observations flagged as
  potential bugs/errors specifically about this file across all sessions.
  """)
    file_scoped_enrich(AFFECTED_FILE)

    # ── Step 7: bug/constraint memories ───────────────────────────────────
    step(7, "Bug/constraint memories — permanent fix records")
    print("""
  GET /agentmemory/memories filtered by type=bug/constraint.
  These are immutable records saved via POST /remember — the most reliable
  source of prior fixes because they're explicitly curated (not auto-extracted).
  """)
    find_bug_memories()

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print(f"  RECOVERY SUMMARY for: {SIMULATED_ERROR[:60]}")
    print(f"{'─' * 72}")
    print(f"  1. smart-search:   scanned episodic obs for error text + bug-type hits")
    print(f"  2. expandIds:      expanded best hit for surrounding context window")
    print(f"  3. lesson-recall:  checked for documented fixes (lessons store)")
    print(f"  4. file-context:   checked file's prior session bug history")
    print(f"  5. enrich:         file-scoped cross-session bug candidates")
    print(f"  6. memories:       bug/constraint type curated records")
    print(f"{'═' * 72}")
    print(f"  If no fix was found: this is a new bug → fix it, then")
    print(f"  call memory_lesson_save with fix as lesson (confidence=0.9)")
    print(f"  so future agents don't need to debug it again.")


if __name__ == "__main__":
    run()
