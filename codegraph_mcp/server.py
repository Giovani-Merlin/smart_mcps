#!/usr/bin/env python3
"""
Codegraph MCP proxy — a trimmed, resident tool surface over `codegraph serve --mcp`.

Upstream exposes 10 tools (~2,400 tokens of schema). These tools are loaded into every
session (`alwaysLoad`), so schema size is a permanent context cost and every extra tool
dilutes selection accuracy. This proxy re-exposes the 6 that earn their keep, with
`projectPath` and low-value knobs dropped and descriptions rewritten to say explicitly
when to prefer these over Grep.

`trace`, `explore`, and `context` are MCP-only — the `codegraph` CLI has no equivalent —
so this forwards to the MCP backend rather than shelling out. Backend text is returned
verbatim, which keeps the ⚠️ staleness banner intact when the backend emits one. (It
rarely does: the banner is populated only by the backend's file watcher, and `--no-watch`
disables it. Callers should treat session-edited files as stale regardless.)

Dropped tools stay reachable via the CLI (`codegraph callers|callees|affected|status`).

Entry point: smart-mcps-codegraph (stdio).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError
from pydantic import Field

# The backend resolves the project from -p; it never sees a projectPath argument.
_PROJECT = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

# Bounds a hung backend so a stuck call surfaces as a tool error instead of a dead session.
_CALL_TIMEOUT = 120.0

_backend: Client | None = None


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Hold one stdio client to the codegraph backend for the life of the server.

    keep_alive=False ties the backend process to this context manager, so it is torn
    down with us rather than being left orphaned.
    """
    global _backend
    transport = StdioTransport(
        command="codegraph",
        args=["serve", "--mcp", "--no-watch", "-p", _PROJECT],
        cwd=_PROJECT,
        keep_alive=False,
    )
    async with Client(transport) as client:
        _backend = client
        try:
            yield
        finally:
            _backend = None


mcp = FastMCP(name="codegraph", lifespan=_lifespan)


async def _forward(tool: str, arguments: dict[str, Any]) -> str:
    """Call a backend tool and return its text verbatim.

    Errors are never swallowed into empty text: a bad argument or renamed upstream tool
    must surface loudly, otherwise this proxy would silently drift from the backend.
    """
    client = _backend
    if client is None or not client.is_connected():
        raise ToolError(
            "codegraph backend is not connected. Check that `codegraph` is on PATH and "
            "that `.codegraph/` exists (run `codegraph init && codegraph index`)."
        )

    args = {k: v for k, v in arguments.items() if v is not None}
    # raise_on_error=True: an isError result from the backend becomes a ToolError here.
    result = await client.call_tool(f"codegraph_{tool}", args, timeout=_CALL_TIMEOUT)

    text = "\n".join(block.text for block in result.content if getattr(block, "text", None))
    if not text.strip():
        raise ToolError(
            f"codegraph_{tool} returned no content — the backend may have failed or its "
            f"schema may have changed. Fall back to the CLI: `codegraph {tool} ...`"
        )
    return text


@mcp.tool(output_schema=None)
async def context(
    task: Annotated[str, Field(description="The task, bug, feature, or area to explain")],
    include_code: bool = True,
) -> str:
    """PRIMARY tool — call FIRST for any structural code question: how does X work, where
    is X defined, what calls X, what is this area's architecture. Composes search + symbol
    details + callers + callees into ONE call returning entry points, related symbols, and
    key code — usually enough to answer with no Grep or Read at all.

    Prefer this over Grep for anything structural. Use Grep only for literal text (a string
    constant, comment, or log message) or in a file you already have open.
    """
    return await _forward("context", {"task": task, "includeCode": include_code})


@mcp.tool(output_schema=None)
async def explore(
    query: Annotated[
        str,
        Field(
            description="Symbol/file/code terms, NOT a sentence. "
            'Good: "renderScene drawElement ShapeCache". Bad: "how are prompts loaded".'
        ),
    ],
    max_files: int = 12,
) -> str:
    """Source for SEVERAL related symbols at once, grouped by file, in ONE capped call —
    the efficient alternative to a loop of Read calls. Use after `context` when you need the
    actual bodies of the symbols it surfaced.

    Returns verbatim line-numbered source, byte-for-byte identical to Read; treat the files
    it shows as already read and do not re-open them.
    """
    return await _forward("explore", {"query": query, "maxFiles": max_files})


@mcp.tool(output_schema=None)
async def trace(
    from_symbol: Annotated[str, Field(description="Symbol the flow starts at")],
    to_symbol: Annotated[str, Field(description="Symbol the flow should reach")],
) -> str:
    """Trace the CALL PATH between two symbols — "how does X reach/become Y?" Returns the
    whole chain in ONE call, each hop with file:line and its body, bridging dynamic dispatch
    hops (callbacks, JSX, descriptors).

    Grep structurally cannot answer this — there is no text pattern for "the path from A to
    B". Use this for any flow question instead of a grep-and-read loop. If no static path
    exists, the output says where the chain breaks.
    """
    # The backend's parameters are literally named `from` and `to`; `from` is a Python
    # keyword, so the wrapper takes *_symbol names and remaps here.
    return await _forward("trace", {"from": from_symbol, "to": to_symbol})


@mcp.tool(output_schema=None)
async def impact(
    symbol: Annotated[str, Field(description="Symbol whose blast radius to analyze")],
) -> str:
    """What breaks if I change this — the impact radius of modifying a symbol, followed
    across real call and import edges rather than name matches. Run before editing any
    widely-used function or constant. Prefer over Grep, which finds coincidental name hits
    and misses indirect dependents.
    """
    return await _forward("impact", {"symbol": symbol})


@mcp.tool(output_schema=None)
async def files(
    path: Annotated[
        str | None, Field(description='Limit to files under this dir, e.g. "hooks/"')
    ] = None,
    pattern: Annotated[str | None, Field(description='Glob filter, e.g. "**/*.test.ts"')] = None,
) -> str:
    """Project file structure from the index — indexed files with language and symbol count.
    Use INSTEAD OF Glob, ls, or find when asking what files exist or how the codebase is
    organized.
    """
    return await _forward("files", {"path": path, "pattern": pattern})


@mcp.tool(output_schema=None)
async def search(
    query: Annotated[str, Field(description="Symbol name or partial name")],
) -> str:
    """Quick symbol lookup by name — locations and signatures only, no code. Use when you
    do not know a symbol's exact name, or as the fallback when `context` returns nothing.

    Prefer `context` for real questions; prefer this over Grep for finding a symbol by name.
    """
    return await _forward("search", {"query": query})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
