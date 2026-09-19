"""Operator decisions, derived from answered escalations (plan U8).

An ``Operator Decision`` is a binding, answered ``coder_question`` escalation:
guidance the operator gave one coder that must carry forward into every later
prompt of the same group, not just the round it unblocked. Nothing new is
written for it — ``decision_ledger`` derives the ledger at read time from the
request/response files ``EscalationBroker.raise_escalation`` and
``answer_escalation`` already write, the same two locations
``execution/export.py``'s ``_escalations_by_group`` globs: the run-level
``escalations/`` directory and, on newer runs, the group's own directory.
"""

from __future__ import annotations

from pydantic import BaseModel

from orchestrator.execution.manifest import RunPaths
from orchestrator.model import EscalationKind, EscalationRequest, EscalationResponse, HumanAction


class OperatorDecision(BaseModel):
    """One binding answer, ready to render into a prompt."""

    escalation_id: str
    at: str
    generation: int
    question: str
    answer: str


def decision_ledger(paths: RunPaths, group_id: str) -> list[OperatorDecision]:
    """Binding, answered ``coder_question`` decisions for ``group_id``, oldest
    first.

    Only requests for this group qualify: ``kind == coder_question``,
    ``action == answer``, ``binding == True`` (the default). Any file that
    fails to parse as the expected shape is skipped rather than raised — real
    runs on disk predate this field and some predate the escalation schema
    entirely, and a ledger is a best-effort read, not a hard join.
    """
    directories = [paths.escalations_dir, paths.group_dir(group_id)]
    decisions: list[OperatorDecision] = []
    seen_ids: set[str] = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for request_path in sorted(directory.glob("request-*.json")):
            esc_id = request_path.name[len("request-") : -len(".json")]
            if esc_id in seen_ids:
                continue
            seen_ids.add(esc_id)
            try:
                request = EscalationRequest.model_validate_json(request_path.read_text())
            except ValueError:
                continue
            if request.group_id != group_id or request.kind != EscalationKind.CODER_QUESTION:
                continue
            response_path = directory / f"response-{esc_id}.json"
            if not response_path.is_file():
                continue
            try:
                response = EscalationResponse.model_validate_json(response_path.read_text())
            except ValueError:
                continue
            if response.action != HumanAction.ANSWER or not response.binding:
                continue
            decisions.append(
                OperatorDecision(
                    escalation_id=esc_id,
                    at=response.answered_at.isoformat(),
                    generation=request.generation,
                    question=request.prompt,
                    answer=response.answer,
                )
            )
    decisions.sort(key=lambda decision: decision.at)
    return decisions


def render_decisions_section(ledger: list[OperatorDecision]) -> str:
    """``## Operator decisions (binding)`` verbatim, or ``""`` for an empty
    ledger. Neither the question nor the answer is trimmed or reflowed — a
    multi-line answer keeps its line breaks, indented under its ``Decision:``
    label."""
    if not ledger:
        return ""
    lines = ["## Operator decisions (binding)"]
    for decision in ledger:
        lines.append(f"- [{decision.escalation_id} @ {decision.at}] Question: {decision.question}")
        answer_lines = decision.answer.split("\n")
        lines.append(f"  Decision: {answer_lines[0]}")
        lines.extend(f"    {line}" for line in answer_lines[1:])
    return "\n".join(lines) + "\n"
