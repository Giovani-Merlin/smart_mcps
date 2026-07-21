"""U8 tests: the tolerant transcript parser and the group artifact endpoint.

Tolerance is the whole point of this unit — the ``.jsonl`` format belongs to
Claude Code, not to us — so most of these tests feed the parser things it was
not designed for and assert it keeps going: unknown row types, a fabricated
future row type, a malformed line, an absent file. They also cover the transcript
half of R20: a finished run's sessions render from disk alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.model import CoderReport, ReviewerVerdict, VerificationResult
from orchestrator.observatory.app import create_app
from orchestrator.observatory.transcripts import parse_transcript
from test_observatory_api import install_run, write_registry

FIXTURE = Path(__file__).parent / "fixtures" / "observatory" / "transcript.jsonl"
RUN = "/api/projects/proj/runs/smoke1"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The post-mortem run, with its sessions' transcripts pointed at copies of
    the fixture — the manifest stores absolute paths, so they are rewritten per
    test rather than assumed to exist on this machine."""
    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "smoke1")
    paths = RunPaths(repo, "smoke1")
    store = ManifestStore(paths)
    manifest = store.load()
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    for entry in manifest.groups.values():
        for session in entry.sessions:
            copy = transcripts / f"{session.session_id}.jsonl"
            copy.write_text(FIXTURE.read_text())
            session.transcript_path = str(copy)
    store.save(manifest)
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    registry = write_registry(tmp_path, [("proj", repo)])
    return TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))


def session_ids(repo: Path) -> list[str]:
    manifest = ManifestStore(RunPaths(repo, "smoke1")).load()
    return [session.session_id for entry in manifest.groups.values() for session in entry.sessions]


# -------------------------------------------------------------------- parser


class TestParser:
    def test_normalizes_the_block_types_the_pane_renders(self):
        events = parse_transcript(FIXTURE)
        kinds = [(event.role, event.kind) for event in events]
        assert ("assistant", "text") in kinds
        assert ("assistant", "tool_use") in kinds
        assert ("user", "tool_result") in kinds

        tool_use = next(event for event in events if event.kind == "tool_use")
        assert tool_use.tool_name == "Bash"
        assert tool_use.tool_input["command"] == "npm init -y"

        result = next(event for event in events if event.kind == "tool_result")
        assert result.tool_result == "Wrote to /ui/package.json"
        assert result.is_error is False

    def test_a_list_shaped_tool_result_is_flattened_to_text(self):
        errored = [event for event in parse_transcript(FIXTURE) if event.kind == "tool_result"][-1]
        assert errored.tool_result == "EACCES: permission denied"
        assert errored.is_error is True

    def test_a_user_prompt_string_becomes_a_text_event(self):
        first = parse_transcript(FIXTURE)[0]
        assert first.role == "user" and first.kind == "text"
        assert "Scaffold the Vite project." in first.text

    def test_unknown_row_types_are_skipped_silently(self):
        """attachment / custom-title / agent-name / mode / queue-operation /
        last-prompt, plus a fabricated future row type."""
        raw = [json.loads(line) for line in FIXTURE.read_text().splitlines() if _is_json(line)]
        present = {row.get("type") for row in raw}
        assert {
            "attachment",
            "custom-title",
            "agent-name",
            "mode",
            "queue-operation",
            "last-prompt",
            "future-event-type",
        } <= present

        events = parse_transcript(FIXTURE)
        assert events  # it did not just give up
        assert {event.role for event in events} <= {"assistant", "user"}
        assert {event.kind for event in events} <= {"text", "tool_use", "tool_result"}

    def test_a_malformed_line_is_skipped_and_the_rest_still_parse(self):
        assert any(not _is_json(line) for line in FIXTURE.read_text().splitlines())
        events = parse_transcript(FIXTURE)
        # the last renderable row sits after the malformed line
        assert events[-1].text == "Done — 2 files written."

    def test_seq_is_dense_and_ordered(self):
        events = parse_transcript(FIXTURE)
        assert [event.seq for event in events] == list(range(1, len(events) + 1))

    def test_an_empty_transcript_is_an_empty_list(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        assert parse_transcript(empty) == []


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


# ------------------------------------------------------------------ endpoint


class TestTranscriptEndpoint:
    def test_resolves_transcript_path_from_the_manifest(self, client, repo):
        session_id = session_ids(repo)[0]
        response = client.get(f"{RUN}/sessions/{session_id}/transcript")
        assert response.status_code == 200
        body = response.json()
        assert body[0]["seq"] == 1
        assert {event["kind"] for event in body} <= {"text", "tool_use", "tool_result"}
        assert any(event["tool_name"] == "Bash" for event in body)

    def test_a_partially_written_transcript_never_500s(self, client, repo, tmp_path):
        session_id = session_ids(repo)[0]
        path = Path(
            ManifestStore(RunPaths(repo, "smoke1")).load().groups["g2"].sessions[0].transcript_path
        )
        path.write_text(FIXTURE.read_text() + '{"type": "assistant", "message": {"cont')
        response = client.get(f"{RUN}/sessions/{session_id}/transcript")
        assert response.status_code == 200
        assert response.json()

    def test_each_call_re_reads_the_file(self, client, repo):
        """What makes the drill-in's poll work while a session is still writing."""
        session_id = session_ids(repo)[0]
        before = client.get(f"{RUN}/sessions/{session_id}/transcript").json()

        path = Path(
            next(
                session.transcript_path
                for entry in ManifestStore(RunPaths(repo, "smoke1")).load().groups.values()
                for session in entry.sessions
                if session.session_id == session_id
            )
        )
        with path.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "one more turn"}]},
                    }
                )
                + "\n"
            )

        after = client.get(f"{RUN}/sessions/{session_id}/transcript").json()
        assert len(after) == len(before) + 1
        assert after[-1]["text"] == "one more turn"

    def test_an_unknown_session_is_404_naming_it(self, client):
        response = client.get(f"{RUN}/sessions/ghost-session/transcript")
        assert response.status_code == 404
        assert "ghost-session" in response.json()["detail"]

    def test_a_null_transcript_path_is_404_naming_the_session(self, client, repo):
        paths = RunPaths(repo, "smoke1")
        store = ManifestStore(paths)
        manifest = store.load()
        session = manifest.groups["g2"].sessions[0]
        session.transcript_path = None
        store.save(manifest)

        response = client.get(f"{RUN}/sessions/{session.session_id}/transcript")
        assert response.status_code == 404
        assert session.session_id in response.json()["detail"]

    def test_a_missing_transcript_file_is_404_naming_the_session(self, client, repo):
        paths = RunPaths(repo, "smoke1")
        store = ManifestStore(paths)
        manifest = store.load()
        session = manifest.groups["g2"].sessions[0]
        Path(session.transcript_path).unlink()

        response = client.get(f"{RUN}/sessions/{session.session_id}/transcript")
        assert response.status_code == 404
        assert session.session_id in response.json()["detail"]

    def test_r20_transcript_half_every_session_of_a_finished_run_reads(self, client, repo):
        for session_id in session_ids(repo):
            response = client.get(f"{RUN}/sessions/{session_id}/transcript")
            assert response.status_code == 200, session_id
            assert response.json()


