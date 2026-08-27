"""U11 tests: SurpriseBoard validates affected_groups ids at mark time instead
of silently accumulating dead buckets under ids nothing will ever read.
"""

from __future__ import annotations

import logging

from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.review import (
    REASON_GROUP_COMPLETED,
    REASON_RUN_ENDED,
    REASON_UNKNOWN_GROUP,
    SurpriseBoard,
    format_residue_report,
    surprise_residue,
)
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.model import Group, ReviewIntensity, Surprise


def make_group(gid: str, tasks: list[str] | None = None) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.5,
        intensity=ReviewIntensity.PAIRED,
        tasks=tasks or [],
    )


def thirteen_groups() -> list[Group]:
    groups = [make_group(f"g{i}") for i in range(1, 14)]
    groups[4] = make_group("g5", tasks=["u16-play-route"])  # g5 owns this task
    return groups


def surprise(description: str = "x", affected_groups: list[str] | None = None) -> Surprise:
    return Surprise(kind="other", description=description, affected_groups=affected_groups or [])


def test_unknown_group_id_lands_in_run_level_list_and_is_logged(caplog):
    board = SurpriseBoard(groups=thirteen_groups())
    s = surprise("g14 doesn't exist", ["g14"])
    with caplog.at_level(logging.WARNING):
        board.mark(s, source_group="g1")
    assert board.pending_for("g14") == []
    assert board.pending_for(SurpriseBoard.RUN_LEVEL) == [s]
    assert any("g14" in rec.message and "g1" in rec.message for rec in caplog.records)


def test_task_id_is_resolved_to_its_owning_group_with_no_dead_bucket(caplog):
    board = SurpriseBoard(groups=thirteen_groups())
    s = surprise("play route changed", ["u16-play-route"])
    board.mark(s, source_group="g1")
    assert board.pending_for("g5") == [s]
    assert board.pending_for("u16-play-route") == []
    assert board.pending_for(SurpriseBoard.RUN_LEVEL) == []


def test_orphan_task_id_lands_in_run_level_list_and_is_logged(caplog):
    board = SurpriseBoard(groups=thirteen_groups())
    s = surprise("no owner for this task", ["u10-calibration-passes"])
    with caplog.at_level(logging.WARNING):
        board.mark(s, source_group="g2")
    assert board.pending_for("u10-calibration-passes") == []
    assert board.pending_for(SurpriseBoard.RUN_LEVEL) == [s]
    assert any(
        "u10-calibration-passes" in rec.message and "g2" in rec.message for rec in caplog.records
    )


def test_wide_fanout_delivers_to_every_existing_group_and_warns_naming_the_count(caplog):
    board = SurpriseBoard(groups=thirteen_groups())
    real = [f"g{i}" for i in range(1, 14)]  # 13 real groups
    fake = ["g14", "g15", "g16"]  # 3 unknown ids
    named = real + fake  # 16 named total — comfortably above the fan-out threshold
    s = surprise("broad interface change", named)
    with caplog.at_level(logging.WARNING):
        board.mark(s, source_group="g0")  # source not among the 16 named ids
    for gid in real:
        assert board.pending_for(gid) == [s]
    assert board.pending_for(SurpriseBoard.RUN_LEVEL) == [s]  # the 3 fake ids fell through
    fanout_lines = [rec.message for rec in caplog.records if "wide fan-out" in rec.message]
    assert len(fanout_lines) == 1
    assert "16" in fanout_lines[0]


def test_same_surprise_marked_five_times_is_delivered_once():
    board = SurpriseBoard(groups=thirteen_groups())
    s = surprise("repeated finding", ["g2"])
    for _ in range(5):
        board.mark(s, source_group="g1")
    assert board.pending_for("g2") == [s]


def test_valid_still_running_group_is_delivered_unchanged():
    board = SurpriseBoard(groups=thirteen_groups())
    s = surprise("normal finding", ["g7"])
    board.mark(s, source_group="g1")
    assert board.pending_for("g7") == [s]


def test_no_groups_configured_preserves_legacy_unvalidated_behavior():
    # Every caller before this unit constructs SurpriseBoard() bare — an id like
    # "g0" that names no real group must keep working exactly as before.
    board = SurpriseBoard()
    s = surprise("legacy", ["g0"])
    board.mark(s, source_group="g1")
    assert board.pending_for("g0") == [s]
    assert board.pending_for(SurpriseBoard.RUN_LEVEL) == []


# ------------------------------------------------------------ U12: residue report


def test_residue_report_is_empty_when_the_board_never_persisted_anything(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    assert surprise_residue(paths, state=None) == []
    assert "none pending" in format_residue_report([])


def test_residue_labels_a_bucket_for_a_completed_group(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    board = SurpriseBoard(paths, groups=thirteen_groups())
    s = surprise("late finding", ["g1"])
    board.mark(s, source_group="g2")
    state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.COMPLETED)})

    entries = surprise_residue(paths, state)

    assert len(entries) == 1
    assert entries[0].bucket == "g1"
    assert entries[0].count == 1
    assert entries[0].reason == REASON_GROUP_COMPLETED
    report = format_residue_report(entries)
    assert "g1: 1 pending" in report
    assert REASON_GROUP_COMPLETED in report


def test_residue_labels_a_resolved_group_the_same_as_completed(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    board = SurpriseBoard(paths, groups=thirteen_groups())
    board.mark(surprise("late finding", ["g1"]), source_group="g2")
    state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.RESOLVED)})

    entries = surprise_residue(paths, state)

    assert entries[0].reason == REASON_GROUP_COMPLETED


def test_residue_labels_a_still_pending_group_as_run_ended_before_delivery(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    board = SurpriseBoard(paths, groups=thirteen_groups())
    board.mark(surprise("late finding", ["g1"]), source_group="g2")
    state = RunState(run_id="r1", groups={"g1": GroupRunState(state=GroupState.RUNNING)})

    entries = surprise_residue(paths, state)

    assert entries[0].reason == REASON_RUN_ENDED


def test_residue_labels_the_run_level_bucket_as_unknown_group_id(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    board = SurpriseBoard(paths, groups=thirteen_groups())
    board.mark(surprise("g14 doesn't exist", ["g14"]), source_group="g1")
    state = RunState(run_id="r1", groups={})

    entries = surprise_residue(paths, state)

    assert len(entries) == 1
    assert entries[0].bucket == SurpriseBoard.RUN_LEVEL
    assert entries[0].reason == REASON_UNKNOWN_GROUP


def test_residue_report_lists_every_bucket_with_its_own_reason(tmp_path):
    paths = RunPaths(tmp_path, "r1")
    board = SurpriseBoard(paths, groups=thirteen_groups())
    board.mark(surprise("finding for g1", ["g1"]), source_group="g3")
    board.mark(surprise("finding for g2", ["g2"]), source_group="g3")
    board.mark(surprise("unknown id", ["g14"]), source_group="g3")
    state = RunState(
        run_id="r1",
        groups={
            "g1": GroupRunState(state=GroupState.COMPLETED),
            "g2": GroupRunState(state=GroupState.RUNNING),
        },
    )

    entries = surprise_residue(paths, state)
    reasons = {entry.bucket: entry.reason for entry in entries}

    assert reasons["g1"] == REASON_GROUP_COMPLETED
    assert reasons["g2"] == REASON_RUN_ENDED
    assert reasons[SurpriseBoard.RUN_LEVEL] == REASON_UNKNOWN_GROUP
    report = format_residue_report(entries)
    for bucket in reasons:
        assert bucket in report
