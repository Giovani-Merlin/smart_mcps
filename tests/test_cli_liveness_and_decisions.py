"""Plan U12 — `status` surfaces liveness/cures/activity, `answer --guidance`
opts an answer out of binding, and the rewrite provider carries the group's
Operator Decisions into its skeleton and subject record."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from orchestrator.cli import _rewrite_provider, main
from orchestrator.execution.escalation import answer_escalation
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.grouping.llm import LlmCallMeta, LlmCallResult
from orchestrator.grouping.llm_record import JsonlCallRecorder
from orchestrator.model import (
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    GroupManifestEntry,
    RunManifest,
    SessionEntry,
    SessionRole,
    Surprise,
)

from test_review_loop import make_group


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).isoformat(timespec="milliseconds")


def _write_state(paths: RunPaths, *, group_state: GroupState = GroupState.RUNNING) -> None:
    atomic_write_text(
        paths.state_path,
        RunState(
            run_id=paths.run_id, groups={"g1": GroupRunState(state=group_state)}
        ).model_dump_json(),
    )


def _write_manifest(paths: RunPaths, *, transcript_path: Path | None) -> None:
    manifest = RunManifest(run_id=paths.run_id, plan_path="plan.md", base_session_id="base-sid")
    manifest.groups["g1"] = GroupManifestEntry(
        group_id="g1",
        group_name="group g1",
        summary="summary g1",
        sessions=[
            SessionEntry(
                session_id="s1",
                role=SessionRole.CODER,
                name="r1-g1-coder-g1",
                transcript_path=str(transcript_path) if transcript_path else None,
            )
        ],
    )
    ManifestStore(paths).save(manifest)


def _write_heartbeat(
    paths: RunPaths,
    gid: str,
    *,
    now: float,
    window_s: float = 600,
    age_s: float,
    cures: int = 0,
    max_cures: int = 2,
    phase: str = "round 1 running",
    liveness: bool = True,
    updated_age_s: float = 0.0,
) -> None:
    hb_path = paths.group_dir(gid) / "heartbeat.json"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "group_id": gid,
        "phase": phase,
        "updated_at": _iso(now - updated_age_s),
    }
    if liveness:
        # A `run` recipe group's heartbeat carries no worker child and no
        # liveness facts — only the phase (``liveness=False``).
        payload.update(
            {
                "child_pid": os.getpid(),
                "child_spawned_at": _iso(now - age_s - 10),
                "last_sign_of_life_at": _iso(now - age_s),
                "sign_of_life_signal": "event",
                "sign_of_life_evidence": "event assistant a while ago",
                "liveness_window_s": window_s,
                "cures": cures,
                "max_cures_per_generation": max_cures,
            }
        )
    atomic_write_text(hb_path, json.dumps(payload))


def _write_driver_record(paths: RunPaths) -> None:
    atomic_write_text(
        paths.driver_record_path,
        json.dumps({"pid": os.getpid(), "started_at": _iso(0), "updated_at": _iso(0)}),
    )


def _write_transcript_with_tool_calls(path: Path, *, n: int = 5) -> None:
    lines = []
    for i in range(1, n + 1):
        ts = f"2026-01-01T00:{i:02d}:00Z"
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"a{i}",
                    "timestamp": ts,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"toolu_{i}",
                                "name": "Read",
                                "input": {"file_path": f"/file{i}"},
                            }
                        ],
                    },
                }
            )
        )
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"u{i}",
                    "timestamp": ts,
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "ok"}
                        ],
                    },
                }
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------- status


class TestStatusLivenessAndActivity:
    def test_status_reports_not_live_and_activity_tail(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        _write_state(paths)
        transcript = tmp_path / "transcript.jsonl"
        _write_transcript_with_tool_calls(transcript, n=5)
        _write_manifest(paths, transcript_path=transcript)
        now = 2_000_000_000.0
        _write_heartbeat(paths, "g1", now=now, window_s=600, age_s=1200, cures=0, max_cures=2)
        _write_driver_record(paths)

        import time as time_mod

        original_time = time_mod.time
        time_mod.time = lambda: now
        try:
            exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        finally:
            time_mod.time = original_time
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "liveness: NOT LIVE for" in out
        assert "cures exhausted" not in out
        assert out.count("[returned]") == 5

    def test_status_reports_cures_exhausted(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        _write_state(paths)
        _write_manifest(paths, transcript_path=None)
        now = 2_000_000_000.0
        _write_heartbeat(paths, "g1", now=now, window_s=600, age_s=1200, cures=2, max_cures=2)
        _write_driver_record(paths)

        import time as time_mod

        original_time = time_mod.time
        time_mod.time = lambda: now
        try:
            exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        finally:
            time_mod.time = original_time
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "cures exhausted (2/2" in out
        assert "kill -INT -" in out
        assert "resume r1" in out

    def test_status_prints_the_command_phase_for_a_run_group_heartbeat(self, tmp_path, capsys):
        # r20260925-101742: `status` printed a bare `g5: running (generation 1)`
        # for a run group's whole command phase — it has no worker child, no
        # liveness facts and (before the runner session) no manifest entry.
        paths = RunPaths(tmp_path, "r1")
        _write_state(paths)
        ManifestStore(paths).save(RunManifest(run_id="r1", plan_path="plan.md"))
        now = 2_000_000_000.0
        _write_heartbeat(
            paths,
            "g1",
            now=now,
            age_s=0,
            phase="command 2/5 · 71s/1200s",
            liveness=False,
            updated_age_s=3,
        )
        _write_driver_record(paths)

        import time as time_mod

        original_time = time_mod.time
        time_mod.time = lambda: now
        try:
            exit_code = main(["status", "r1", "--repo", str(tmp_path)])
        finally:
            time_mod.time = original_time
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "g1: running (generation 1)" in out
        assert "phase: command 2/5 · 71s/1200s (updated 3s ago)" in out
        assert "liveness:" not in out


# --------------------------------------------------------------------- answer


class TestAnswerGuidance:
    def _write_request(self, paths: RunPaths, esc_id: str = "e1") -> None:
        request = EscalationRequest(
            id=esc_id,
            run_id=paths.run_id,
            group_id="g1",
            generation=1,
            kind=EscalationKind.CODER_QUESTION,
            prompt="which lib?",
            context=EscalationContext(),
        )
        directory = paths.escalations_dir
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(directory / f"request-{esc_id}.json", request.model_dump_json())

    def test_guidance_flag_writes_binding_false(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        self._write_request(paths, "e1")
        exit_code = main(
            ["answer", "r1", "e1", "--guidance", "--text", "x", "--repo", str(tmp_path)]
        )
        assert exit_code == 0
        response = json.loads((paths.escalations_dir / "response-e1.json").read_text())
        assert response["binding"] is False

    def test_default_answer_is_binding(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        self._write_request(paths, "e1")
        exit_code = main(["answer", "r1", "e1", "--text", "x", "--repo", str(tmp_path)])
        assert exit_code == 0
        response = json.loads((paths.escalations_dir / "response-e1.json").read_text())
        assert response["binding"] is True

    def test_guidance_with_non_answer_action_rejected(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        self._write_request(paths, "e1")
        exit_code = main(
            [
                "answer",
                "r1",
                "e1",
                "--guidance",
                "--action",
                "retry",
                "--text",
                "fixed it",
                "--repo",
                str(tmp_path),
            ]
        )
        assert exit_code == 2
        assert not (paths.escalations_dir / "response-e1.json").is_file()


# ------------------------------------------------------------- rewrite provider


def _stub_speccer_runner(prompt: str, schema: dict) -> LlmCallResult:
    payload = json.dumps(
        {
            "groups": [
                {
                    "group_id": "g1",
                    "name": "group g1",
                    "summary": "rewritten summary",
                    "spec": "spec g1 v2",
                    "verification": [{"id": "v1", "description": "tests pass"}],
                }
            ]
        }
    )
    meta = LlmCallMeta(
        session_id="sess-rewrite-1",
        model="claude-opus-5",
        duration_ms=1200,
        input_tokens=500,
        output_tokens=120,
        cache_read_tokens=10,
        cache_creation_tokens=5,
    )
    return LlmCallResult(payload, meta)


class TestRewriteProviderCarriesDecisions:
    def test_rewrite_provider_operator_decisions_reach_skeleton_and_subject(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        esc_id = "e1"
        request = EscalationRequest(
            id=esc_id,
            run_id="r1",
            group_id="g1",
            generation=1,
            kind=EscalationKind.CODER_QUESTION,
            prompt="which lib should I use?",
            context=EscalationContext(),
        )
        paths.escalations_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            paths.escalations_dir / f"request-{esc_id}.json", request.model_dump_json()
        )
        answer_escalation(paths, esc_id, "answer", "use httpx, it is already a dependency")

        recorder = JsonlCallRecorder(paths.run_dir, grouping_run_id="r1")
        rewrite_spec = _rewrite_provider(
            "plan text",
            _stub_speccer_runner,
            tmp_path / "failures",
            recorder=recorder,
            paths=paths,
        )
        group = make_group("g1")
        surprises = [Surprise(kind="other", description="something broke", affected_groups=["g1"])]

        rewritten = rewrite_spec(group, surprises)
        assert rewritten.spec == "spec g1 v2"

        index = json.loads((paths.run_dir / "llm" / "calls.json").read_text())
        assert len(index["calls"]) == 1
        call = index["calls"][0]
        assert call["subject"]["operator_decisions"] == 1

        request_file = paths.run_dir / "llm" / call["request_file"]
        request_text = request_file.read_text()
        assert "use httpx, it is already a dependency" in request_text
        assert "which lib should I use?" in request_text