# ----------------------------------------------------------------- artifacts


class TestArtifactsEndpoint:
    def test_lists_reports_and_verdicts_with_parsed_contents(self, client):
        body = client.get(f"{RUN}/groups/g1/artifacts").json()
        names = [artifact["name"] for artifact in body]
        assert "report-g1-r1.json" in names
        assert "verdict-g1-r1.json" in names

        report = next(item for item in body if item["name"] == "report-g1-r1.json")
        assert report["kind"] == "report"
        assert report["content"]["status"]
        verdict = next(item for item in body if item["name"] == "verdict-g1-r1.json")
        assert verdict["kind"] == "verdict"
        assert verdict["content"]["status"]

    def test_the_pane_gets_the_fields_it_renders(self, client, repo):
        """status, summary/notes, verification_results and surprises survive the
        round trip through the endpoint."""
        directory = RunPaths(repo, "smoke1").group_dir("g1")
        report = CoderReport(
            status="completed",
            summary="did the thing",
            verification_results=[VerificationResult(item_id="v1", status="pass", notes="ok")],
        )
        (directory / "report-g2-r1.json").write_text(report.model_dump_json(indent=2))
        verdict = ReviewerVerdict(status="approved", notes="looks good")
        (directory / "verdict-g2-r1.json").write_text(verdict.model_dump_json(indent=2))

        body = {
            item["name"]: item["content"]
            for item in client.get(f"{RUN}/groups/g1/artifacts").json()
        }
        assert body["report-g2-r1.json"]["summary"] == "did the thing"
        assert body["report-g2-r1.json"]["verification_results"][0]["item_id"] == "v1"
        assert body["report-g2-r1.json"]["surprises"] == []
        assert body["verdict-g2-r1.json"]["notes"] == "looks good"

    def test_a_group_with_no_directory_is_an_empty_list(self, client):
        response = client.get(f"{RUN}/groups/g99/artifacts")
        assert response.status_code == 200
        assert response.json() == []

    def test_a_malformed_artifact_is_named_rather_than_failing_the_list(self, client, repo):
        directory = RunPaths(repo, "smoke1").group_dir("g1")
        (directory / "report-g3-r1.json").write_text("{not json")
        body = client.get(f"{RUN}/groups/g1/artifacts").json()
        broken = next(item for item in body if item["name"] == "report-g3-r1.json")
        assert broken["content"] is None
        assert broken["error"]
        assert len(body) > 1  # the readable ones still came back
