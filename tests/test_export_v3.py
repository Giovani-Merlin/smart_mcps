"""Run Bundle v3: per-event usage, peak context, phases, denial links, wait tags."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.execution.export import SCHEMA_VERSION, build_export
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.round_signals import WAIT_COMMAND_RE
from orchestrator.execution.transcript_events import read_events_gz
from orchestrator.model import GroupManifestEntry, SessionEntry, SessionRole
from tests.test_export import _write_run

FIXTURE_RUN = Path("tests/fixtures/runs/r20260828-220035")


def _line(kind: str, uuid: str, content: list, usage: dict | None = None) -> str:
    message: dict = {"role": kind, "content": content}
    if usage is not None:
        message["usage"] = usage
    return json.dumps(
        {"type": kind, "uuid": uuid, "timestamp": "2026-01-01T00:00:00Z", "message": message}
    )


def _bash(uuid: str, tool_id: str, command: str) -> str:
    block = {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}
    return _line("assistant", uuid, [block])


def _transcript(root: Path, session_id: str, lines: list[str]) -> None:
    directory = root / "slug"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")


def _run(tmp_path: Path, **session_fields):
    entry = SessionEntry(
        session_id="aaa",
        role=SessionRole.CODER,
        generation=1,
        name="n",
        started_at="2026-01-01T00:00:00+00:00",
        **session_fields,
    )
    return _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1", group_name="alpha", summary="s", sessions=[entry]
            )
        },
        states={"g1": {"state": "completed"}},
    )


def _export(paths: RunPaths, root: Path):
    events_dir = paths.run_dir / "ingest" / "events"
    export = build_export(paths, project="p", events_dir=events_dir, transcript_root=root)
    return export, events_dir


def test_usage_on_assistant_events_and_peak_context(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    usage = {
        "input_tokens": 3,
        "output_tokens": 5,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 11,
    }
    _transcript(
        root,
        "aaa",
        [
            _line("user", "u1", [{"type": "text", "text": "hi"}]),
            _line("assistant", "a1", [{"type": "text", "text": "ok"}], usage),
            _line("user", "u2", [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]),
            _line("assistant", "a2", [{"type": "text", "text": "done"}], usage),
        ],
    )
    paths = _run(tmp_path, last_context_tokens=40, peak_context_tokens=90)
    export, events_dir = _export(paths, root)

    assert export.schema_version == SCHEMA_VERSION == 3
    session = export.groups[0].sessions[0]
    assert session.peak_context_tokens >= session.last_context_tokens == 40
    events = read_events_gz(events_dir / "aaa.jsonl.gz")
    with_usage = [e for e in events if e.usage is not None]
    assert [e.event_id for e in with_usage] == ["a1", "a2"]
    assert with_usage[0].usage.model_dump() == {
        "input": 3,
        "output": 5,
        "cache_read": 7,
        "cache_creation": 11,
    }
    assert all(e.usage is None for e in events if e.role != "assistant")


def test_phases_from_fixture_run_log() -> None:
    paths = RunPaths(Path("."), "r20260828-220035", run_dir=FIXTURE_RUN)
    export = build_export(paths, project="fx")
    assert export.groups
    for group in export.groups:
        names = [p.phase for p in group.phases]
        assert names, group.id
        assert names[0] in ("worktree ready", "round 1: started")
        assert names[-1] in ("completed", "failed")
        ats = [p.at for p in group.phases]
        assert ats == sorted(ats)
        assert all(s.peak_context_tokens == 0 for s in group.sessions)


def test_denial_event_id_links_the_denied_bash_call(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _transcript(
        root,
        "aaa",
        [
            _line("user", "u1", [{"type": "text", "text": "go"}]),
            _bash("b1", "t1", "git  status"),
            _bash("b2", "t2", "cd x && git log"),
            _bash("b3", "t3", "cd x && git log"),
        ],
    )
    paths = _run(tmp_path)
    group_dir = paths.group_dir("g1")
    group_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        group_dir / "report-g1-r1.json",
        json.dumps(
            {
                "status": "permission_denied",
                "denied_command": "cd x &&  git log",
                "surprises": [{"kind": "other", "description": "a"}, {"kind": "other"}],
            }
        ),
    )
    export, _ = _export(paths, root)
    [artifact] = export.groups[0].artifacts
    assert artifact.denial_event_id == "b3"
    assert [s.seq for s in artifact.surprises] == [0, 1]
    assert all(s.session_id == "aaa" for s in artifact.surprises)


def test_wait_commands_are_tagged(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _transcript(
        root,
        "aaa",
        [
            _line("user", "u1", [{"type": "text", "text": "go"}]),
            _bash("b1", "t1", "kill -0 123"),
            _bash("b2", "t2", "sleep 5"),
            _bash("b3", "t3", "pytest -q"),
        ],
    )
    _, events_dir = _export(_run(tmp_path), root)
    events = {e.event_id: e for e in read_events_gz(events_dir / "aaa.jsonl.gz")}
    assert events["b1"].tags == ["wait"]
    assert events["b2"].tags == ["wait"]
    assert events["b3"].tags == []
    assert WAIT_COMMAND_RE.search("kill -0 1")
