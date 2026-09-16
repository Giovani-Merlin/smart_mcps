"""U8 tests: the decision ledger derived from answered escalations."""

from __future__ import annotations

from orchestrator.execution.decisions import (
    OperatorDecision,
    decision_ledger,
    render_decisions_section,
)
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.model import EscalationKind, EscalationRequest, EscalationResponse, HumanAction


def _write_pair(
    directory,
    esc_id: str,
    *,
    group_id: str = "g1",
    kind: EscalationKind = EscalationKind.CODER_QUESTION,
    generation: int = 1,
    prompt: str = "which auth scheme?",
    answered: bool = True,
    action: HumanAction = HumanAction.ANSWER,
    answer: str = "use JWT",
    binding: bool = True,
    answered_at: str = "2026-09-16T10:00:00Z",
) -> None:
    request = EscalationRequest(
        id=esc_id,
        run_id="r1",
        group_id=group_id,
        generation=generation,
        kind=kind,
        prompt=prompt,
    )
    atomic_write_text(directory / f"request-{esc_id}.json", request.model_dump_json())
    if not answered:
        return
    response = EscalationResponse(
        id=esc_id,
        action=action,
        answer=answer,
        binding=binding,
        answered_at=answered_at,
    )
    atomic_write_text(directory / f"response-{esc_id}.json", response.model_dump_json())


class TestDecisionLedger:
    def test_only_binding_answered_coder_questions_for_the_group_are_kept(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        group_dir = paths.group_dir("g1")

        # Non-binding — excluded.
        _write_pair(
            paths.escalations_dir,
            "q-nonbinding",
            binding=False,
            answer="skip this one",
            answered_at="2026-09-16T09:00:00Z",
        )
        # Binding, under groups/g1/.
        _write_pair(
            group_dir,
            "q-group",
            answer="use the `env.FOO` flag, cost is $5",
            answered_at="2026-09-16T10:00:00Z",
        )
        # Binding, under escalations/.
        _write_pair(
            paths.escalations_dir,
            "q-run",
            answer="a backtick ` and a dollar $ in this one too",
            answered_at="2026-09-16T11:00:00Z",
        )
        # coder_blocked, answered — wrong kind, excluded.
        _write_pair(
            paths.escalations_dir,
            "blocked-1",
            kind=EscalationKind.CODER_BLOCKED,
            answer="fixed the env",
            answered_at="2026-09-16T12:00:00Z",
        )
        # coder_question answered with retry — wrong action, excluded.
        _write_pair(
            paths.escalations_dir,
            "q-retry",
            action=HumanAction.RETRY,
            answer="relaunch",
            answered_at="2026-09-16T13:00:00Z",
        )
        # coder_question, unanswered — excluded.
        _write_pair(paths.escalations_dir, "q-unanswered", answered=False)

        ledger = decision_ledger(paths, "g1")

        assert [d.escalation_id for d in ledger] == ["q-group", "q-run"]
        assert all(isinstance(d, OperatorDecision) for d in ledger)
        assert ledger[0].answer == "use the `env.FOO` flag, cost is $5"
        assert ledger[1].answer == "a backtick ` and a dollar $ in this one too"

        section = render_decisions_section(ledger)
        assert "## Operator decisions (binding)" in section
        assert "use the `env.FOO` flag, cost is $5" in section
        assert "a backtick ` and a dollar $ in this one too" in section
        assert "q-nonbinding" not in section
        assert "blocked-1" not in section
        assert "q-retry" not in section
        assert "q-unanswered" not in section

    def test_a_different_group_ids_requests_are_ignored(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        _write_pair(paths.escalations_dir, "q-other", group_id="g2")
        assert decision_ledger(paths, "g1") == []

    def test_an_empty_run_dir_returns_an_empty_ledger(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        assert decision_ledger(paths, "g1") == []

    def test_a_response_written_before_binding_existed_defaults_binding_true(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        request = EscalationRequest(
            id="q-old",
            run_id="r1",
            group_id="g1",
            generation=1,
            kind=EscalationKind.CODER_QUESTION,
            prompt="old style?",
        )
        atomic_write_text(paths.escalations_dir / "request-q-old.json", request.model_dump_json())
        # Hand-write a response with no "binding" key, mimicking a pre-field file.
        atomic_write_text(
            paths.escalations_dir / "response-q-old.json",
            '{"id": "q-old", "action": "answer", "answer": "yes", '
            '"answered_at": "2026-09-16T10:00:00Z"}',
        )
        ledger = decision_ledger(paths, "g1")
        assert [d.escalation_id for d in ledger] == ["q-old"]

    def test_malformed_files_are_skipped_not_raised(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        paths.escalations_dir.mkdir(parents=True)
        (paths.escalations_dir / "request-bad.json").write_text("not json")
        assert decision_ledger(paths, "g1") == []

        _write_pair(paths.escalations_dir, "q-good")
        (paths.escalations_dir / "response-q-good.json").write_text("not json either")
        assert decision_ledger(paths, "g1") == []


class TestRenderDecisionsSection:
    def test_empty_ledger_renders_empty_string(self):
        assert render_decisions_section([]) == ""

    def test_multiline_answer_is_indented_and_not_reflowed(self):
        ledger = [
            OperatorDecision(
                escalation_id="e1",
                at="2026-09-16T10:00:00Z",
                generation=1,
                question="pick one?",
                answer="line one\nline two\n  line three indented",
            )
        ]
        section = render_decisions_section(ledger)
        lines = section.splitlines()
        assert lines[0] == "## Operator decisions (binding)"
        assert lines[1] == "- [e1 @ 2026-09-16T10:00:00Z] Question: pick one?"
        assert lines[2] == "  Decision: line one"
        assert lines[3] == "    line two"
        assert lines[4] == "      line three indented"

    def test_single_line_answer_round_trips_verbatim(self):
        ledger = [
            OperatorDecision(
                escalation_id="e2",
                at="2026-09-16T11:00:00Z",
                generation=2,
                question="use `X` for $5?",
                answer="yes, use `X`, it costs $5",
            )
        ]
        section = render_decisions_section(ledger)
        assert "Question: use `X` for $5?" in section
        assert "Decision: yes, use `X`, it costs $5" in section
