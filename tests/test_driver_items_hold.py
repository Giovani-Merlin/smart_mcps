"""Driver-item hold: a dependent waits while its dependency has unrecorded
``Run (driver):`` items (plan U2). In-process; executors are stubs."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.config import EscalationConfig
from orchestrator.execution import scheduler as scheduler_module
from orchestrator.execution.driver_items import record_driver_item
from orchestrator.execution.escalation import EscalationBroker, EscalationPolicy
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.scheduler import GroupState, HoldReason, Scheduler
from orchestrator.model import EscalationKind, EscalationResponse, HumanAction, VerificationItem

from tests.test_scheduler import StubBroker, completing_executor, make_group, wait_until


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(scheduler_module, "DRIVER_ITEMS_POLL_S", 0.01)


def _driver_group(gid: str = "g1"):
    group = make_group(gid)
    group.verification = [
        VerificationItem(id=f"{gid}-1", description="Run (driver): uv run pytest -q Pass: green")
    ]
    return group


def _log(paths: RunPaths) -> str:
    log = paths.run_dir / "logs" / "run.log"
    return log.read_text() if log.is_file() else ""


def _all_completed(result) -> bool:
    return set(result.values()) == {GroupState.COMPLETED}


@pytest.mark.asyncio
async def test_dependent_waits_for_record_then_runs(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    g1 = _driver_group()
    started: list[str] = []
    scheduler = Scheduler(
        groups=[g1, make_group("g2", deps=["g1"])],
        paths=paths,
        executor=completing_executor(started),
    )
    task = asyncio.create_task(scheduler.run())
    await wait_until(lambda: "held — dependency g1" in _log(paths))
    await asyncio.sleep(0.1)
    assert started == ["g1"]
    assert scheduler.state.groups["g2"].state == GroupState.PENDING
    hold = [h for h in scheduler.state.groups["g2"].holds if h.reason == HoldReason.DRIVER_ITEMS]
    assert [(h.group_id, h.files) for h in hold] == [("g1", ["g1-1"])]
    assert not task.done()

    record_driver_item(paths, g1, "g1-1", "pass")
    result = await asyncio.wait_for(task, 5)
    assert _all_completed(result)
    assert _log(paths).count("held — dependency g1") == 1


@pytest.mark.asyncio
async def test_no_driver_items_means_no_hold(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    scheduler = Scheduler(
        groups=[make_group("g1"), make_group("g2", deps=["g1"])],
        paths=paths,
        executor=completing_executor(),
    )
    assert _all_completed(await scheduler.run())


@pytest.mark.asyncio
async def test_skipped_record_does_not_hold(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    g1 = _driver_group()
    record_driver_item(paths, g1, "g1-1", "skipped", "moot")
    scheduler = Scheduler(
        groups=[g1, make_group("g2", deps=["g1"])], paths=paths, executor=completing_executor()
    )
    assert _all_completed(await scheduler.run())
    assert "held — dependency" not in _log(paths)


@pytest.mark.asyncio
async def test_one_request_per_dependency_and_retry_releases_all(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    broker = StubBroker(EscalationResponse(id="x", action=HumanAction.RETRY))
    scheduler = Scheduler(
        groups=[_driver_group(), make_group("g2", deps=["g1"]), make_group("g3", deps=["g1"])],
        paths=paths,
        executor=completing_executor(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "orchestrator_only"),
    )
    result = await asyncio.wait_for(scheduler.run(), 5)
    assert _all_completed(result)
    assert len(broker.raised) == 1
    request = broker.raised[0]
    assert request.kind == EscalationKind.DRIVER_ITEMS_PENDING
    assert request.group_id == "g1"
    assert "g2" in request.prompt and "g3" in request.prompt
    assert not paths.driver_items_path("g1").exists()


@pytest.mark.asyncio
async def test_on_failure_tier_never_raises_it(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    g1 = _driver_group()
    broker = StubBroker(None)
    scheduler = Scheduler(
        groups=[g1, make_group("g2", deps=["g1"])],
        paths=paths,
        executor=completing_executor(),
        broker=broker,
        policy=EscalationPolicy("on_failure", "orchestrator_only"),
    )
    task = asyncio.create_task(scheduler.run())
    await wait_until(lambda: "held — dependency g1" in _log(paths))
    record_driver_item(paths, g1, "g1-1", "pass")
    await asyncio.wait_for(task, 5)
    assert broker.raised == []


@pytest.mark.asyncio
async def test_recording_settles_an_unanswered_request(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    g1 = _driver_group()
    broker = EscalationBroker(paths, EscalationConfig(timeout_s=30, poll_interval_s=0.01))
    scheduler = Scheduler(
        groups=[g1, make_group("g2", deps=["g1"])],
        paths=paths,
        executor=completing_executor(),
        broker=broker,
        policy=EscalationPolicy("on_stuck", "orchestrator_only"),
    )
    task = asyncio.create_task(scheduler.run())
    await wait_until(lambda: any(paths.escalations_dir.glob("request-*.json")))
    record_driver_item(paths, g1, "g1-1", "pass")
    assert _all_completed(await asyncio.wait_for(task, 5))
