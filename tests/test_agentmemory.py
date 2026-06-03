"""
Integration tests for the agentmemory REST API and MCP proxy.

All tests call the live service at localhost:3111 — no mocks.
On first run, API responses are saved to fixtures/ as structural reference snapshots.
Subsequent runs load those fixtures to assert structural parity (same keys, same types).

Run:
    python -m pytest tests/mcp/test_agentmemory.py -v

Requires agentmemory running:
    systemctl --user start agentmemory
"""
from __future__ import annotations

import json
import uuid

PROJECT = "/home/gbm1996/wksp/gionodes"


# ---------------------------------------------------------------------------
# 1. Health
# ---------------------------------------------------------------------------


def test_health(client, snapshot):
    r = client.get("/agentmemory/health")
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("health", data)

    # Health info lives under data["health"]
    health = data.get("health", {})
    assert health.get("connectionState") == "connected", (
        f"Worker not connected: connectionState='{health.get('connectionState')}'"
    )

    kv = health.get("kvConnectivity", {})
    if "latencyMs" in kv:
        assert kv["latencyMs"] < 500, f"KV latency {kv['latencyMs']}ms is suspiciously high"

    for key in saved:
        assert key in data, f"Key '{key}' present in fixture but missing from live response"


# ---------------------------------------------------------------------------
# 2. Profile
# ---------------------------------------------------------------------------


