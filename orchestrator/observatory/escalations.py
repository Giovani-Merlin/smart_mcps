"""The HITL channel over HTTP: pending escalations and answering them (plan U6).

This was for a long time the Observatory's *only* write path, and the module
docstring said so. That is no longer true and the sentence has been rewritten
rather than left contradicting the code: ``launch.py`` now starts groupings,
runs and resumes. Answering an escalation is still the only write that touches a
*live* run's state, and it remains the more delicate of the two — a launch that
goes wrong has not started, while an answer here unblocks a group that is
already waiting on it.

This module does not implement the answer:
listing delegates to ``pending_escalations`` and answering to
``answer_escalation``, both in ``orchestrator.execution.escalation``. The
request/response pairing rule therefore has exactly one implementation, shared
with the CLI's ``answer`` subcommand — a UI answer and a CLI answer produce the
same file.

What is genuinely this layer's job is mapping that contract's failures onto
status codes: unknown escalation → 404, already answered → 409, and an action
outside ``HumanAction`` → 422 from FastAPI's own validation, before any file is
touched.

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from orchestrator.execution.escalation import (
    EscalationError,
    answer_escalation,
    pending_escalations,
)
from orchestrator.model import EscalationRequest, EscalationResponse, HumanAction
from orchestrator.observatory.runs import RUN_PREFIX, resolve_run

router = APIRouter(tags=["escalations"], prefix=RUN_PREFIX)


class AnswerBody(BaseModel):
    """``action`` is typed as the enum, so an unknown verb is rejected with 422
    by validation — the route body never runs and no response file is written."""

    action: HumanAction
    text: str = ""


class AnswerResult(BaseModel):
    id: str
    action: HumanAction
    answered_at: datetime
    response_path: str


@router.get("/escalations", response_model=list[EscalationRequest])
def get_escalations(request: Request, project: str, run_id: str) -> list[EscalationRequest]:
    """Unanswered requests, oldest first. A run whose escalations dir was never
    created (none ever fired) is an empty list, not a 404."""
    return pending_escalations(resolve_run(request, project, run_id))


@router.post("/escalations/{esc_id}/answer", response_model=AnswerResult)
def post_answer(
    request: Request, project: str, run_id: str, esc_id: str, body: AnswerBody
) -> AnswerResult:
    """Write the response file the blocked group is polling for.

    Writing that file *is* the whole answer protocol — no signal, no socket —
    so a successful POST is what unblocks the run.
    """
    paths = resolve_run(request, project, run_id)
    try:
        response_path = answer_escalation(paths, esc_id, body.action, body.text)
    except EscalationError as exc:
        # Classify rather than re-check the contract: if the request exists the
        # only way to fail is that it was already answered.
        known = (paths.escalations_dir / f"request-{esc_id}.json").is_file()
        raise HTTPException(status_code=409 if known else 404, detail=str(exc)) from exc

    written = EscalationResponse.model_validate_json(response_path.read_text())
    return AnswerResult(
        id=written.id,
        action=written.action,
        answered_at=written.answered_at,
        response_path=str(response_path),
    )
