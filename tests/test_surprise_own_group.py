"""A surprise naming only its own group is dropped with an anchor line."""

from __future__ import annotations

from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.surprises import SurpriseBoard, surprise_residue
from orchestrator.model import Group, Surprise
from tests.test_surprise_board import make_group


def _groups() -> list[Group]:
    return [make_group(gid, [f"t-{gid}"]) for gid in ("g1", "g2")]


def _board(tmp_path) -> tuple[SurpriseBoard, RunPaths]:
    paths = RunPaths(tmp_path, "r1")
    return SurpriseBoard(paths, groups=_groups(), is_settled=lambda gid: False), paths


def _log(paths: RunPaths) -> str:
    path = paths.run_dir / "logs" / "run.log"
    return path.read_text() if path.is_file() else ""


def _s(groups: list[str]) -> Surprise:
    return Surprise(kind="other", description="finding", affected_groups=groups)


def test_own_group_only_is_dropped_with_an_anchor(tmp_path):
    board, paths = _board(tmp_path)
    board.mark(_s(["g1"]), source_group="g1")
    assert "group g1 → (own group; already in its report)" in _log(paths)
    assert not paths.surprises_path.is_file() or not surprise_residue(paths, None)


def test_own_group_via_task_id_is_dropped(tmp_path):
    board, paths = _board(tmp_path)
    board.mark(_s(["t-g1"]), source_group="g1")
    assert "(own group; already in its report)" in _log(paths)
    assert board.pending_for("__run__") == []


def test_empty_affected_groups_lands_in_run_level_with_new_reason(tmp_path):
    board, paths = _board(tmp_path)
    board.mark(_s([]), source_group="g1")
    entries = surprise_residue(paths, None)
    assert [e.bucket for e in entries] == ["__run__"]
    assert entries[0].reason == "no target group named, or an unknown id"


def test_other_group_still_delivered(tmp_path):
    board, _ = _board(tmp_path)
    board.mark(_s(["g2"]), source_group="g1")
    assert len(board.pending_for("g2")) == 1


def test_unknown_id_lands_in_run_level(tmp_path):
    board, _ = _board(tmp_path)
    board.mark(_s(["zzz"]), source_group="g1")
    assert len(board.pending_for("__run__")) == 1
