"""``export`` refuses a metadata-only bundle and a stale ``--out`` unless told to."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.execution import export as export_module
from orchestrator.execution.export import ExportError, export_run
from orchestrator.execution.manifest import RunPaths
from orchestrator.model import GroupManifestEntry, SessionRole
from tests.test_export import RUN_ID, _session, _write_run, _write_transcript


def _two_session_run(tmp_path: Path, *, present: tuple[str, ...]) -> tuple[RunPaths, Path]:
    root = tmp_path / "projects"
    for sid in present:
        _write_transcript(root, "slug", sid, text_after_base="work")
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="alpha",
                summary="s",
                sessions=[
                    _session("aaa", SessionRole.CODER, started_at="2026-01-01T00:00:05+00:00"),
                    _session("bbb", SessionRole.REVIEWER, started_at="2026-01-01T00:00:06+00:00"),
                ],
            )
        },
        states={"g1": {"state": "completed"}},
    )
    return paths, root


def test_complete_run_exports_and_cli_prints_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, root = _two_session_run(tmp_path, present=("aaa", "bbb"))
    out = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    assert (out / "ingest.json").is_file()

    monkeypatch.setattr(export_module, "default_transcript_root", lambda: root)
    code = main(
        ["export", RUN_ID, "--repo", str(paths.repo_root), "--out", str(tmp_path / "o"), "--clear"]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert "2/2 sessions with transcripts, 0 transcript_missing" in stdout
    assert stdout.index("2/2") < stdout.index("wrote ")


def test_missing_transcript_refuses_and_writes_nothing(tmp_path: Path) -> None:
    paths, root = _two_session_run(tmp_path, present=("aaa",))
    out = tmp_path / "out"
    with pytest.raises(ExportError, match=r"1/2 .*--allow-missing"):
        export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root, out_dir=out)
    assert not (out / "ingest.json").exists()
    assert not (out / "events").exists()


def test_allow_missing_writes_and_marks_the_session(tmp_path: Path) -> None:
    paths, root = _two_session_run(tmp_path, present=("aaa",))
    out = export_run(
        paths.repo_root,
        RUN_ID,
        project="proj",
        transcript_root=root,
        out_dir=tmp_path / "out",
        allow_missing=True,
    )
    payload = json.loads((out / "ingest.json").read_text())
    flags = {s["session_id"]: s["transcript_missing"] for s in payload["groups"][0]["sessions"]}
    assert flags == {"aaa": False, "bbb": True}


def test_non_empty_out_refuses_and_clear_removes_only_the_export(tmp_path: Path) -> None:
    paths, root = _two_session_run(tmp_path, present=("aaa", "bbb"))
    out = tmp_path / "out"
    (out / "events").mkdir(parents=True)
    stale = out / "events" / "stale.jsonl.gz"
    stale.write_bytes(b"x")
    notes = out / "notes.txt"
    notes.write_text("keep me")

    with pytest.raises(ExportError, match=r"--clear") as excinfo:
        export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root, out_dir=out)
    assert str(out) in str(excinfo.value)
    assert stale.exists()

    export_run(
        paths.repo_root, RUN_ID, project="proj", transcript_root=root, out_dir=out, clear=True
    )
    assert not stale.exists()
    assert notes.read_text() == "keep me"
    assert (out / "ingest.json").is_file()
    assert (out / "events" / "aaa.jsonl.gz").is_file()
