"""
Tests for the agentmemory FastMCP proxy tool functions.

These tests import the proxy's async functions directly and call them via asyncio.run().
They validate:
  - Response structure produced by proxy formatters (_fmt_observation, _fmt_action, etc.)
  - Bug regressions (Bug 5: frontier key mismatch)
  - Dispatch correctness (memory_save vs memory_lesson_save hit different endpoints)
  - Pure logic helpers (_follow_memories, _is_useful_observation) — no HTTP

Requires agentmemory running at localhost:3111.

Run:
    python -m pytest tests/mcp/test_mcp_proxy.py -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Proxy module loading
#
# We inject a lightweight stub for fastmcp before loading the proxy so the
# @mcp.tool() and @mcp.prompt() decorators become identity functions and all
# function bodies are preserved without needing a live FastMCP/MCP install.
# ---------------------------------------------------------------------------


class _FakeFastMCP:
    """Stub FastMCP that keeps @mcp.tool() and @mcp.prompt() as identity decorators."""

    def __init__(self, *args, **kwargs):
        pass

    def tool(self):
        def decorator(func):
            return func
        return decorator

    def prompt(self):
        def decorator(func):
            return func
        return decorator

    def run(self):
        pass


_fake_fastmcp_mod = type(sys)("fastmcp")
_fake_fastmcp_mod.FastMCP = _FakeFastMCP
sys.modules.setdefault("fastmcp", _fake_fastmcp_mod)

_PROXY_FILE = Path(__file__).parent.parent / "agentmemory" / "proxy.py"
_spec = importlib.util.spec_from_file_location("agentmemory.proxy", _PROXY_FILE)
_proxy_mod = importlib.util.module_from_spec(_spec)
sys.modules["agentmemory.proxy"] = _proxy_mod
_spec.loader.exec_module(_proxy_mod)  # type: ignore[union-attr]

_fmt_frontier_entry = _proxy_mod._fmt_frontier_entry
_fmt_observation = _proxy_mod._fmt_observation
_follow_memories = _proxy_mod._follow_memories
_is_useful_observation = _proxy_mod._is_useful_observation
memory_find = _proxy_mod.memory_find
memory_lesson_save = _proxy_mod.memory_lesson_save
memory_next = _proxy_mod.memory_next
memory_profile = _proxy_mod.memory_profile
memory_save = _proxy_mod.memory_save
memory_task_context = _proxy_mod.memory_task_context
memory_update_task = _proxy_mod.memory_update_task

PROJECT = "/home/gbm1996/wksp/gionodes"


@pytest.fixture(scope="session", autouse=True)
def skip_if_down():
    try:
        r = httpx.get("http://localhost:3111/agentmemory/health", timeout=3.0)
        r.raise_for_status()
    except Exception:
        pytest.skip("agentmemory service unreachable at localhost:3111")


# ---------------------------------------------------------------------------
# Pure logic — no HTTP
# ---------------------------------------------------------------------------


def test_is_useful_observation_keeps_facts():
    assert _is_useful_observation({"type": "command_run", "facts": ["ran git status"]})


def test_is_useful_observation_drops_command_run_without_facts():
    assert not _is_useful_observation({"type": "command_run", "facts": []})


def test_is_useful_observation_drops_file_read_without_facts():
    assert not _is_useful_observation({"type": "file_read"})


def test_is_useful_observation_keeps_decision_without_facts():
    assert _is_useful_observation({"type": "decision"})


def test_is_useful_observation_keeps_subagent_without_facts():
    """subagent observations carry useful context even when facts[] is empty."""
    assert _is_useful_observation({"type": "subagent"})


def test_is_useful_observation_keeps_other_without_facts():
    assert _is_useful_observation({"type": "other"})


def test_follow_memories_by_obs_id():
    obs = [{"id": "obs1", "sessionId": "s1", "concepts": [], "files": []}]
    memories = [
        {"id": "mem1", "sourceObservationIds": ["obs1"], "concepts": [], "files": [], "strength": 5},
        {"id": "mem2", "sourceObservationIds": ["obs999"], "concepts": [], "files": [], "strength": 3},
    ]
    result = _follow_memories(obs, memories)
    ids = [m["id"] for m in result]
    assert "mem1" in ids
    assert "mem2" not in ids


def test_follow_memories_by_concept():
    obs = [{"id": "obs1", "sessionId": "s1", "concepts": ["SAM", "pose"], "files": []}]
    memories = [
        {"id": "mem_match", "sourceObservationIds": [], "concepts": ["SAM"], "files": [], "strength": 7},
        {"id": "mem_miss", "sourceObservationIds": [], "concepts": ["LTX"], "files": [], "strength": 9},
    ]
    result = _follow_memories(obs, memories)
    ids = [m["id"] for m in result]
    assert "mem_match" in ids
    assert "mem_miss" not in ids


def test_follow_memories_by_file():
    obs = [{"id": "obs1", "sessionId": "s1", "concepts": [], "files": ["mcp/proxy.py"]}]
    memories = [
        {"id": "mem_match", "sourceObservationIds": [], "concepts": [], "files": ["mcp/proxy.py"], "strength": 4},
    ]
    result = _follow_memories(obs, memories)
    assert result[0]["id"] == "mem_match"


def test_follow_memories_sorted_by_strength():
    obs = [{"id": "obs1", "sessionId": "s1", "concepts": ["X"], "files": []}]
    memories = [
        {"id": "weak", "sourceObservationIds": [], "concepts": ["X"], "files": [], "strength": 1},
        {"id": "strong", "sourceObservationIds": [], "concepts": ["X"], "files": [], "strength": 10},
    ]
    result = _follow_memories(obs, memories)
    assert result[0]["id"] == "strong"


def test_fmt_frontier_entry_flat():
    """_fmt_frontier_entry unpacks nested {action, score, leased} envelope."""
    entry = {
        "action": {"id": "act_1", "title": "Do thing", "status": "pending", "priority": 8,
                   "tags": [], "description": "desc", "result": None, "createdAt": "2026-01-01"},
        "score": 99.5,
        "leased": False,
        "blockers": [],
    }
    fmt = _fmt_frontier_entry(entry)
    assert fmt["id"] == "act_1"
    assert fmt["score"] == 99.5
    assert fmt["leased"] is False
    assert "blockers" not in fmt  # empty blockers are omitted


def test_fmt_frontier_entry_includes_blockers():
    entry = {
        "action": {"id": "act_2", "title": "t", "status": "blocked", "priority": 5,
                   "tags": [], "description": "", "result": None, "createdAt": "2026-01-01"},
        "score": 10.0,
        "leased": True,
        "blockers": ["act_1"],
    }
    fmt = _fmt_frontier_entry(entry)
    assert fmt["blockers"] == ["act_1"]
    assert fmt["leased"] is True


def test_fmt_observation_truncation():
    obs = {
        "id": "obs_abc",
        "sessionId": "sess_1",
        "type": "decision",
        "title": "A" * 200,
        "facts": ["f1", "f2", "f3", "f4", "f5", "f6"],
        "concepts": ["c1", "c2", "c3", "c4", "c5", "c6", "c7"],
        "files": ["file1", "file2", "file3", "file4", "file5", "file6"],
        "importance": 0.9,
        "score": 0.75,
    }
    fmt = _fmt_observation(obs, 0.75)
    assert len(fmt["title"]) == 100
    assert len(fmt["facts"]) == 4
    assert len(fmt["concepts"]) == 6
    assert len(fmt["files"]) == 5
    assert fmt["score"] == 0.75


# ---------------------------------------------------------------------------
# Dispatch correctness — monkey-patch _call to check endpoint routing
# ---------------------------------------------------------------------------


def test_memory_save_dispatches_to_remember():
    """`memory_save` must POST to /agentmemory/remember, NOT /agentmemory/lessons."""
    captured = {}

    async def fake_call(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = kwargs.get("json", {})
        return {"memory": {"id": "mem_test", "project": PROJECT}}

    with patch("agentmemory.proxy._call", new=fake_call):
        asyncio.run(memory_save(content="test content", project=PROJECT))

    assert captured["path"] == "/agentmemory/remember", (
        f"memory_save posted to {captured['path']!r} — expected /agentmemory/remember"
    )
    assert "content" in captured["body"]
    assert "title" not in captured["body"] or captured["body"].get("content") == "test content"


def test_memory_lesson_save_dispatches_to_lessons():
    """`memory_lesson_save` must POST to /agentmemory/lessons, NOT /agentmemory/remember."""
    captured = {}

    async def fake_call(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = kwargs.get("json", {})
        return {"lesson": {"id": "les_test"}}

    with patch("agentmemory.proxy._call", new=fake_call):
        asyncio.run(memory_lesson_save(content="test lesson", confidence=0.8, project=PROJECT))

    assert captured["path"] == "/agentmemory/lessons", (
        f"memory_lesson_save posted to {captured['path']!r} — expected /agentmemory/lessons"
    )
    assert captured["body"].get("confidence") == 0.8


def test_memory_save_title_as_separate_field():
    """memory_save must send title as a separate field, not prepended to content (Bug 4 regression)."""
    captured = {}

    async def fake_call(method, path, **kwargs):
        captured["body"] = kwargs.get("json", {})
        return {"memory": {"id": "m1"}}

    with patch("agentmemory.proxy._call", new=fake_call):
        asyncio.run(memory_save(content="my content", title="My Title", project=PROJECT))

    body = captured["body"]
    assert body.get("title") == "My Title"
    assert body.get("content") == "my content"
    assert not body["content"].startswith("My Title"), (
        "content starts with title — title is still being prepended (Bug 4 still present)"
    )


# ---------------------------------------------------------------------------
# Live integration — call proxy tools against real service
# ---------------------------------------------------------------------------


def test_proxy_memory_find_returns_observations():
    result = asyncio.run(memory_find(query="pose estimation", project=PROJECT, limit=5))
    assert "observations" in result, f"memory_find missing 'observations' key — got: {list(result.keys())}"
    assert isinstance(result["observations"], list)


def test_proxy_memory_find_observation_fields():
    result = asyncio.run(memory_find(query="SAM segmentation", project=PROJECT, limit=3))
    for obs in result.get("observations", []):
        assert "id" in obs
        assert "type" in obs
        assert "score" in obs
        assert "sessionId" in obs
        assert len(obs.get("title", "")) <= 100
        assert len(obs.get("facts", [])) <= 4
        assert len(obs.get("concepts", [])) <= 6
        assert len(obs.get("files", [])) <= 5


def test_proxy_memory_next_returns_nonempty_actions():
    """Regression for Bug 5: memory_next was always returning [] before the frontier key fix."""
    result = asyncio.run(memory_next(project=PROJECT, limit=3, include_context=False))
    actions = result.get("actions", [])
    assert isinstance(actions, list)
    assert len(actions) > 0, (
        "memory_next returned no actions — frontier key bug may have regressed. "
        "Check that proxy reads data.get('frontier', []) not data.get('actions', [])."
    )


def test_proxy_memory_next_actions_have_score():
    """Each action from memory_next must include 'score' from the frontier envelope."""
    result = asyncio.run(memory_next(project=PROJECT, limit=2, include_context=False))
    for action in result.get("actions", []):
        assert "score" in action, (
            f"Action {action.get('id')} missing 'score' — "
            "_fmt_frontier_entry must extract score from outer envelope."
        )
        assert "id" in action
        assert "title" in action
        assert "status" in action


def test_proxy_memory_profile_has_frontier():
    """Regression for Bug 5 in memory_profile: frontier was always [] before the fix."""
    result = asyncio.run(memory_profile(project=PROJECT, include_frontier=True))
    assert "frontier" in result, (
        "memory_profile missing 'frontier' key — "
        "frontier key bug may have regressed in memory_profile."
    )
    frontier = result["frontier"]
    assert isinstance(frontier, list)
    assert len(frontier) > 0, "memory_profile frontier is empty — key bug or no pending actions"
    assert "score" in frontier[0], "frontier entry missing 'score' field from envelope"


def test_proxy_memory_profile_includes_lessons_insights():
    result = asyncio.run(memory_profile(
        project=PROJECT, include_lessons=True, include_insights=True, include_frontier=False
    ))
    assert "profile" in result or "sessionCount" in result, (
        f"memory_profile response has no profile data — got: {list(result.keys())}"
    )


def test_proxy_memory_task_context_with_task_param():
    result = asyncio.run(memory_task_context(task="pose estimation pipeline", project=PROJECT))
    assert "objective" in result, f"memory_task_context missing 'objective' — got: {list(result.keys())}"
    assert result["objective"]


def test_proxy_memory_update_task_create_and_cancel():
    """Create an action via proxy then cancel it — verifies memory_update_task dispatch."""
    uid = uuid.uuid4().hex[:8]
    create_result = asyncio.run(memory_update_task(
        operation="create",
        title=f"proxy-test-{uid}",
        description="Created by test_mcp_proxy — cancelled immediately",
        priority=1,
        project=PROJECT,
        tags="type:test",
    ))
    action = create_result.get("action", {})
    action_id = action.get("id")
    assert action_id, f"create did not return action id — response: {create_result}"

    cancel_result = asyncio.run(memory_update_task(
        operation="cancel",
        action_id=action_id,
        result="test cleanup",
    ))
    final = cancel_result.get("action", {})
    assert final.get("status") == "cancelled", (
        f"action status after cancel is '{final.get('status')}' not 'cancelled'"
    )


def test_proxy_memory_lesson_save_live():
    """memory_lesson_save reaches the live /agentmemory/lessons endpoint."""
    tag = f"proxy-lesson-{uuid.uuid4().hex[:6]}"
    result = asyncio.run(memory_lesson_save(
        content=f"Proxy test lesson — frontier key is 'frontier'. Tag: {tag}",
        context="test_mcp_proxy",
        confidence=0.75,
        project=PROJECT,
        tags="type:test",
    ))
    # Accept either {lesson: {id: ...}} or a direct {id: ...} response shape
    lesson_id = result.get("lesson", result).get("id") if isinstance(result, dict) else None
    assert lesson_id, f"memory_lesson_save returned no id — response: {result}"
