"""U6 tests: pending-escalation listing and the one write endpoint.

The write path is deliberately thin — it delegates to ``answer_escalation``, the
same function the CLI's ``answer`` subcommand calls — so these tests care about
two things: that the delegation really happens (the CLI and the UI must produce
the same file), and that the contract's failures map onto the right status
codes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.cli import main
from orchestrator.execution.escalation import pending_escalations
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.model import (
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    HumanAction,
)
from orchestrator.observatory.app import create_app
from test_observatory_api import install_run, write_registry

RUN = "/api/projects/proj/runs/smoke1"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "smoke1")
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    registry = write_registry(tmp_path, [("proj", repo)])
    return TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))


def raise_escalation(
    repo: Path,
    esc_id: str,
    *,
    kind: EscalationKind = EscalationKind.CODER_QUESTION,
    group_id: str = "g1",
    generation: int = 1,
    created_at: datetime | None = None,
    context: EscalationContext | None = None,
) -> EscalationRequest:
    """Write a request file exactly as ``EscalationBroker.raise_escalation`` does."""
    paths = RunPaths(repo, "smoke1")
    request = EscalationRequest(
        id=esc_id,
        run_id="smoke1",
        group_id=group_id,
        generation=generation,
        kind=kind,
        prompt=f"decide {esc_id}",
        context=context or EscalationContext(),
        created_at=created_at or datetime.now(UTC),
    )
    atomic_write_text(
        paths.escalations_dir / f"request-{esc_id}.json", request.model_dump_json(indent=2)
    )
    return request


class TestListing:
    def test_no_escalations_dir_is_an_empty_list(self, client, repo):
        assert not (repo / ".orchestrator" / "runs" / "smoke1" / "escalations").exists()
        response = client.get(f"{RUN}/escalations")
        assert response.status_code == 200
        assert response.json() == []

    def test_lists_every_field_the_panel_renders(self, client, repo):
        raise_escalation(
            repo,
            "e1",
            kind=EscalationKind.REVIEWER_TOO_HARD,
            group_id="g2",
            generation=3,
            context=EscalationContext(
                report_path="/tmp/report.json", diff_summary="3 files changed"
            ),
        )
        entry = client.get(f"{RUN}/escalations").json()[0]
        assert entry["id"] == "e1"
        assert entry["kind"] == "reviewer_too_hard"
        assert entry["group_id"] == "g2"
        assert entry["generation"] == 3
        assert entry["prompt"] == "decide e1"
        assert entry["created_at"]
        assert entry["context"]["report_path"] == "/tmp/report.json"
        assert entry["context"]["diff_summary"] == "3 files changed"

    def test_sorted_by_created_at(self, client, repo):
        now = datetime.now(UTC)
        raise_escalation(repo, "late", created_at=now)
        raise_escalation(repo, "early", created_at=now - timedelta(minutes=5))
        raise_escalation(repo, "middle", created_at=now - timedelta(minutes=1))
        listed = [entry["id"] for entry in client.get(f"{RUN}/escalations").json()]
        assert listed == ["early", "middle", "late"]

    def test_answered_escalations_are_excluded(self, client, repo):
        raise_escalation(repo, "open")
        raise_escalation(repo, "done")
        paths = RunPaths(repo, "smoke1")
        atomic_write_text(
            paths.escalations_dir / "response-done.json",
            EscalationResponse(id="done", action=HumanAction.ANSWER).model_dump_json(),
        )
        listed = [entry["id"] for entry in client.get(f"{RUN}/escalations").json()]
        assert listed == ["open"]

    def test_unknown_run_is_404(self, client):
        assert client.get("/api/projects/proj/runs/ghost/escalations").status_code == 404


class TestAnswer:
    def test_answering_writes_the_response_and_clears_the_pending_entry(self, client, repo):
        raise_escalation(repo, "e1")
        response = client.post(
            f"{RUN}/escalations/e1/answer", json={"action": "answer", "text": "use JWT"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "e1" and body["action"] == "answer"

        written = Path(body["response_path"])
        assert written.is_file()
        assert written.name == "response-e1.json"
        parsed = EscalationResponse.model_validate_json(written.read_text())
        assert parsed.action == HumanAction.ANSWER and parsed.answer == "use JWT"

        # the run's own view of what is outstanding agrees
        assert pending_escalations(RunPaths(repo, "smoke1")) == []
        assert client.get(f"{RUN}/escalations").json() == []

    @pytest.mark.parametrize("action", [action.value for action in HumanAction])
    def test_every_human_action_is_accepted(self, client, repo, action):
        raise_escalation(repo, f"e-{action}")
        response = client.post(f"{RUN}/escalations/e-{action}/answer", json={"action": action})
        assert response.status_code == 200
        assert response.json()["action"] == action

    def test_an_unknown_action_is_422_and_writes_nothing(self, client, repo):
        raise_escalation(repo, "e1")
        response = client.post(
            f"{RUN}/escalations/e1/answer", json={"action": "retry", "text": "nope"}
        )
        assert response.status_code == 422
        paths = RunPaths(repo, "smoke1")
        assert not (paths.escalations_dir / "response-e1.json").exists()
        assert [req.id for req in pending_escalations(paths)] == ["e1"]

    def test_unknown_escalation_id_is_404(self, client, repo):
        response = client.post(f"{RUN}/escalations/ghost/answer", json={"action": "answer"})
        assert response.status_code == 404
        assert "ghost" in response.json()["detail"]

    def test_already_answered_is_409_and_leaves_the_first_response_intact(self, client, repo):
        raise_escalation(repo, "e1")
        first = client.post(
            f"{RUN}/escalations/e1/answer", json={"action": "answer", "text": "first"}
        )
        assert first.status_code == 200
        written = Path(first.json()["response_path"])
        before = written.read_bytes()

        second = client.post(
            f"{RUN}/escalations/e1/answer", json={"action": "skip", "text": "second"}
        )
        assert second.status_code == 409
        assert "already answered" in second.json()["detail"]
        assert written.read_bytes() == before


class TestSharedWithTheCli:
    def test_the_route_and_the_cli_produce_the_same_response_file(self, client, repo, tmp_path):
        """Both call answer_escalation, so the files must agree field for field
        except the timestamp — one implementation of the HITL contract."""
        raise_escalation(repo, "via-http")
        raise_escalation(repo, "via-cli")

        client.post(
            f"{RUN}/escalations/via-http/answer", json={"action": "answer", "text": "same text"}
        )
        exit_code = main(
            [
                "answer",
                "smoke1",
                "via-cli",
                "--action",
                "answer",
                "--text",
                "same text",
                "--repo",
                str(repo),
            ]
        )
        assert exit_code == 0

        directory = RunPaths(repo, "smoke1").escalations_dir
        http_body = json.loads((directory / "response-via-http.json").read_text())
        cli_body = json.loads((directory / "response-via-cli.json").read_text())
        assert set(http_body) == set(cli_body)
        for field in set(http_body) - {"id", "answered_at"}:
            assert http_body[field] == cli_body[field], field
        assert http_body["id"] == "via-http" and cli_body["id"] == "via-cli"
