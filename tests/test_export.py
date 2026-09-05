"""The `export` command's ``ingest/`` package contract (Run Bundle v2).

Fixture-run-dir tests: every case builds a run directory the way real runs
write theirs (ManifestStore + atomic files), then asserts on the composed
contract — package layout, base-context stripping, rewrite-history
assembly, escalation collection from both locations, and old-run tolerance
(absent fields -> null, never invented values).
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.execution.export import (
    SCHEMA_VERSION,
    ExportError,
    build_export,
    export_run,
)
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.transcript_events import read_events_gz
from orchestrator.model import (
    CoderReport,
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    Group,
    GroupManifestEntry,
    GroupingResult,
    HumanAction,
    ReviewIntensity,
    RunManifest,
    SessionEntry,
    SessionRole,
    Surprise,
)

RUN_ID = "r20260101-000000"
BASE_CONTEXT = "# Base context\n\nSome shared rules here."


def _session(
    sid: str,
    role: SessionRole,
    *,
    generation: int = 1,
    started_at: str | None = None,
    transcript_path: str | None = None,
) -> SessionEntry:
    return SessionEntry(
        session_id=sid,
        role=role,
        generation=generation,
        name=f"{RUN_ID}-{sid}",
        started_at=started_at,
        transcript_path=transcript_path,
        total_output_tokens=7,
    )


def _group(gid: str, **overrides) -> Group:
    defaults = dict(
        id=gid,
        name=f"group {gid}",
        summary="s",
        spec="do the thing",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=[],
    )
    defaults.update(overrides)
    return Group(**defaults)


def _write_run(
    tmp_path: Path,
    *,
    groups: dict[str, GroupManifestEntry],
    states: dict[str, dict] | None = None,
    with_base_context: bool = True,
    grouping_groups: list[Group] | None = None,
) -> RunPaths:
    repo = tmp_path / "repo"
    paths = RunPaths(repo, RUN_ID)
    paths.run_dir.mkdir(parents=True)
    manifest = RunManifest(
        run_id=RUN_ID,
        plan_path="docs/plan.md",
        base_session_id=None,
        groups=groups,
    )
    ManifestStore(paths).save(manifest)
    if states is not None:
        state = {
            "run_id": RUN_ID,
            "schema_version": 2,
            "groups": {
                gid: {"state": "pending", "generation": 1, "failure": None, **entry}
                for gid, entry in states.items()
            },
            "live_pids": {},
            "interrupted_at": None,
        }
        atomic_write_text(paths.state_path, json.dumps(state))
    if with_base_context:
        (paths.run_dir / "base-context.md").write_text(BASE_CONTEXT)
    grouping = GroupingResult(
        plan_path="docs/plan.md",
        groups=grouping_groups if grouping_groups is not None else [_group(gid) for gid in groups],
    )
    atomic_write_text(paths.groups_path, grouping.model_dump_json())
    return paths


def _write_transcript(
    root: Path, slug: str, session_id: str, text_after_base: str | None = None
) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    first_text = (
        f"{BASE_CONTEXT}\n\n{text_after_base}"
        if text_after_base is not None
        else "no base context here"
    )
    lines = [
        json.dumps(
            {
                "type": "user",
                "uuid": f"{session_id}-u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": [{"type": "text", "text": first_text}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"{session_id}-a1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _export(paths: RunPaths, transcript_root: Path, events_dir: Path | None = None):
    return build_export(
        paths,
        project="proj",
        events_dir=events_dir or (paths.run_dir / "ingest" / "events"),
        transcript_root=transcript_root,
    )


# ---------------------------------------------------------------- composition


def test_package_layout_and_manifest_shape(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="do the g1 task")
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="alpha",
                summary="does alpha",
                sessions=[
                    _session("aaa", SessionRole.CODER, started_at="2026-01-01T00:00:05+00:00")
                ],
            )
        },
        states={"g1": {"state": "completed"}},
    )
    destination = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    assert destination == paths.run_dir / "ingest"
    manifest_path = destination / "ingest.json"
    assert manifest_path.is_file()
    payload = json.loads(manifest_path.read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "base_session" not in payload
    assert payload["framework"] == "smart-mcps-orchestrator"
    assert payload["plan"]["path"] == "docs/plan.md"

    [group] = payload["groups"]
    assert group["id"] == "g1"
    assert group["spec"]["id"] == "g1"
    [session] = group["sessions"]
    assert session["events_path"] == "events/aaa.jsonl.gz"
    events_file = destination / session["events_path"]
    assert events_file.is_file()
    events = read_events_gz(events_file)
    assert len(events) == session["events_count"] > 0


def test_base_context_recorded_and_hashed(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
    import hashlib

    expected_sha = hashlib.sha256((paths.run_dir / "base-context.md").read_bytes()).hexdigest()
    export = _export(paths, root)
    assert export.base_context is not None
    assert export.base_context.path == "base-context.md"
    assert export.base_context.sha256 == expected_sha
    assert export.base_context.char_len == len(BASE_CONTEXT)


def test_no_base_context_file_exports_null(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
        with_base_context=False,
    )
    export = _export(paths, root)
    assert export.base_context is None


# ---------------------------------------------------------------------- strip


def test_strip_applied_when_transcript_opens_with_base_context(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="worker prompt")
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="a",
                summary="s",
                sessions=[_session("aaa", SessionRole.CODER)],
            )
        },
    )
    [group] = _export(paths, root).groups
    [session] = group.sessions
    assert session.base_context_stripped is True
    events = read_events_gz(paths.run_dir / "ingest" / "events" / "aaa.jsonl.gz")
    assert events[0].text == "worker prompt"


def test_non_matching_transcript_exported_whole(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "bbb", text_after_base=None)  # no base-context prefix
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="a",
                summary="s",
                sessions=[_session("bbb", SessionRole.CODER)],
            )
        },
    )
    [group] = _export(paths, root).groups
    [session] = group.sessions
    assert session.base_context_stripped is False
    events = read_events_gz(paths.run_dir / "ingest" / "events" / "bbb.jsonl.gz")
    assert events[0].text == "no base context here"


# ------------------------------------------------------------------- rewrites


def test_rewrite_history_assembled_in_order(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
    store = ManifestStore(paths)
    gen1_group = _group("g1", spec="spec v1")
    gen2_group = _group("g1", spec="spec v2")
    (paths.group_dir("g1")).mkdir(parents=True, exist_ok=True)
    atomic_write_text(paths.group_dir("g1") / "spec-gen2.json", gen2_group.model_dump_json())
    atomic_write_text(paths.group_dir("g1") / "spec-gen1.json", gen1_group.model_dump_json())

    request = EscalationRequest(
        id="esc-1",
        run_id=RUN_ID,
        group_id="g1",
        generation=1,
        kind=EscalationKind.CODER_QUESTION,
        prompt="which db?",
        context=EscalationContext(),
    )
    paths.escalations_dir.mkdir(parents=True)
    (paths.escalations_dir / "request-esc-1.json").write_text(request.model_dump_json())

    store.save_group_artifact(
        "g1",
        "report-g1-r1.json",
        CoderReport(
            status="needs_input",
            question="which db?",
            surprises=[Surprise(kind="other", description="trigger for gen1")],
        ),
    )

    [group] = _export(paths, root).groups
    assert [r.generation for r in group.rewrites] == [1, 2]
    assert group.rewrites[0].spec["spec"] == "spec v1"
    assert group.rewrites[1].spec["spec"] == "spec v2"
    assert group.rewrites[0].escalation_ids == ["esc-1"]
    assert [s.description for s in group.rewrites[0].triggering_surprises] == ["trigger for gen1"]
    assert group.rewrites[1].escalation_ids == []


# ------------------------------------------------------------------ escalations


def test_escalations_found_in_both_locations(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
    request_a = EscalationRequest(
        id="esc-top",
        run_id=RUN_ID,
        group_id="g1",
        generation=1,
        kind=EscalationKind.CODER_QUESTION,
        prompt="top-level",
        context=EscalationContext(),
    )
    paths.escalations_dir.mkdir(parents=True)
    (paths.escalations_dir / "request-esc-top.json").write_text(request_a.model_dump_json())
    response_a = EscalationResponse(id="esc-top", action=HumanAction.ANSWER, answer="sqlite")
    (paths.escalations_dir / "response-esc-top.json").write_text(response_a.model_dump_json())

    request_b = EscalationRequest(
        id="esc-group",
        run_id=RUN_ID,
        group_id="g1",
        generation=2,
        kind=EscalationKind.PREFLIGHT_FAILED,
        prompt="group-dir",
        context=EscalationContext(),
    )
    group_dir = paths.group_dir("g1")
    group_dir.mkdir(parents=True, exist_ok=True)
    (group_dir / "request-esc-group.json").write_text(request_b.model_dump_json())

    [group] = _export(paths, root).groups
    ids = {e.id for e in group.escalations}
    assert ids == {"esc-top", "esc-group"}
    by_id = {e.id: e for e in group.escalations}
    assert by_id["esc-top"].action == "answer"
    assert by_id["esc-top"].answer == "sqlite"
    assert by_id["esc-group"].response_path is None


# ------------------------------------------------------------------ tolerance


def test_missing_transcript_writes_no_events_file(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="a",
                summary="s",
                sessions=[_session("gone", SessionRole.CODER)],
            )
        },
    )
    [group] = _export(paths, root).groups
    [session] = group.sessions
    assert session.transcript_missing is True
    assert session.events_path is None
    assert session.events_count == 0
    assert not (paths.run_dir / "ingest" / "events" / "gone.jsonl.gz").exists()


def test_stale_failure_normalized_to_null(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s"),
            "g2": GroupManifestEntry(group_id="g2", group_name="b", summary="s"),
        },
        states={
            "g1": {"state": "completed", "failure": "old failure text"},
            "g2": {"state": "failed", "failure": "real failure"},
        },
    )
    by_id = {g.id: g for g in _export(paths, root).groups}
    assert by_id["g1"].failure is None
    assert by_id["g1"].stale_failure is True
    assert by_id["g2"].failure == "real failure"
    assert by_id["g2"].stale_failure is False


def test_reexport_overwrites_package_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="task")
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="a",
                summary="s",
                sessions=[_session("aaa", SessionRole.CODER)],
            )
        },
    )
    first = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    second = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    assert first == second
    payload = json.loads((second / "ingest.json").read_text())
    assert payload["groups"][0]["sessions"][0]["session_id"] == "aaa"


def test_missing_run_dir_is_an_export_error(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "repo", "nope")
    try:
        build_export(
            paths, project="proj", events_dir=tmp_path / "events", transcript_root=tmp_path
        )
    except ExportError as exc:
        assert "no run directory" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ExportError")


# ----------------------------------------------------- orchestrator llm calls


def _write_llm_index(paths: RunPaths, calls: list[dict]) -> None:
    """The recorder's own on-disk shape (`JsonlCallRecorder._write_index`)."""
    directory = paths.run_dir / "llm"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "calls.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "grouping_run_id": RUN_ID,
                "produced": None,
                "calls": calls,
            },
            indent=2,
        )
        + "\n"
    )


