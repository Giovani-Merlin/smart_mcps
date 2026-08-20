"""U11 + U12 tests: escalation contracts, the intensity policy matrix, and the
file-based broker (plan Phase D). All offline, token-free.

The broker is exercised with a real ``EscalationBroker`` against ``tmp_path``: a
background thread plays the operator (writes response files), the main thread
raises escalations, mirroring the live supervision loop deterministically.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.config import EscalationConfig, OrchestratorConfig, load_config
from orchestrator.execution.escalation import (
    EscalationBroker,
    EscalationError,
    EscalationPolicy,
    answer_escalation,
    pending_escalations,
)
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.model import (
    CoderReport,
    EscalationContext,
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    HumanAction,
    Surprise,
)


# ------------------------------------------------------------------ models


class TestCoderReportNeedsInput:
    def test_needs_input_requires_a_question(self):
        with pytest.raises(ValidationError, match="question"):
            CoderReport.model_validate({"status": "needs_input", "question": "   "})

    def test_needs_input_with_a_question_round_trips(self):
        report = CoderReport(status="needs_input", question="Which auth scheme?")
        assert CoderReport.model_validate_json(report.model_dump_json()) == report

    def test_other_statuses_do_not_require_a_question(self):
        assert CoderReport(status="blocked").question == ""
        assert CoderReport(status="completed", summary="done").question == ""


class TestEscalationModels:
    def test_request_round_trips_losslessly(self):
        request = EscalationRequest(
            id="esc-1",
            run_id="r1",
            group_id="g1",
            generation=2,
            kind=EscalationKind.REVIEWER_TOO_HARD,
            prompt="reviewer says too hard",
            context=EscalationContext(
                report_path="/r/report.json",
                verdict_path="/r/verdict.json",
                diff_summary="3 files changed",
                surprises=[
                    Surprise(kind="merge_conflict", description="x", affected_groups=["g2"])
                ],
            ),
        )
        assert EscalationRequest.model_validate_json(request.model_dump_json()) == request

    def test_response_round_trips_losslessly(self):
        response = EscalationResponse(id="esc-1", action=HumanAction.ANSWER, answer="use JWT")
        assert EscalationResponse.model_validate_json(response.model_dump_json()) == response

    def test_response_defaults_are_minimal(self):
        response = EscalationResponse(id="esc-2", action=HumanAction.SKIP)
        assert response.answer == "" and response.answered_at is not None


# ------------------------------------------------------------------ policy

ALL_KINDS = list(EscalationKind)
WORKERS = "workers_via_orchestrator"


class TestEscalationPolicy:
    def test_autonomous_escalates_nothing(self):
        policy = EscalationPolicy("autonomous", WORKERS)
        assert not any(policy.should_escalate(kind) for kind in ALL_KINDS)

    def test_on_failure_escalates_caps_exhausted_and_group_resolve(self):
        policy = EscalationPolicy("on_failure", WORKERS)
        escalated = {kind for kind in ALL_KINDS if policy.should_escalate(kind)}
        assert escalated == {EscalationKind.CAPS_EXHAUSTED, EscalationKind.GROUP_RESOLVE}

    def test_on_stuck_covers_stuck_and_failure_but_not_approval_gates(self):
        policy = EscalationPolicy("on_stuck", WORKERS)
        escalated = {kind for kind in ALL_KINDS if policy.should_escalate(kind)}
        assert escalated == {
            EscalationKind.CAPS_EXHAUSTED,
            EscalationKind.GROUP_RESOLVE,
            EscalationKind.CODER_QUESTION,
            EscalationKind.CODER_BLOCKED,
            EscalationKind.REVIEWER_TOO_HARD,
            EscalationKind.REVIEWER_STRUCTURAL,
            EscalationKind.MERGE_CONFLICT,
        }
        for gate in (
            EscalationKind.GROUP_START,
            EscalationKind.RESPAWN,
            EscalationKind.MERGE_APPROVE,
        ):
            assert not policy.should_escalate(gate)

    def test_interactive_escalates_every_kind(self):
        policy = EscalationPolicy("interactive", WORKERS)
        assert all(policy.should_escalate(kind) for kind in ALL_KINDS)

    def test_orchestrator_only_suppresses_the_coder_question_channel(self):
        policy = EscalationPolicy("on_stuck", "orchestrator_only")
        assert not policy.should_escalate(EscalationKind.CODER_QUESTION)
        # every other on_stuck kind still escalates
        assert policy.should_escalate(EscalationKind.CODER_BLOCKED)
        assert policy.should_escalate(EscalationKind.CAPS_EXHAUSTED)

    def test_orchestrator_only_still_suppresses_question_at_interactive(self):
        policy = EscalationPolicy("interactive", "orchestrator_only")
        assert not policy.should_escalate(EscalationKind.CODER_QUESTION)
        assert policy.should_escalate(EscalationKind.GROUP_START)


# ------------------------------------------------------------------ config


class TestEscalationConfig:
    def test_defaults_load_without_a_file(self):
        config = load_config(None)
        assert config.escalation.enabled is False  # F1: HITL is opt-in
        assert config.escalation.intensity == "autonomous"
        assert config.escalation.source == "workers_via_orchestrator"
        assert config.escalation.timeout_s is None
        assert config.escalation.on_timeout == "autonomous"
        assert config.escalation.poll_interval_s == 1.0

    def test_toml_overrides_merge_over_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[escalation]\n"
            "enabled = true\n"
            'intensity = "interactive"\n'
            'source = "orchestrator_only"\n'
            "timeout_s = 30.0\n"
            'on_timeout = "skip"\n'
            "poll_interval_s = 0.25\n"
        )
        config = load_config(config_file)
        assert config.escalation.enabled is True
        assert config.escalation.intensity == "interactive"
        assert config.escalation.source == "orchestrator_only"
        assert config.escalation.timeout_s == 30.0
        assert config.escalation.on_timeout == "skip"
        assert config.escalation.poll_interval_s == 0.25
        # untouched sections keep defaults
        assert config.breaker.max_generations == 3

    def test_invalid_intensity_is_rejected(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[escalation]\nintensity = "sometimes"\n')
        with pytest.raises(ValidationError):
            load_config(config_file)

    def test_absent_section_keeps_full_defaults(self):
        assert OrchestratorConfig().escalation == EscalationConfig()


# ------------------------------------------------------------------ broker


def _request(
    esc_id: str = "e1", kind: EscalationKind = EscalationKind.CODER_QUESTION
) -> EscalationRequest:
    return EscalationRequest(
        id=esc_id, run_id="r1", group_id="g1", generation=1, kind=kind, prompt="please decide"
    )


def _answer_when_present(
    paths: RunPaths, esc_id: str, response: EscalationResponse, *, timeout_s: float = 3.0
) -> threading.Thread:
    """Background 'operator': wait for request-<id>.json, then write the response."""

    def worker() -> None:
        request_path = paths.escalations_dir / f"request-{esc_id}.json"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if request_path.is_file():
                atomic_write_text(
                    paths.escalations_dir / f"response-{esc_id}.json",
                    response.model_dump_json(),
                )
                return
            time.sleep(0.01)

    thread = threading.Thread(target=worker)
    thread.start()
    return thread


class TestEscalationBroker:
    def _broker(self, tmp_path: Path, **overrides) -> EscalationBroker:
        paths = RunPaths(tmp_path, "r1")
        return EscalationBroker(paths, EscalationConfig(poll_interval_s=0.01, **overrides))

    def test_request_then_response_happy_path(self, tmp_path):
        broker = self._broker(tmp_path)
        thread = _answer_when_present(
            broker.paths, "e1", EscalationResponse(id="e1", action=HumanAction.ANSWER, answer="go")
        )
        response = broker.raise_escalation(_request("e1"))
        thread.join()
        assert response is not None
        assert response.action == HumanAction.ANSWER and response.answer == "go"

    def test_request_file_is_written_and_matches_by_correlation_id(self, tmp_path):
        broker = self._broker(tmp_path)
        thread = _answer_when_present(
            broker.paths, "abc", EscalationResponse(id="abc", action=HumanAction.SKIP)
        )
        broker.raise_escalation(_request("abc"))
        thread.join()
        request_path = broker.paths.escalations_dir / "request-abc.json"
        assert request_path.is_file()
        written = EscalationRequest.model_validate_json(request_path.read_text())
        assert written.id == "abc" and written.group_id == "g1"

    def test_timeout_with_autonomous_fallback_returns_none(self, tmp_path):
        broker = self._broker(tmp_path, timeout_s=0.05, on_timeout="autonomous")
        assert broker.raise_escalation(_request("e-timeout")) is None

    def test_timeout_with_skip_synthesizes_a_skip_response(self, tmp_path):
        broker = self._broker(tmp_path, timeout_s=0.05, on_timeout="skip")
        response = broker.raise_escalation(_request("e-skip"))
        assert response is not None and response.action == HumanAction.SKIP

    def test_abort_event_unblocks_a_waiter_promptly(self, tmp_path):
        broker = self._broker(tmp_path)  # no timeout → would block forever

        def abort_soon() -> None:
            time.sleep(0.05)
            broker.trigger_abort()

        thread = threading.Thread(target=abort_soon)
        thread.start()
        started = time.monotonic()
        response = broker.raise_escalation(_request("e-abort"))
        thread.join()
        assert response is not None and response.action == HumanAction.ABORT
        assert time.monotonic() - started < 2.0  # unblocked well before any poll timeout

    def test_pending_escalations_lists_unanswered_requests_only(self, tmp_path):
        broker = self._broker(tmp_path)
        # a stale, unanswered request (mirrors a crash-resume leftover — still readable)
        atomic_write_text(
            broker.paths.escalations_dir / "request-open.json", _request("open").model_dump_json()
        )
        # an answered one
        atomic_write_text(
            broker.paths.escalations_dir / "request-done.json", _request("done").model_dump_json()
        )
        atomic_write_text(
            broker.paths.escalations_dir / "response-done.json",
            EscalationResponse(id="done", action=HumanAction.ANSWER).model_dump_json(),
        )
        pending = pending_escalations(broker.paths)
        assert [req.id for req in pending] == ["open"]

    def test_event_log_records_raise_and_answer(self, tmp_path):
        broker = self._broker(tmp_path)
        thread = _answer_when_present(
            broker.paths, "e-log", EscalationResponse(id="e-log", action=HumanAction.ANSWER)
        )
        broker.raise_escalation(_request("e-log"))
        thread.join()
        log = broker.paths.event_log_path.read_text()
        assert "ESCALATION e-log" in log
        assert "answered" in log

    def test_pending_escalations_empty_when_no_directory(self, tmp_path):
        assert pending_escalations(RunPaths(tmp_path, "nope")) == []

    # ------------------------------------------------------- stdout (plan U7)

    def test_raise_prints_id_kind_and_group_to_stdout(self, tmp_path, capsys):
        broker = self._broker(tmp_path)
        thread = _answer_when_present(
            broker.paths, "e-out", EscalationResponse(id="e-out", action=HumanAction.ANSWER)
        )
        broker.raise_escalation(_request("e-out", kind=EscalationKind.CODER_BLOCKED))
        thread.join()
        out = capsys.readouterr().out
        assert "[escalation]" in out
        assert "e-out" in out and "coder_blocked" in out and "g1" in out

    def test_raise_names_pending_groups_when_a_provider_is_wired(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        broker = EscalationBroker(
            paths,
            EscalationConfig(poll_interval_s=0.01),
            pending_groups_provider=lambda: ["g1", "g2", "g3"],
        )
        thread = _answer_when_present(
            paths, "e-blk", EscalationResponse(id="e-blk", action=HumanAction.ANSWER)
        )
        broker.raise_escalation(_request("e-blk"))  # request itself is for g1
        thread.join()
        out = capsys.readouterr().out
        # the escalating group itself is not named as "blocked" by its own escalation
        assert "g2" in out and "g3" in out and "blocks pending group" in out
        line = next(line for line in out.splitlines() if "e-blk" in line)
        assert "g1" not in line.split("blocks pending group")[-1]

    def test_raise_omits_the_blocks_clause_when_nothing_is_pending(self, tmp_path, capsys):
        paths = RunPaths(tmp_path, "r1")
        broker = EscalationBroker(
            paths, EscalationConfig(poll_interval_s=0.01), pending_groups_provider=lambda: []
        )
        thread = _answer_when_present(
            paths, "e-none", EscalationResponse(id="e-none", action=HumanAction.ANSWER)
        )
        broker.raise_escalation(_request("e-none"))
        thread.join()
        assert "blocks pending group" not in capsys.readouterr().out

    def test_raise_with_no_provider_wired_prints_no_blocks_clause(self, tmp_path, capsys):
        broker = self._broker(tmp_path)  # default construction: no provider
        thread = _answer_when_present(
            broker.paths, "e-noprov", EscalationResponse(id="e-noprov", action=HumanAction.ANSWER)
        )
        broker.raise_escalation(_request("e-noprov"))
        thread.join()
        assert "blocks pending group" not in capsys.readouterr().out


# ------------------------------------------------------- answer_escalation (U1)


class TestAnswerEscalation:
    """The one implementation of the answer contract, shared by the CLI's
    ``answer`` subcommand and the Observatory's write endpoint (plan U1)."""

    def _with_request(self, tmp_path: Path, esc_id: str = "e1") -> RunPaths:
        paths = RunPaths(tmp_path, "r1")
        atomic_write_text(
            paths.escalations_dir / f"request-{esc_id}.json",
            _request(esc_id).model_dump_json(),
        )
        return paths

    def test_writes_the_response_file_and_returns_its_path(self, tmp_path):
        paths = self._with_request(tmp_path)
        written = answer_escalation(paths, "e1", HumanAction.ANSWER, "use JWT")
        assert written == paths.escalations_dir / "response-e1.json"
        response = EscalationResponse.model_validate_json(written.read_text())
        assert response.id == "e1"
        assert response.action == HumanAction.ANSWER
        assert response.answer == "use JWT"

    def test_the_answered_escalation_stops_being_pending(self, tmp_path):
        paths = self._with_request(tmp_path)
        assert [req.id for req in pending_escalations(paths)] == ["e1"]
        answer_escalation(paths, "e1", HumanAction.ANSWER)
        assert pending_escalations(paths) == []

    def test_a_plain_string_action_is_accepted(self, tmp_path):
        paths = self._with_request(tmp_path, "e-str")
        written = answer_escalation(paths, "e-str", "skip")
        response = EscalationResponse.model_validate_json(written.read_text())
        assert response.action == HumanAction.SKIP

    def test_unknown_escalation_id_raises(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        with pytest.raises(EscalationError, match="no escalation nope"):
            answer_escalation(paths, "nope", HumanAction.ANSWER)
        assert not (paths.escalations_dir / "response-nope.json").exists()

    def test_answering_twice_raises_and_leaves_the_first_answer_intact(self, tmp_path):
        paths = self._with_request(tmp_path, "e-twice")
        written = answer_escalation(paths, "e-twice", HumanAction.ANSWER, "first")
        first = written.read_bytes()
        with pytest.raises(EscalationError, match="already answered"):
            answer_escalation(paths, "e-twice", HumanAction.SKIP, "second")
        assert written.read_bytes() == first

    def test_answering_prints_the_action_taken_to_stdout(self, tmp_path, capsys):
        paths = self._with_request(tmp_path, "e-stdout")
        answer_escalation(paths, "e-stdout", HumanAction.SKIP, "not worth it")
        out = capsys.readouterr().out
        assert "e-stdout" in out and "skip" in out
