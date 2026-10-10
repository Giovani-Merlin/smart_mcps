"""The Driver Item Record: what the run driver recorded for a group's
``Run (driver):`` items settles them for auto-finish."""

from __future__ import annotations

import json

import pytest

from orchestrator.execution.driver_items import (
    DriverItemError,
    pending_driver_items,
    read_driver_items,
    record_driver_item,
)
from orchestrator.execution.finish import pending_driver_run_items
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.model import Group, GroupingResult, ReviewIntensity, VerificationItem

LIVE = "Run (driver): `uv run pytest -m llm tests/test_x.py` — Pass: green."


def _group() -> Group:
    return Group(
        id="g1",
        name="n",
        summary="s",
        spec="x",
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        verification=[
            VerificationItem(id="g1-1", description="unit tests pass"),
            VerificationItem(id="g1-2", description=LIVE),
            VerificationItem(id="g1-3", description=LIVE),
            VerificationItem(id="g1-4", description=LIVE),
        ],
    )


@pytest.fixture
def run(tmp_path):
    group = _group()
    paths = RunPaths(tmp_path, "r-rec")
    paths.run_dir.mkdir(parents=True)
    grouping = GroupingResult(plan_path="p.md", groups=[group])
    atomic_write_text(paths.groups_path, grouping.model_dump_json())
    return tmp_path, paths, group


def test_pass_and_skipped_settle_an_item_and_fail_keeps_it_pending(run):
    root, paths, group = run
    assert pending_driver_run_items(root, "r-rec") == {"g1": ["g1-2", "g1-3", "g1-4"]}

    record_driver_item(paths, group, "g1-2", "pass", "ran it")
    record_driver_item(paths, group, "g1-3", "skipped", "no network")
    record_driver_item(paths, group, "g1-4", "fail", "red")

    assert pending_driver_run_items(root, "r-rec") == {"g1": ["g1-4"]}
    assert pending_driver_items(paths, group) == ["g1-4"]
    stored = read_driver_items(paths, "g1")
    assert stored["g1-2"]["status"] == "pass" and stored["g1-2"]["by"] == "driver"
    assert stored["g1-2"]["notes"] == "ran it" and stored["g1-2"]["recorded_at"]
    assert json.loads(paths.driver_items_path("g1").read_text()).keys() == stored.keys()

    # Re-recording overwrites: the failed item later passes.
    record_driver_item(paths, group, "g1-4", "pass")
    assert pending_driver_run_items(root, "r-rec") == {}


def test_the_recorded_line_reaches_the_run_log(run):
    _, paths, group = run
    record_driver_item(paths, group, "g1-2", "pass")
    assert "group g1: driver item g1-2 recorded pass by the driver" in (
        paths.event_log_path.read_text()
    )


def test_an_unknown_item_or_status_raises_naming_what_is_known(run):
    _, paths, group = run
    with pytest.raises(DriverItemError, match=r"g1-1, g1-2, g1-3, g1-4"):
        record_driver_item(paths, group, "g1-9", "pass")
    with pytest.raises(DriverItemError, match="pass, fail, skipped"):
        record_driver_item(paths, group, "g1-2", "done")
    assert read_driver_items(paths, "g1") == {}


def test_a_missing_or_corrupt_record_reads_as_empty(run):
    _, paths, _ = run
    assert read_driver_items(paths, "g1") == {}
    atomic_write_text(paths.driver_items_path("g1"), "{not json")
    assert read_driver_items(paths, "g1") == {}