def _llm_call(seq: int, session_id: str | None, transcript: Path | None, **overrides) -> dict:
    call = {
        "seq": seq,
        "recorded_at": f"2026-01-01T00:0{seq}:00+00:00",
        "gen_ai.operation.name": "speccer_output",
        "gen_ai.request.model": "claude-opus-5",
        "attempt": 0,
        "status": {"code": "ok"},
        "error": None,
        "claude.session_id": session_id,
        "claude.transcript_path": str(transcript) if transcript else None,
        "gen_ai.usage.input_tokens": 4,
        "gen_ai.usage.output_tokens": 11,
        "claude.usage.cache_read_tokens": 90,
        "claude.usage.cache_creation_tokens": 5,
        "duration_ms": 1234,
        "schema_title": "speccer_output",
        "request_file": f"{seq:02d}-speccer_output-a0.request.txt",
        "raw_file": f"{seq:02d}-speccer_output-a0.raw.txt",
    }
    call.update(overrides)
    return call


def _run_with_one_group(tmp_path: Path) -> RunPaths:
    return _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="alpha",
                summary="does alpha",
                sessions=[
                    _session("aaa", SessionRole.CODER, started_at="2026-01-01T00:00:05+00:00")
                ],
            )
        },
        states={"g1": {"state": "completed"}},
    )


