"""Tests for codegraph_mcp/server.py — the trimmed proxy over `codegraph serve --mcp`.

These guard the two ways this proxy can silently drift from its backend: the
`from`/`to` argument remap, and swallowing a backend failure into empty text.
"""

import pytest
from fastmcp.exceptions import ToolError

from codegraph_mcp import server

KEPT_TOOLS = {"context", "explore", "trace", "impact", "files", "search"}


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeBackend:
    """Stands in for the persistent client, recording what the wrappers forward."""

    def __init__(self, text="ok"):
        self.calls: list[tuple[str, dict]] = []
        self.text = text

    def is_connected(self):
        return True

    async def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, arguments))
        return _Result(self.text)


@pytest.fixture
def backend(monkeypatch):
    fake = _FakeBackend()
    monkeypatch.setattr(server, "_backend", fake)
    return fake


async def _tool_names():
    return {t.name for t in await server.mcp.list_tools()}


@pytest.mark.asyncio
async def test_exposes_exactly_the_six_kept_tools():
    """The trimming is the point — 6 tools, not upstream's 10."""
    assert await _tool_names() == KEPT_TOOLS


@pytest.mark.asyncio
async def test_dropped_tools_are_not_exposed():
    names = await _tool_names()
    assert not names & {"callers", "callees", "node", "status"}


@pytest.mark.asyncio
async def test_trace_remaps_to_the_backends_from_and_to_params(backend):
    """`from` is a Python keyword, so the wrapper renames it. If this remap breaks,
    the backend rejects the call — this is the likeliest silent-drift point."""
    await server.trace(from_symbol="main", to_symbol="_lint")

    name, args = backend.calls[0]
    assert name == "codegraph_trace"
    assert args == {"from": "main", "to": "_lint"}


@pytest.mark.asyncio
async def test_project_path_is_never_forwarded(backend):
    """We always operate on the current project; the backend resolves it from -p."""
    await server.context(task="anything")
    await server.files()

    assert all("projectPath" not in args for _, args in backend.calls)


@pytest.mark.asyncio
async def test_wrappers_forward_camel_case_backend_params(backend):
    await server.context(task="t", include_code=False)
    await server.explore(query="q", max_files=3)

    assert backend.calls[0][1] == {"task": "t", "includeCode": False}
    assert backend.calls[1][1] == {"query": "q", "maxFiles": 3}


@pytest.mark.asyncio
async def test_none_valued_args_are_dropped_not_sent_as_null(backend):
    await server.files(path="hooks", pattern=None)

    assert backend.calls[0][1] == {"path": "hooks"}


@pytest.mark.asyncio
async def test_backend_text_is_returned_verbatim(monkeypatch):
    """Text passes through untouched, so a staleness banner survives the wrapper."""
    banner = (
        "⚠️ Some files referenced below were edited since the last index sync — "
        "their codegraph entries may be stale:\n  - pplx/cli.py (edited 5ms ago, "
        "pending sync)\n\n## Code Context\nbody"
    )
    monkeypatch.setattr(server, "_backend", _FakeBackend(text=banner))

    assert await server.context(task="t") == banner


@pytest.mark.asyncio
async def test_empty_backend_text_raises_rather_than_returning_nothing(monkeypatch):
    """Failing loudly is the whole mitigation for upstream drift."""
    monkeypatch.setattr(server, "_backend", _FakeBackend(text="   "))

    with pytest.raises(ToolError, match="returned no content"):
        await server.search(query="q")


@pytest.mark.asyncio
async def test_disconnected_backend_raises_a_clear_error(monkeypatch):
    class _Down(_FakeBackend):
        def is_connected(self):
            return False

    monkeypatch.setattr(server, "_backend", _Down())

    with pytest.raises(ToolError, match="not connected"):
        await server.context(task="t")


@pytest.mark.asyncio
async def test_missing_backend_raises_rather_than_hanging(monkeypatch):
    monkeypatch.setattr(server, "_backend", None)

    with pytest.raises(ToolError, match="not connected"):
        await server.impact(symbol="x")