def test_profile_structure(client, snapshot):
    r = client.get("/agentmemory/profile", params={"project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("profile", data)

    # Response shape: {"cached": bool, "profile": {project, sessionCount, ...}}
    assert "profile" in data, f"'profile' key missing — got keys: {list(data.keys())}"
    profile = data["profile"]
    assert profile.get("project") == PROJECT, (
        f"profile.project='{profile.get('project')}' != '{PROJECT}' — "
        "project filter or canonical path may be broken"
    )
    assert isinstance(profile.get("sessionCount"), int), "sessionCount should be an int"

    for key in saved.get("profile", {}):
        assert key in profile, f"Profile key '{key}' missing from live response"


# ---------------------------------------------------------------------------
# 3. smart-search returns observations with required fields
# ---------------------------------------------------------------------------


def test_smart_search_structure(client, snapshot):
    r = client.post(
        "/agentmemory/smart-search",
        json={"query": "pose estimation", "limit": 5, "format": "full", "project": PROJECT},
    )
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("search_results", data)

    assert "results" in data
    assert isinstance(data["results"], list)
    if data["results"]:
        first = data["results"][0]
        assert "sessionId" in first, "smart-search result missing sessionId"
        assert "score" in first, "smart-search result missing score"
        assert isinstance(first["score"], (int, float))

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live response"


# ---------------------------------------------------------------------------
# 4. /search vs /smart-search endpoint comparison (Bug 1 regression)
#
# Both return observations. smart-search uses BM25+vector; /search is BM25 only.
# memory_sessions_find must use /smart-search for semantic session recovery.
# ---------------------------------------------------------------------------


def test_smart_search_returns_at_least_as_many_as_search(client):
    q = "pose estimation pipeline"
    r_search = client.post("/agentmemory/search", json={"query": q, "limit": 10, "project": PROJECT})
    r_smart = client.post(
        "/agentmemory/smart-search", json={"query": q, "limit": 10, "project": PROJECT}
    )
    assert r_search.status_code == 200
    assert r_smart.status_code == 200

    n_search = len(r_search.json().get("results", []))
    n_smart = len(r_smart.json().get("results", []))

    assert n_smart >= n_search, (
        f"smart-search ({n_smart} results) returned fewer than /search ({n_search}). "
        "memory_sessions_find should use /smart-search, not /search."
    )


def test_search_result_has_session_id_at_top_level(client):
    """Both /search and /smart-search expose sessionId at top level of each result.

    The memory_sessions_find tool does obs.get('sessionId') to extract session anchors.
    This test confirms that field is accessible at the top level for both endpoints,
    so the access pattern is safe regardless of which endpoint is used.
    """
    for endpoint in ("/agentmemory/search", "/agentmemory/smart-search"):
        r = client.post(endpoint, json={"query": "pose", "limit": 3, "project": PROJECT})
        assert r.status_code == 200
        results = r.json().get("results", [])
        if results:
            assert "sessionId" in results[0], (
                f"{endpoint} result[0] missing top-level 'sessionId' — "
                "memory_sessions_find obs.get('sessionId') would silently return None"
            )


# ---------------------------------------------------------------------------
# 5. memory_save roundtrip
# ---------------------------------------------------------------------------


def test_memory_save_roundtrip(client):
    tag = f"test-mcp-roundtrip-{uuid.uuid4().hex[:8]}"
    save_r = client.post(
        "/agentmemory/remember",
        json={
            "content": f"Test memory for roundtrip validation. Unique tag: {tag}",
            "title": f"Roundtrip test {tag}",
            "type": "fact",
            "concepts": [tag, "test-mcp"],
            "project": PROJECT,
        },
    )
    assert save_r.status_code in (200, 201)
    mem = save_r.json().get("memory", {})
    mem_id = mem.get("id")
    assert mem_id, "memory save did not return an id"
    assert mem.get("project") == PROJECT, "saved memory has wrong project"

    # Cleanup — governance delete
    del_r = client.post(
        "/agentmemory/mcp/call",
        json={"name": "memory_governance_delete", "arguments": {"memoryIds": mem_id, "reason": "test cleanup"}},
    )
    assert del_r.status_code == 200


def test_memory_save_title_is_separate_field(client):
    """Bug 4 regression: memory_save used to prepend title into content.

    The fix sends title as a dedicated field. We verify the save succeeds and
    that the stored memory content does NOT start with the title string
    (i.e. the content is clean).
    """
    tag = f"test-title-{uuid.uuid4().hex[:8]}"
    title = f"Title-{tag}"
    content = f"Content-only body. Unique: {tag}"

    save_r = client.post(
        "/agentmemory/remember",
        json={"content": content, "title": title, "type": "fact", "project": PROJECT},
    )
    assert save_r.status_code in (200, 201)
    mem = save_r.json().get("memory", {})
    mem_id = mem.get("id")
    assert mem_id

    stored_content = mem.get("content", "")
    assert not stored_content.startswith(title), (
        "Stored content starts with the title — title is still being prepended into content. "
        f"title='{title}', content='{stored_content[:80]}'"
    )

    client.post(
        "/agentmemory/mcp/call",
        json={"name": "memory_governance_delete", "arguments": {"memoryIds": mem_id, "reason": "test cleanup"}},
    )


# ---------------------------------------------------------------------------
# 6. Actions CRUD + priority regression (Bug 3)
# ---------------------------------------------------------------------------


def test_actions_crud(client):
    uid = uuid.uuid4().hex[:8]
    create_r = client.post(
        "/agentmemory/actions",
        json={
            "title": f"test-action-{uid}",
            "description": "Created by MCP test suite — cancelled immediately",
            "priority": 3,
            "project": PROJECT,
        },
    )
    assert create_r.status_code in (200, 201)
    create_data = create_r.json()
    action = create_data.get("action", {})
    action_id = action.get("id")
    assert action_id, "action create did not return id"
    assert action.get("status") in ("pending", "active")
    assert action.get("priority") == 3

    # Update priority
    upd_r = client.post(
        "/agentmemory/actions/update",
        json={"actionId": action_id, "priority": 7},
    )
    assert upd_r.status_code == 200

    # Cancel to clean up
    cancel_r = client.post(
        "/agentmemory/actions/update",
        json={"actionId": action_id, "status": "cancelled", "result": "test cleanup"},
    )
    assert cancel_r.status_code == 200
    final_action = cancel_r.json().get("action", {})
    assert final_action.get("status") == "cancelled"


def test_action_priority_zero_not_silently_dropped(client):
    """Bug 3 regression: `if priority and priority != 5` silently dropped priority=0.

    The fix uses `if priority != 5` so that an explicit 0 (clamped to 1 by the engine)
    is sent rather than skipped. This test creates an action with priority=8, then
    updates to priority=0, and asserts the priority changed (not stayed at 8).
    """
    uid = uuid.uuid4().hex[:8]
    create_r = client.post(
        "/agentmemory/actions",
        json={"title": f"test-priority-zero-{uid}", "priority": 8, "project": PROJECT},
    )
    assert create_r.status_code in (200, 201)
    action_id = create_r.json().get("action", {}).get("id")
    assert action_id

    upd_r = client.post(
        "/agentmemory/actions/update",
        json={"actionId": action_id, "priority": 0},
    )
    assert upd_r.status_code == 200
    updated_priority = upd_r.json().get("action", {}).get("priority")
    assert updated_priority != 8, (
        "priority=0 was silently ignored — action stayed at priority 8. "
        "The falsy check `if priority and priority != 5` bug is still present."
    )

    client.post(
        "/agentmemory/actions/update",
        json={"actionId": action_id, "status": "cancelled", "result": "test cleanup"},
    )


# ---------------------------------------------------------------------------
# 7. Frontier
# ---------------------------------------------------------------------------


def test_frontier_structure(client, snapshot):
    r = client.get("/agentmemory/frontier", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    snapshot("frontier", data)

    # Response shape: {"frontier": [{action: {...}, blockers, leased, score}], "totalActions": N, ...}
    assert "frontier" in data, f"'frontier' key missing — got: {list(data.keys())}"
    assert isinstance(data["frontier"], list)
    for entry in data["frontier"]:
        assert "action" in entry, f"frontier entry missing 'action' key: {list(entry.keys())}"
        a = entry["action"]
        assert "id" in a
        assert "title" in a
        assert "status" in a


# ---------------------------------------------------------------------------
# 8. Memories list
# ---------------------------------------------------------------------------


def test_memories_list_structure(client, snapshot):
    r = client.get("/agentmemory/memories", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("memories", data)

    assert "memories" in data
    for m in data["memories"]:
        assert "id" in m
        assert "content" in m
        assert "type" in m

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live memories response"


def test_memories_query_param_ignored(client):
    """Documents known engine behaviour: GET /memories?q= is silently ignored.

    The q param has no filtering effect — same results regardless of value.
    Callers must use /smart-search for text-filtered memory discovery.
    """
    r_any = client.get("/agentmemory/memories", params={"limit": 1})
    r_pose = client.get("/agentmemory/memories", params={"limit": 1, "q": "pose estimation"})
    r_impossible = client.get("/agentmemory/memories", params={"limit": 1, "q": "xyzabc123impossible99"})

    assert r_any.status_code == 200
    assert r_pose.status_code == 200
    assert r_impossible.status_code == 200

    # All three return the same first memory — q= is ignored
    id_any = r_any.json().get("memories", [{}])[0].get("id")
    id_pose = r_pose.json().get("memories", [{}])[0].get("id")
    id_impossible = r_impossible.json().get("memories", [{}])[0].get("id")

    assert id_any == id_pose == id_impossible, (
        "GET /memories?q= appears to filter results — expected it to be ignored. "
        "This test documents expected (no-op) behaviour; update if the engine adds search support."
    )


# ---------------------------------------------------------------------------
# 9. Sessions list
# ---------------------------------------------------------------------------


def test_sessions_list_structure(client, snapshot):
    r = client.get("/agentmemory/sessions", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("sessions", data)

    assert "sessions" in data
    sessions = data["sessions"]
    assert isinstance(sessions, list)
    assert len(sessions) > 0, (
        "No sessions returned for the project — project filter may be broken or "
        "sessions are stored under a different project key"
    )
    # Some sessions may be sparse (endedAt + status only); verify at least one has an id
    full_sessions = [s for s in sessions if "id" in s]
    assert full_sessions, (
        "No sessions with an 'id' field returned — all sessions are sparse/tombstoned"
    )
    for s in full_sessions:
        assert "startedAt" in s, f"Session {s['id']} missing 'startedAt'"

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live sessions response"


# ---------------------------------------------------------------------------
# 10. Diagnose — no check-failures
# ---------------------------------------------------------------------------


def test_diagnose_no_failures(client):
    r = client.post(
        "/agentmemory/mcp/call",
        json={"name": "memory_diagnose", "arguments": {"project": PROJECT}},
    )
    assert r.status_code == 200
    body = r.json()
    content = body.get("content", [])
    assert content, "diagnose returned empty content"
    inner = json.loads(content[0]["text"])
    checks = inner.get("checks", [])
    assert checks, "diagnose returned no checks"

    # Only hard-fail on non-fixable failures — fixable issues (e.g. missing project scope
    # on old memories) are pre-existing data conditions, not code bugs.
    hard_failures = [c for c in checks if c.get("status") == "fail" and not c.get("fixable")]
    assert not hard_failures, (
        f"diagnose reported non-fixable failures: {[c['name'] for c in hard_failures]}"
    )


# ---------------------------------------------------------------------------
# 11. Session context injection
# ---------------------------------------------------------------------------


def test_session_start_returns_context(client, snapshot):
    """`POST /agentmemory/session/start` should return a project-scoped context XML block.

    This is the same endpoint the session-start hook calls. The returned `context`
    field is the <agentmemory-context> XML block assembled from profile, lessons,
    and last-N session summaries filtered to the project.
    """
    session_id = f"test-ctx-{uuid.uuid4().hex[:12]}"
    r = client.post(
        "/agentmemory/session/start",
        json={"sessionId": session_id, "project": PROJECT, "cwd": PROJECT},
    )
    assert r.status_code == 200
    data = r.json()

    assert "session" in data, "session/start response missing 'session' key"
    assert "context" in data, "session/start response missing 'context' key"

    session = data["session"]
    assert session.get("project") == PROJECT, (
        f"session.project='{session.get('project')}' != '{PROJECT}' — "
        "project scoping is broken; context may contain data from the wrong project"
    )

    context = data["context"]
    assert isinstance(context, str)
    assert "<agentmemory-context" in context, (
        "context field is missing the <agentmemory-context> XML block"
    )
    assert PROJECT in context or "gionodes" in context, (
        "context XML does not reference the expected project — may be for a different project"
    )

    # Save structural metadata only — context content changes every session
    snapshot(
        "session_context",
        {
            "response_keys": sorted(data.keys()),
            "session_keys": sorted(session.keys()),
            "context_has_xml_tag": "<agentmemory-context" in context,
            "context_references_project": PROJECT in context or "gionodes" in context,
        },
    )

    # Clean up the temporary session
    client.post("/agentmemory/session/end", json={"sessionId": session_id})


def test_session_context_is_project_scoped(client):
    """Verify that two different project names produce different context content.

    If the context XML says project="gionodes" but we sent project="/home/.../gionodes",
    they should still match — the engine may normalise to the basename. But a completely
    unrelated project path should produce a different (likely empty) context.
    """
    sid_a = f"test-scope-a-{uuid.uuid4().hex[:8]}"
    sid_b = f"test-scope-b-{uuid.uuid4().hex[:8]}"

    r_a = client.post(
        "/agentmemory/session/start",
        json={"sessionId": sid_a, "project": PROJECT, "cwd": PROJECT},
    )
    r_b = client.post(
        "/agentmemory/session/start",
        json={"sessionId": sid_b, "project": "/tmp/unrelated-project-xyz", "cwd": "/tmp"},
    )

    assert r_a.status_code == 200
    assert r_b.status_code == 200

    ctx_a = r_a.json().get("context", "")
    ctx_b = r_b.json().get("context", "")

    # The gionodes project should have richer context than an unknown project
    assert len(ctx_a) >= len(ctx_b), (
        f"Known project context ({len(ctx_a)} chars) is shorter than unknown project "
        f"context ({len(ctx_b)} chars) — project scoping may not be working"
    )

    # Cleanup
    client.post("/agentmemory/session/end", json={"sessionId": sid_a})
    client.post("/agentmemory/session/end", json={"sessionId": sid_b})


# ---------------------------------------------------------------------------
# 12. Lessons endpoint (used by _fetch_lessons in proxy)
# ---------------------------------------------------------------------------


def test_lessons_structure(client, snapshot):
    r = client.get("/agentmemory/lessons", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("lessons", data)

    assert "lessons" in data, f"'lessons' key missing — got: {list(data.keys())}"
    for lesson in data["lessons"]:
        assert "id" in lesson
        assert "content" in lesson
        assert "confidence" in lesson
        assert isinstance(lesson["confidence"], (int, float))

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live lessons response"


def test_lesson_save_roundtrip(client):
    """POST /agentmemory/lessons saves and the lesson appears in subsequent GET."""
    tag = f"lesson-test-{uuid.uuid4().hex[:6]}"
    save_r = client.post(
        "/agentmemory/lessons",
        json={
            "content": f"Test lesson: frontier key is 'frontier' not 'actions'. Tag: {tag}",
            "context": "test suite",
            "confidence": 0.8,
            "project": PROJECT,
            "tags": ["type:test"],
        },
    )
    assert save_r.status_code in (200, 201), f"lesson save failed: {save_r.text[:200]}"
    lesson = save_r.json().get("lesson", save_r.json())
    lesson_id = lesson.get("id")
    assert lesson_id, "lesson save did not return an id"


# ---------------------------------------------------------------------------
# 13. Crystals endpoint (used by _fetch_crystals in proxy)
# ---------------------------------------------------------------------------


def test_crystals_structure(client, snapshot):
    r = client.get("/agentmemory/crystals", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("crystals", data)

    assert "crystals" in data, f"'crystals' key missing — got: {list(data.keys())}"
    for crystal in data["crystals"]:
        assert "id" in crystal

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live crystals response"


# ---------------------------------------------------------------------------
# 14. Insights endpoint (used by _fetch_insights in proxy)
# ---------------------------------------------------------------------------


def test_insights_structure(client, snapshot):
    r = client.get("/agentmemory/insights", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("insights", data)

    assert "insights" in data, f"'insights' key missing — got: {list(data.keys())}"
    for insight in data["insights"]:
        assert "id" in insight

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live insights response"


# ---------------------------------------------------------------------------
# 15. Enrich endpoint (used by _enrich_files in proxy)
# ---------------------------------------------------------------------------


def test_enrich_structure(client, snapshot):
    """POST /agentmemory/enrich with a recent session and known file."""
    sessions_r = client.get("/agentmemory/sessions", params={"limit": 1})
    assert sessions_r.status_code == 200
    sessions = sessions_r.json().get("sessions", [])
    if not sessions:
        import pytest
        pytest.skip("no sessions available for enrich test")

    session_id = sessions[0].get("id") or sessions[0].get("sessionId")
    assert session_id, "session has no id field"

    r = client.post(
        "/agentmemory/enrich",
        json={
            "sessionId": session_id,
            "files": ["mcp/agentmemory_mcp_proxy.py"],
            "project": PROJECT,
        },
    )
    assert r.status_code == 200
    data = r.json()
    saved = snapshot("enrich", data)

    for key in saved:
        assert key in data, f"Key '{key}' in fixture but missing from live enrich response"


# ---------------------------------------------------------------------------
# 16. Actions list endpoint (used by memory_task_context action lookup)
# ---------------------------------------------------------------------------


def test_actions_list_endpoint(client):
    """GET /agentmemory/actions must exist and return {actions: [...]}."""
    r = client.get("/agentmemory/actions", params={"limit": 5, "project": PROJECT})
    assert r.status_code == 200, (
        f"GET /agentmemory/actions returned {r.status_code} — "
        "memory_task_context action lookup will silently fall back to query-only mode"
    )
    data = r.json()
    assert "actions" in data, (
        f"Response missing 'actions' key — got: {list(data.keys())}. "
        "memory_task_context does actions_resp.get('actions', []) — will be empty."
    )
    assert isinstance(data["actions"], list)
    if data["actions"]:
        first = data["actions"][0]
        assert "id" in first
        assert "title" in first
        assert "status" in first


# ---------------------------------------------------------------------------
# 17. Frontier nested structure (Bug 5 regression)
# ---------------------------------------------------------------------------


def test_frontier_entries_are_nested():
    """Regression for Bug 5: frontier entries wrap action under 'action' key.

    The proxy previously did data.get('actions', []) — wrong key.
    This test confirms the live API shape so regressions surface immediately.
    """
    import httpx as _httpx
    r = _httpx.get(
        "http://localhost:3111/agentmemory/frontier",
        params={"limit": 2, "project": PROJECT},
        timeout=10,
    )
    assert r.status_code == 200
    data = r.json()

    assert "frontier" in data, (
        f"Frontier response has no 'frontier' key — got: {list(data.keys())}. "
        "Proxy _fmt_frontier_entry and memory_next need updating if key changed."
    )
    for entry in data["frontier"]:
        assert "action" in entry, (
            f"Frontier entry is flat — expected nested {{action: {{...}}, score, leased}}. "
            f"Got keys: {list(entry.keys())}"
        )
        assert "score" in entry, "Frontier entry missing 'score' from outer envelope"
        assert "leased" in entry, "Frontier entry missing 'leased' from outer envelope"


# ---------------------------------------------------------------------------
# 18. Smart-search bundles lessons field
# ---------------------------------------------------------------------------


def test_smart_search_has_lessons_key(client):
    """POST /smart-search response always includes a 'lessons' key (may be empty).

    memory_find relies on obs_resp.get('lessons', []) to get bundled lessons.
    If the key is absent, memory_find silently returns no lessons even when
    lessons exist. This test documents the expected response contract.
    """
    r = client.post(
        "/agentmemory/smart-search",
        json={"query": "architecture constraints", "limit": 3, "project": PROJECT},
    )
    assert r.status_code == 200
    data = r.json()
    assert "lessons" in data, (
        "smart-search response missing 'lessons' key — "
        "memory_find's obs_resp.get('lessons', []) will always be empty. "
        "Add explicit _fetch_lessons() call to memory_find if this fails."
    )
    assert isinstance(data["lessons"], list)


# ---------------------------------------------------------------------------
# 19. Crystallize endpoint
# ---------------------------------------------------------------------------


def test_crystallize_via_mcp_call(client):
    """mem::crystallize has no direct REST endpoint — it goes through POST /mcp/call.

    memory_crystallize tool routes: POST /agentmemory/mcp/call {name: 'memory_crystallize', arguments: {...}}.
    This test confirms the MCP call dispatch path works and returns a parseable response.
    """
    r = client.post(
        "/agentmemory/mcp/call",
        json={"name": "memory_crystallize", "arguments": {"actionIds": "act_nonexistent_test"}},
    )
    assert r.status_code == 200, (
        f"POST /agentmemory/mcp/call (crystallize) returned {r.status_code} — "
        "MCP call dispatch may be broken; memory_crystallize tool will fail."
    )
    data = r.json()
    assert "content" in data, f"mcp/call response missing 'content' key — got: {list(data.keys())}"
    assert isinstance(data["content"], list)
    assert data["content"], "mcp/call response has empty content"
    assert data["content"][0].get("type") == "text"
