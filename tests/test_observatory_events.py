"""U4 tests: the two SSE streams — the log tail and the debounced run-change nudge.

These drive the async stream generators directly rather than over HTTP. The
streams are infinite and everything under test is *timing* — backlog-then-tail,
never re-emitting, debouncing a burst into one event, and tearing the watcher
down on disconnect — all of which is deterministic to assert on the generator but
flaky through a ``TestClient`` portal. That the endpoints are registered on the
module's ``router`` and reachable through ``create_app`` is covered by
``test_observatory_api``'s assembly test; a local sanity check of the routes is
kept here too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.execution.manifest import RunPaths
from orchestrator.observatory import events
from orchestrator.observatory.events import active_stream_count


class FakeRequest:
    """The only bit of ``Request`` the streams touch: the disconnect flag."""

    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


@pytest.fixture
def paths(tmp_path: Path) -> RunPaths:
    run = RunPaths(tmp_path, "run1")
    run.run_dir.mkdir(parents=True)
    return run


def append_line(paths: RunPaths, text: str) -> None:
    """Append one raw line to run.log — raw so tests assert exact content."""
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    with paths.event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


async def _drain_into(agen, sink: list) -> None:
    """Consume a stream generator until it returns (which it does on disconnect)."""
    async for event in agen:
        sink.append(event)


async def _until(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


# --------------------------------------------------------------------- log tail


@pytest.mark.asyncio
async def test_log_streams_backlog_then_each_appended_line_once(paths):
    """Existing lines first, then each appended line as its own event, in order,
    with nothing re-emitted."""
    append_line(paths, "backlog-1")
    append_line(paths, "backlog-2")

    req = FakeRequest()
    sink: list = []
    task = asyncio.create_task(_drain_into(events._log_stream(paths, req), sink))
    try:
        await _until(lambda: len(sink) >= 2)
        assert [event["data"] for event in sink] == ["backlog-1", "backlog-2"]

        append_line(paths, "live-1")
        append_line(paths, "live-2")
        await _until(lambda: len(sink) >= 4)
        assert [event["data"] for event in sink] == [
            "backlog-1",
            "backlog-2",
            "live-1",
            "live-2",
        ]

        # Several more poll cycles must not re-send an already-emitted line.
        await asyncio.sleep(events.POLL_S * 5)
        assert [event["data"] for event in sink] == [
            "backlog-1",
            "backlog-2",
            "live-1",
            "live-2",
        ]
    finally:
        req.disconnected = True
        await asyncio.wait_for(task, timeout=2)
    assert active_stream_count() == 0


@pytest.mark.asyncio
async def test_log_stream_holds_open_for_a_missing_file_then_tails_it(paths):
    """A client that connects before run.log exists gets an open stream, not a
    404, and starts receiving once the file appears."""
    assert not paths.event_log_path.exists()

    req = FakeRequest()
    sink: list = []
    task = asyncio.create_task(_drain_into(events._log_stream(paths, req), sink))
    try:
        await asyncio.sleep(events.POLL_S * 4)
        assert sink == []
        assert not task.done()  # still open, waiting for the file

        append_line(paths, "appeared")
        await _until(lambda: len(sink) >= 1)
        assert [event["data"] for event in sink] == ["appeared"]
    finally:
        req.disconnected = True
        await asyncio.wait_for(task, timeout=2)
    assert active_stream_count() == 0


@pytest.mark.asyncio
async def test_log_stream_holds_a_torn_final_line_until_its_newline(paths):
    """A half-written last line waits for its terminator rather than emitting a
    partial line the transcript never had."""
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    paths.event_log_path.write_text("whole\npartial-no-newline")

    req = FakeRequest()
    sink: list = []
    task = asyncio.create_task(_drain_into(events._log_stream(paths, req), sink))
    try:
        await _until(lambda: len(sink) >= 1)
        await asyncio.sleep(events.POLL_S * 3)
        assert [event["data"] for event in sink] == ["whole"]  # partial withheld

        with paths.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write("-now-complete\n")
        await _until(lambda: len(sink) >= 2)
        assert [event["data"] for event in sink] == ["whole", "partial-no-newline-now-complete"]
    finally:
        req.disconnected = True
        await asyncio.wait_for(task, timeout=2)
    assert active_stream_count() == 0


# ---------------------------------------------------------------- run changes


@pytest.mark.asyncio
async def test_run_stream_debounces_a_burst_into_fewer_events(paths):
    """Five state.json writes inside the debounce window collapse to fewer events
    than writes, and at least one."""
    paths.state_path.write_text("{}")

    req = FakeRequest()
    sink: list = []
    task = asyncio.create_task(_drain_into(events._run_stream(paths, req), sink))
    try:
        # let the generator capture its baseline signature first
        await asyncio.sleep(events.POLL_S * 2)

        writes = 5
        for i in range(writes):
            paths.state_path.write_text(json.dumps({"n": i}))
            await asyncio.sleep(0.01)  # whole burst well inside DEBOUNCE_S

        await _until(lambda: len(sink) >= 1, timeout=3)
        # give any stray extra events time to arrive before counting
        await asyncio.sleep(events.DEBOUNCE_S * 2)
        assert 1 <= len(sink) < writes
        assert all(event["event"] == "changed" for event in sink)
        assert sink[0]["data"] == "run1"
    finally:
        req.disconnected = True
        await asyncio.wait_for(task, timeout=3)
    assert active_stream_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ["state", "manifest", "escalations", "groups"])
async def test_run_stream_reacts_to_each_watched_artifact(paths, artifact):
    """A change to any of state.json, manifest.json, escalations/ or groups/
    produces a changed event."""
    req = FakeRequest()
    sink: list = []
    task = asyncio.create_task(_drain_into(events._run_stream(paths, req), sink))
    try:
        await asyncio.sleep(events.POLL_S * 2)  # baseline captured

        if artifact == "state":
            paths.state_path.write_text("{}")
        elif artifact == "manifest":
            paths.manifest_path.write_text("{}")
        elif artifact == "escalations":
            paths.escalations_dir.mkdir(parents=True, exist_ok=True)
            (paths.escalations_dir / "request-e1.json").write_text("{}")
        else:  # groups
            group_dir = paths.group_dir("g1")
            group_dir.mkdir(parents=True, exist_ok=True)
            (group_dir / "report-g1-r1.json").write_text("{}")

        await _until(lambda: len(sink) >= 1, timeout=3)
        assert sink[0]["event"] == "changed"
    finally:
        req.disconnected = True
        await asyncio.wait_for(task, timeout=3)
    assert active_stream_count() == 0


@pytest.mark.asyncio
async def test_run_stream_excludes_the_log_from_its_signature(paths):
    """Log lines have their own stream; appending to run.log must not by itself
    provoke a run-change event (that would make every log line re-fetch the
    snapshot)."""
    req = FakeRequest()
    sink: list = []
    task = asyncio.create_task(_drain_into(events._run_stream(paths, req), sink))
    try:
        await asyncio.sleep(events.POLL_S * 2)  # baseline captured
        append_line(paths, "a log line")
        await asyncio.sleep(events.DEBOUNCE_S * 2 + events.POLL_S * 4)
        assert sink == []
    finally:
        req.disconnected = True
        await asyncio.wait_for(task, timeout=2)
    assert active_stream_count() == 0


# ---------------------------------------------------------------- teardown


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["log", "run"])
async def test_stream_tears_down_on_client_disconnect(paths, stream):
    """A disconnected client leaves no watcher behind — the generator returns and
    the live-stream count drops back."""
    paths.state_path.write_text("{}")
    append_line(paths, "x")

    req = FakeRequest()
    sink: list = []
    generator = (
        events._log_stream(paths, req) if stream == "log" else events._run_stream(paths, req)
    )
    task = asyncio.create_task(_drain_into(generator, sink))

    await _until(lambda: active_stream_count() >= 1)  # stream is live
    req.disconnected = True
    await asyncio.wait_for(task, timeout=2)

    assert task.done()
    assert active_stream_count() == 0


# ---------------------------------------------------------------- registration


def test_both_sse_endpoints_are_on_the_module_router():
    registered = {route.path for route in events.router.routes}
    assert {"/events/log", "/events/run"} <= registered