def test_llm_calls_exported_in_order_with_events(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="do the g1 task")
    resolvable = _write_transcript(root, "slug", "spec-1")
    paths = _run_with_one_group(tmp_path)
    _write_llm_index(
        paths,
        [
            _llm_call(2, "spec-1", resolvable),
            # Out of file order: the export sorts by seq, not by position.
            _llm_call(1, "gone-forever", tmp_path / "vanished" / "gone-forever.jsonl"),
        ],
    )

    destination = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    payload = json.loads((destination / "ingest.json").read_text())
    missing, present = payload["llm_calls"]

    assert [call["seq"] for call in payload["llm_calls"]] == [1, 2]
    assert present["operation"] == "speccer_output"
    assert present["model"] == "claude-opus-5"
    assert present["status"] == "ok"
    assert present["attempt"] == 0
    assert present["duration_ms"] == 1234
    assert present["session_id"] == "spec-1"
    assert present["tokens"] == {"input": 4, "output": 11, "cache_read": 90, "cache_creation": 5}
    assert present["transcript_missing"] is False
    assert present["events_path"] == "events/spec-1.jsonl.gz"
    events_file = destination / present["events_path"]
    assert events_file.is_file()
    assert len(read_events_gz(events_file)) == present["events_count"] > 0

    # An unresolvable transcript is flagged, never faked, and writes no file.
    assert missing["transcript_missing"] is True
    assert missing["events_path"] is None
    assert missing["events_count"] == 0
    assert not (destination / "events" / "gone-forever.jsonl.gz").exists()


