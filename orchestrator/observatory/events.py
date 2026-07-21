"""SSE streams: the live log tail and debounced run-change events (plan U4).

Two streams, deliberately different in what they carry:

- ``/events/log`` sends *content* — every line of ``run.log``, backlog first,
  then each appended line as it lands. It tracks a byte offset and only emits
  complete lines, so a line is never sent twice and a half-written final line
  waits for its newline.
- ``/events/run`` sends a *nudge* — a bare ``changed`` event when the run
  directory mutates, debounced, so the SPA re-fetches the snapshot instead of
  polling it on a fixed interval. It carries no payload on purpose: the snapshot
  endpoint stays the single composition point.

Both poll disk rather than using inotify. Polling is dependency-free, behaves
the same on every filesystem (WSL, containers, network mounts), and at
human-latency intervals costs nothing — the same reasoning the escalation broker
already applies to its response-file wait.

Neither stream 404s on a missing artifact: a run's ``run.log`` and
``escalations/`` are both created lazily, and a client that connects a moment
early should stream once they appear rather than fail.

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from orchestrator.execution.manifest import RunPaths
from orchestrator.observatory.runs import resolve_run

router = APIRouter(tags=["events"])

POLL_S = 0.05
# One quiet moment before reporting: a single scheduler transition rewrites
# state.json and the manifest back to back, and that is one change to a reader.
DEBOUNCE_S = 0.3

# Live stream count, so a test can assert a cancelled request really tore its
# watcher down instead of leaking a task behind the response.
_active_streams = 0
_lock = asyncio.Lock()


def active_stream_count() -> int:
    return _active_streams


async def _opened() -> None:
    global _active_streams
    async with _lock:
        _active_streams += 1


async def _closed() -> None:
    global _active_streams
    async with _lock:
        _active_streams -= 1


# --------------------------------------------------------------------- log


async def _log_stream(paths: RunPaths, request: Request) -> AsyncIterator[dict]:
    """Backlog then tail, by byte offset so no line is emitted twice."""
    await _opened()
    path = paths.event_log_path
    offset = 0
    pending = ""
    try:
        while True:
            if await request.is_disconnected():
                return
            if path.is_file():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
                if chunk:
                    pending += chunk
                    # Only whole lines go out; a torn tail waits for its newline.
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if line:
                            yield {"data": line}
            await asyncio.sleep(POLL_S)
    finally:
        await _closed()


@router.get("/events/log")
async def stream_log(request: Request, project: str, run: str) -> EventSourceResponse:
    """Tail ``logs/run.log`` as unnamed SSE messages (``EventSource.onmessage``)."""
    paths = resolve_run(request, project, run)
    return EventSourceResponse(_log_stream(paths, request))


# --------------------------------------------------------------------- run


def _signature(paths: RunPaths) -> tuple:
    """A cheap fingerprint of everything the snapshot is composed from.

    ``logs/`` is deliberately excluded — log lines have their own stream, and
    folding them in here would make every event line re-fetch the snapshot.
    """
    parts: list[tuple] = []
    for path in (paths.state_path, paths.manifest_path, paths.groups_path):
        parts.append((path.name, _stat(path)))
    for directory in (paths.escalations_dir, paths.run_dir / "groups"):
        if not directory.is_dir():
            parts.append((directory.name, None))
            continue
        parts.append(
            (
                directory.name,
                tuple(sorted((str(child), _stat(child)) for child in directory.rglob("*"))),
            )
        )
    return tuple(parts)


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


async def _run_stream(paths: RunPaths, request: Request) -> AsyncIterator[dict]:
    """Emit ``changed`` once per quiet period, not once per write."""
    await _opened()
    last = _signature(paths)
    try:
        while True:
            if await request.is_disconnected():
                return
            await asyncio.sleep(POLL_S)
            current = _signature(paths)
            if current == last:
                continue
            # Let a burst finish: keep waiting until the run dir holds still,
            # then report the whole burst as one change.
            while True:
                await asyncio.sleep(DEBOUNCE_S)
                settled = _signature(paths)
                if settled == current:
                    break
                current = settled
            last = current
            yield {"event": "changed", "data": paths.run_id}
    finally:
        await _closed()


@router.get("/events/run")
async def stream_run(request: Request, project: str, run: str) -> EventSourceResponse:
    """Emit a named ``changed`` event when the run directory mutates.

    Named, so the SPA subscribes with ``addEventListener("changed", ...)`` and
    the two streams never get crossed on one connection.
    """
    paths = resolve_run(request, project, run)
    return EventSourceResponse(_run_stream(paths, request))
