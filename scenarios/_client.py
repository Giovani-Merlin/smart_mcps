"""
Synchronous HTTP client for agentmemory REST API — debug scenarios only.

Run any scenario with:
    python mcp/agentmemory/scenario_X.py

Requires agentmemory running:
    systemctl --user start agentmemory

━━━ Endpoint quick-reference (PRACTICAL MINIMAL PAYLOADS) ━━━━━━━━━━━━━━━━━
  ▓▓▓ SESSION START (call these once at the beginning of a session)
  GET  /agentmemory/health                 service health check
  GET  /agentmemory/profile                stable project snapshot
                                             minimal: {project}; optional: refresh=true (re-compute)
                                             returns: {topConcepts, topFiles, conventions}
  POST /agentmemory/context                "previously on this project" briefing (recency-based)
                                             minimal: {sessionId, project}; optional: budget (tokens, default 2000)

  ▓▓▓ PRIMARY RECALL (ordered by typical agent workflow)
  POST /agentmemory/lessons/search         behavioral rules BEFORE taking actions (REST, not MCP wrapper)
                                             minimal: {query, project, minConfidence: 0.3, limit: 5}
                                             scoring: confidence × term_overlap × recency_decay (NOT BM25)
                                             NOTE: minConfidence and limit must be NUMBERS (not strings)
  POST /agentmemory/smart-search           PRIMARY SCORED ENTRY POINT — episodic obs + memories
                                             minimal: {query, project, limit}
                                             optional: includeLessons=true (bundle lesson hits inline)
                                             optional: expandIds=[...] (direct KV fetch, skips scoring, max 20)
                                             returns: {results: CompressedObservation[], lessons: Lesson[]}
                                             NOTE: results[] mixes obs AND memories (memories: type="decision")
                                             NOTE: only CompressedObservations are in BM25/vector indexes
  POST /agentmemory/search                 session discovery — find related sessions by semantic content
                                             minimal: {query, project, format: "compact", limit}
                                             returns obs hits with sessionId anchors
                                             follow-up: GET /sessions → GET /crystals?project=...
  POST /agentmemory/graph/query            architectural context (file/function/concept neighbors)
                                             minimal: {query, project, maxDepth: 2}  (NOT 'depth')
                                             optional: nodeType ("file"|"function"|"concept"|"decision"|...)
                                             returns: GraphNode[] — NOT Memory objects (needs second fetch)
                                             (only useful if GRAPH_EXTRACTION_ENABLED=true)
  POST /agentmemory/insights/search        high-level synthesized patterns (planning only)
                                             minimal: {query, project, minConfidence: 0.5, limit: 5}
                                             NOTE: no query-relevance scorer — keyword filter + confidence sort
  GET  /agentmemory/insights               list insights without query (alternative to insights/search)
                                             minimal: {project, minConfidence: 0.5, limit: 10}

  ▓▓▓ FOLLOW-UP ENDPOINTS (after finding a scored observation hit)
  GET  /agentmemory/memories               list all memories for client-side filtering
                                             minimal: {project, limit: 100}
                                             returns Memory[] with sourceObservationIds, files, concepts
  GET  /agentmemory/sessions               list session records (summary, firstPrompt, status)
                                             minimal: {limit: 20}
  GET  /agentmemory/crystals               session narrative summaries (keyOutcomes, filesAffected)
                                             minimal: {project}; optional: sessionId (filter)
  POST /agentmemory/enrich                 file-scoped bug history + bridging memories
                                             minimal: {sessionId, files: [path], project}
  POST /agentmemory/timeline               chronological observations window around an event
                                             minimal: {anchor: keyword, project, before: 4, after: 4}

  ▓▓▓ ACTION/TASK MANAGEMENT
  POST /agentmemory/actions                create task (minimal: {title, project})
  POST /agentmemory/actions/update         update action status (minimal: {actionId, status})
  GET  /agentmemory/frontier               unblocked high-priority tasks (minimal: {project, limit: 5})

  ▓▓▓ WRITE ENDPOINTS
  POST /agentmemory/remember               save curated memory
                                             minimal: {content, type, project, concepts, files}
                                             NOTE: skip sourceObservationIds — consolidation stamps it
  POST /agentmemory/observe                create observation manually (for testing/logging)
                                             minimal: {hookType, sessionId, project, cwd, timestamp, data}
  POST /agentmemory/lessons                save behavioral lesson
                                             minimal: {content, project, confidence, source: "manual", tags}
  POST /agentmemory/lessons/strengthen     reinforce a lesson's confidence (prevent decay)
                                             minimal: {lessonId}
  POST /agentmemory/session/end            end session (minimal: {sessionId})
  POST /agentmemory/mcp/call               MCP wrapper (use direct REST endpoints above instead)
                                             name="memory_crystallize": args: {actionIds, project?, sessionId?}

  ▓▓▓ CLEANUP / GOVERNANCE
  POST /agentmemory/forget                 remove observations from KV + search indexes (synchronous)
                                             body: {sessionId} or {observationIds: [...]}
  DELETE /agentmemory/governance/memories  hard-delete memories with audit trail
                                             body: {memoryIds: [...], reason: "..."}
  GET  /agentmemory/export                 full export including tombstoned records
                                             minimal: {maxSessions: N}; lessons with deleted:true appear here

━━━ MCP call functions (POST /agentmemory/mcp/call) ━━━━━━━━━━━━━━━━━━━━━━━
  All argument values must be strings (MCP protocol constraint).

  memory_crystallize      args: {actionIds (comma-sep), project?, sessionId?}
  memory_sentinel_create  args: {name, gatedActionIds?, expiresInMs?,
                                  type ("webhook"|"timer"|"threshold"|"pattern"|"approval"|"custom")}
  memory_sentinel_trigger args: {sentinelId, result?}
  memory_signal_emit      args: {event, payload? (JSON string)}
  memory_lesson_recall    args: {query, project?, minConfidence?, limit?}
                          NOTE: prefer POST /lessons/search (direct REST) over this MCP wrapper
  memory_file_history     args: {files (comma-sep paths), sessionId?}
  memory_relations        args: {memoryId, maxHops? (default "2"), minConfidence? (default "0")}
  memory_diagnose         args: {categories? (comma-sep)}
  memory_reflect          args: {project?, maxClusters?}
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx

BASE_URL = "http://localhost:3111"
# PROJECT must match the identifier used when sessions were started on this instance.
# Check with: GET /agentmemory/sessions → look at the 'project' field.
# This instance uses "smart_mcps". Change to match your target project.
PROJECT = "smart_mcps"

_http = httpx.Client(base_url=BASE_URL, timeout=30.0)

_WIDTH = 72


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def call(
    method: str, path: str, params: dict | None = None, body: dict | None = None
) -> dict:
    """
    Synchronous REST call to agentmemory.

    Args:
        method: "GET" | "POST" | ...
        path:   e.g. "/agentmemory/smart-search"
        params: query-string dict (GET)
        body:   JSON body dict (POST)

    Exits on connection error, raises httpx.HTTPStatusError on 4xx/5xx.
    """
    try:
        resp = _http.request(method, path, params=params, json=body)
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        print(f"\n[ERROR] agentmemory unreachable at {BASE_URL}")
        print("  Run: systemctl --user start agentmemory")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"\n[HTTP {e.response.status_code}] {method} {path}")
        try:
            print(f"  body: {json.dumps(e.response.json(), indent=2)[:400]}")
        except Exception:
            print(f"  body: {e.response.text[:400]}")
        raise


def mcp_call(name: str, arguments: dict[str, str]) -> Any:
    """
    Call a mem:: function via POST /agentmemory/mcp/call.

    Endpoint: POST /agentmemory/mcp/call
    Body:     {"name": "<name>", "arguments": {<key>: <str_value>}}

    Note: MCP protocol requires all argument values to be strings.
    Response envelope: {content: [{type: "text", text: "<json>"}]}
    This function unwraps the inner JSON automatically.

    Example:
        mcp_call("memory_crystallize", {"actionIds": "act_1,act_2", "project": PROJECT})
        mcp_call("memory_lesson_recall", {"query": "authentication routing", "limit": "5"})
        mcp_call("memory_signal_emit", {"event": "ci.passed", "payload": '{"pr": "123"}'})
    """
    resp = call(
        "POST", "/agentmemory/mcp/call", body={"name": name, "arguments": arguments}
    )
    content = resp.get("content", [])
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except Exception:
            return {"raw": content[0]["text"]}
    return resp


def check_health() -> bool:
    """Verify agentmemory is reachable. Exits on failure."""
    r = call("GET", "/agentmemory/health")
    print(f"[health] status={r.get('status', '?')}  version={r.get('version', '?')}")
    return True


def get_latest_session_id() -> str | None:
    """Fetch the most recent session ID — needed for /enrich."""
    resp = call("GET", "/agentmemory/sessions", params={"limit": 1})
    sessions = resp.get("sessions", [])
    if not sessions:
        return None
    s = sessions[0]
    return s.get("id") or s.get("sessionId")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def banner(title: str) -> None:
    """Print a scenario title banner."""
    pad = max(0, (_WIDTH - len(title) - 4) // 2)
    b = "═" * _WIDTH
    print(f"\n{b}")
    print(f"{'═' * pad}  {title}  {'═' * pad}")
    print(b)


def step(n: int, title: str) -> None:
    """Print a numbered step divider."""
    print(f"\n{'─' * _WIDTH}")
    print(f"  STEP {n}: {title}")
    print(f"{'─' * _WIDTH}")


def pp(data: Any, label: str = "", truncate: int = 1000) -> None:
    """Pretty-print a dict/list with optional label and length cap."""
    if label:
        print(f"\n  [{label}]")
    text = json.dumps(data, indent=2, default=str)
    if truncate and len(text) > truncate:
        text = text[:truncate] + f"\n  ... (+{len(text) - truncate} chars)"
    for line in text.splitlines():
        print(f"    {line}")


def print_obs_summary(results: list, label: str = "observations") -> None:
    """Print a compact table of observation hits."""
    print(f"\n  [{label}]  ({len(results)} hits)")
    for i, r in enumerate(results[:8]):
        score = r.get("score", r.get("relevanceScore", 0))
        obs_type = r.get("type", "?")
        title = (r.get("title") or "")[:60]
        obs_id = (r.get("id") or r.get("obsId") or "")[:20]
        print(f'    {i+1:2}. [{obs_type:12}] score={score:.3f}  id={obs_id}  "{title}"')


def print_frontier(frontier: list) -> None:
    """Print frontier actions as a compact table."""
    print(f"\n  [frontier]  ({len(frontier)} actions)")
    for i, entry in enumerate(frontier[:6]):
        action = entry.get("action", entry)
        score = entry.get("score", "?")
        leased = entry.get("leased", False)
        status = action.get("status", "?")
        priority = action.get("priority", "?")
        title = (action.get("title") or "")[:50]
        act_id = (action.get("id") or "")[:20]
        print(f"    {i+1}. [{status:8}] pri={priority}  score={score}  leased={leased}")
        print(f"       id={act_id}")
        print(f'       "{title}"')