def test_llm_call_transcript_keeps_its_whole_first_message(tmp_path: Path) -> None:
    """No base-context strip: a speccer prompt never carries base context, and
    a partial strip of a non-matching prefix would silently lose the prompt."""
    root = tmp_path / "projects"
    # Deliberately opens with the byte-exact base context anyway.
    transcript = _write_transcript(root, "slug", "spec-1", text_after_base="rewrite g1")
    paths = _run_with_one_group(tmp_path)
    _write_llm_index(paths, [_llm_call(1, "spec-1", transcript)])

    destination = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    payload = json.loads((destination / "ingest.json").read_text())
    [call] = payload["llm_calls"]
    assert "base_context_stripped" not in call
    events = read_events_gz(destination / call["events_path"])
    assert events[0].text == f"{BASE_CONTEXT}\n\nrewrite g1"


def test_run_without_llm_dir_exports_empty_list(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="do the g1 task")
    paths = _run_with_one_group(tmp_path)
    assert not (paths.run_dir / "llm").exists()

    export = _export(paths, root)
    assert export.llm_calls == []
    payload = json.loads(export.model_dump_json())
    assert payload["llm_calls"] == []


def test_unreadable_llm_index_degrades_to_empty_list(tmp_path: Path) -> None:
    """The audit trail is inert by contract on both sides — losing it must
    never fail a bundle that is otherwise whole."""
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="do the g1 task")
    paths = _run_with_one_group(tmp_path)
    (paths.run_dir / "llm").mkdir()
    (paths.run_dir / "llm" / "calls.json").write_text("{not json")

    export = _export(paths, root)
    assert export.llm_calls == []
    assert export.groups[0].sessions[0].events_count > 0


def test_failed_llm_call_without_session_is_flagged_missing(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    _write_transcript(root, "slug", "aaa", text_after_base="do the g1 task")
    paths = _run_with_one_group(tmp_path)
    _write_llm_index(
        paths,
        [
            _llm_call(
                1,
                None,
                None,
                **{
                    "gen_ai.request.model": None,
                    "status": {"code": "error"},
                    "error": "schema repair exhausted",
                    "attempt": 2,
                    "duration_ms": None,
                },
            )
        ],
    )

    export = _export(paths, root)
    [call] = export.llm_calls
    assert call.status == "error"
    assert call.error == "schema repair exhausted"
    assert call.attempt == 2
    assert call.model is None
    assert call.duration_ms is None
    assert call.session_id is None
    assert call.transcript_missing is True
    assert call.events_path is None
