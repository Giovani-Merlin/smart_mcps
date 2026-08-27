"""The `export` command's ingest.json contract (Infinity Skills ingestion v1).

Fixture-run-dir tests: every case builds a run directory the way real runs
write theirs (ManifestStore + atomic files), then asserts on the composed
contract — chronological ordering, transcript re-resolution by session id,
stale-failure normalization, artifact/escalation summarization, and old-run
tolerance (absent fields → null, never invented values).
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
from orchestrator.model import (
    CoderReport,
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    GroupManifestEntry,
    HumanAction,
    ReviewerVerdict,
    RunManifest,
    SessionEntry,
    SessionRole,
    Surprise,
)

RUN_ID = "r20260101-000000"


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


def _write_run(
    tmp_path: Path,
    *,
    groups: dict[str, GroupManifestEntry],
    states: dict[str, dict] | None = None,
    base_session_id: str | None = "base-0000",
) -> RunPaths:
    repo = tmp_path / "repo"
    paths = RunPaths(repo, RUN_ID)
    paths.run_dir.mkdir(parents=True)
    manifest = RunManifest(
        run_id=RUN_ID,
        plan_path="docs/plan.md",
        base_session_id=base_session_id,
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
    (paths.run_dir / "base-context.md").write_text("shared base context\n")
    return paths


def _export(paths: RunPaths, transcript_root: Path):
    return build_export(paths, project="proj", transcript_root=transcript_root)


# ---------------------------------------------------------------- composition


def test_exports_run_group_session_join(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    (root / "slug").mkdir(parents=True)
    (root / "slug" / "aaa.jsonl").write_text("{}\n")
    (root / "slug" / "base-0000.jsonl").write_text("{}\n")
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
    export = _export(paths, root)

    assert export.schema_version == SCHEMA_VERSION
    assert export.framework == "smart-mcps-orchestrator"
    assert export.project == "proj"
    assert export.plan_path == "docs/plan.md"
    assert export.base_session is not None
    assert export.base_session.session_id == "base-0000"
    assert export.base_session.transcript_missing is False
    assert export.base_session.base_context_path == "base-context.md"
    assert export.base_session.base_context_sha256 is not None

    [group] = export.groups
    assert (group.id, group.name, group.final_state) == ("g1", "alpha", "completed")
    [session] = group.sessions
    assert session.role == "coder"
    assert session.transcript_path == str(root / "slug" / "aaa.jsonl")
    assert session.transcript_missing is False
    assert session.tokens.output == 7


def test_transcript_reresolved_by_session_id_and_missing_marked(tmp_path: Path) -> None:
    """A null or stale recorded path re-globs by session id; nothing on disk
    marks the session, never fails the export."""
    root = tmp_path / "projects"
    (root / "slug").mkdir(parents=True)
    (root / "slug" / "found.jsonl").write_text("{}\n")
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="alpha",
                summary="s",
                sessions=[
                    _session("found", SessionRole.CODER, transcript_path="/nonexistent/x.jsonl"),
                    _session("gone", SessionRole.REVIEWER),
                ],
            )
        },
    )
    [group] = _export(paths, root).groups
    by_id = {s.session_id: s for s in group.sessions}
    assert by_id["found"].transcript_path == str(root / "slug" / "found.jsonl")
    assert by_id["found"].transcript_missing is False
    assert by_id["gone"].transcript_missing is True


def test_groups_and_sessions_in_chronological_order(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={
            # Manifest order is g1 first; g2 started earlier and must lead.
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="late",
                summary="s",
                sessions=[
                    _session(
                        "b2",
                        SessionRole.CODER,
                        generation=2,
                        started_at="2026-01-01T02:00:00+00:00",
                    ),
                    _session("b1", SessionRole.CODER, started_at="2026-01-01T01:00:00+00:00"),
                ],
            ),
            "g2": GroupManifestEntry(
                group_id="g2",
                group_name="early",
                summary="s",
                sessions=[
                    _session("a1", SessionRole.CODER, started_at="2026-01-01T00:30:00+00:00")
                ],
            ),
            "g3": GroupManifestEntry(group_id="g3", group_name="never-ran", summary="s"),
        },
    )
    export = _export(paths, root)
    assert [g.id for g in export.groups] == ["g2", "g1", "g3"]
    assert [s.session_id for s in export.groups[1].sessions] == ["b1", "b2"]


def test_stale_failure_normalized_to_null(tmp_path: Path) -> None:
    """A failure string on a completed state is history, not this group's
    outcome — exported null with the flag set."""
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


# ------------------------------------------------------------------ artifacts


def test_artifacts_inline_status_surprises_and_denials(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
    store = ManifestStore(paths)
    store.save_group_artifact(
        "g1",
        "report-g1-r1.json",
        CoderReport(
            status="completed",
            surprises=[Surprise(kind="other", description="found it", affected_groups=["g2"])],
        ),
    )
    store.save_group_artifact(
        "g1",
        "report-g2-r1.json",
        CoderReport(
            status="permission_denied",
            denied_command="git push",
            denial_error="EACCES: permission denied",
            denial_source="command_error",
        ),
    )
    store.save_group_artifact(
        "g1",
        "verdict-g1-r1.json",
        ReviewerVerdict(status="changes_required", required_changes=["fix the test"]),
    )
    # Non-round bookkeeping in the group dir must not become an artifact.
    (paths.group_dir("g1") / "heartbeat.json").write_text("{}")

    [group] = _export(paths, root).groups
    assert [(a.kind, a.generation, a.round) for a in group.artifacts] == [
        ("coder_report", 1, 1),
        ("reviewer_verdict", 1, 1),
        ("coder_report", 2, 1),
    ]
    report, verdict, denied = group.artifacts
    assert report.status == "completed"
    assert report.path == "groups/g1/report-g1-r1.json"
    assert [s.description for s in report.surprises] == ["found it"]
    assert report.surprises[0].affected_groups == ["g2"]
    assert verdict.status == "changes_required"
    assert verdict.required_changes == ["fix the test"]
    assert denied.denial_kind == "kernel_denied"
    assert denied.denied_command == "git push"


def test_half_written_artifact_lists_with_error(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
    torn = paths.group_dir("g1") / "report-g1-r1.json"
    torn.parent.mkdir(parents=True)
    torn.write_text('{"status": "comp')
    [group] = _export(paths, root).groups
    [artifact] = group.artifacts
    assert artifact.error is not None
    assert artifact.status is None


# ---------------------------------------------------------------- escalations


def test_escalations_attach_to_their_group_with_response(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
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
    response = EscalationResponse(id="esc-1", action=HumanAction.ANSWER, answer="sqlite")
    (paths.escalations_dir / "response-esc-1.json").write_text(response.model_dump_json())

    [group] = _export(paths, root).groups
    [escalation] = group.escalations
    assert escalation.kind == "coder_question"
    assert escalation.prompt == "which db?"
    assert escalation.action == "answer"
    assert escalation.answer == "sqlite"
    assert escalation.request_path == "escalations/request-esc-1.json"
    assert escalation.response_path == "escalations/response-esc-1.json"


# ------------------------------------------------------------------ tolerance


def test_old_run_missing_fields_export_as_null(tmp_path: Path) -> None:
    """A manifest written before newer fields existed: no state.json, no
    started_at, no base-context — nulls, never invented values."""
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={
            "g1": GroupManifestEntry(
                group_id="g1",
                group_name="a",
                summary="s",
                sessions=[_session("old", SessionRole.CODER)],
            )
        },
    )
    (paths.run_dir / "base-context.md").unlink()
    export = _export(paths, root)
    assert export.base_session is not None
    assert export.base_session.base_context_path is None
    assert export.base_session.base_context_sha256 is None
    [group] = export.groups
    assert group.final_state == "pending"  # no state.json → never observed running
    [session] = group.sessions
    assert session.started_at is None
    assert session.ended_at is None
    assert session.model is None


def test_export_run_writes_ingest_json(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    paths = _write_run(
        tmp_path,
        groups={"g1": GroupManifestEntry(group_id="g1", group_name="a", summary="s")},
    )
    destination = export_run(paths.repo_root, RUN_ID, project="proj", transcript_root=root)
    assert destination == paths.run_dir / "ingest.json"
    payload = json.loads(destination.read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["groups"][0]["id"] == "g1"


def test_missing_run_dir_is_an_export_error(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "repo", "nope")
    try:
        build_export(paths, project="proj", transcript_root=tmp_path)
    except ExportError as exc:
        assert "no run directory" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ExportError")
