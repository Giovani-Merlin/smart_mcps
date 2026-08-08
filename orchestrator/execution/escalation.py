"""Human-in-the-loop escalation: intensity policy + file-based request/response
broker (plan Phase D).

The orchestrator process stays alive while a group waits on the operator — the
group's coroutine hands the blocking poll to ``asyncio.to_thread`` so sibling
groups keep running. That sidesteps the LangGraph/Temporal replay hazards a
crash-resume design would face (Perplexity research 2026-07-16): a pause is just
an ``await``, not a persisted checkpoint. The transport is deliberately boring —
correlation-ID files, atomic temp-then-rename writes, and simple polling, which is
fine for human-latency waits.

Two halves live here:

- ``EscalationPolicy`` — pure tier matrix: does *this* kind escalate under *this*
  intensity/source? No I/O; the review loop asks it before touching the broker.
- ``EscalationBroker`` — writes the request, blocks polling for the response, and
  unblocks promptly on a run-wide abort. One broker per run, shared by every
  group, so ``trigger_abort`` releases all waiters at once.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from orchestrator.config import EscalationConfig
from orchestrator.execution.manifest import RunPaths, atomic_write_text, log_event
from orchestrator.model import (
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    HumanAction,
)

# The tier matrix, smallest to largest. Each tier is the set of kinds it escalates.
_ON_FAILURE: frozenset[EscalationKind] = frozenset(
    {EscalationKind.CAPS_EXHAUSTED, EscalationKind.GROUP_RESOLVE}
)
_ON_STUCK: frozenset[EscalationKind] = _ON_FAILURE | {
    EscalationKind.CODER_QUESTION,
    EscalationKind.CODER_BLOCKED,
    EscalationKind.REVIEWER_TOO_HARD,
    EscalationKind.REVIEWER_STRUCTURAL,
    EscalationKind.MERGE_CONFLICT,
}
_INTERACTIVE: frozenset[EscalationKind] = _ON_STUCK | {
    EscalationKind.GROUP_START,
    EscalationKind.RESPAWN,
    EscalationKind.MERGE_APPROVE,
}

_TIERS: dict[str, frozenset[EscalationKind]] = {
    "autonomous": frozenset(),
    "on_failure": _ON_FAILURE,
    "on_stuck": _ON_STUCK,
    "interactive": _INTERACTIVE,
}


class EscalationPolicy:
    """Pure decision: should ``kind`` pause for the operator under this config?"""

    def __init__(self, intensity: str, source: str):
        self.intensity = intensity
        self.source = source

    def should_escalate(self, kind: EscalationKind) -> bool:
        # orchestrator_only owns the whole human channel: a coder's question is
        # never surfaced as itself — the review loop downgrades it to the
        # coder_blocked path instead (plan Phase D: workers never talk direct).
        if self.source == "orchestrator_only" and kind == EscalationKind.CODER_QUESTION:
            return False
        return kind in _TIERS.get(self.intensity, frozenset())


class EscalationBroker:
    """The single, curated human channel for one run (plan Phase D).

    ``raise_escalation`` writes ``request-<id>.json``, logs a loud event line, then
    blocks polling for ``response-<id>.json``. It is called from a worker thread
    (``asyncio.to_thread``) so it may block freely. Returns ``None`` when the
    caller should fall through to its autonomous action — either escalation is off
    for this kind or a timeout with ``on_timeout = autonomous`` fired.
    """

    def __init__(self, paths: RunPaths, config: EscalationConfig):
        self.paths = paths
        self.config = config
        self.abort_event = threading.Event()

    def trigger_abort(self) -> None:
        """Release every waiter at once — a run-wide abort is in flight."""
        self.abort_event.set()

    def raise_escalation(self, request: EscalationRequest) -> EscalationResponse | None:
        request_path = self.paths.escalations_dir / f"request-{request.id}.json"
        response_path = self.paths.escalations_dir / f"response-{request.id}.json"
        atomic_write_text(request_path, request.model_dump_json(indent=2) + "\n")
        log_event(
            self.paths,
            f"ESCALATION {request.id} [{request.kind.value}] {request.group_id}: {request.prompt}",
        )

        deadline = (
            None if self.config.timeout_s is None else time.monotonic() + self.config.timeout_s
        )
        poll = max(self.config.poll_interval_s, 0.0)
        while True:
            if self.abort_event.is_set():
                # A sibling's abort is unwinding the run; unblock as an abort so
                # this group also raises RunAbort if the value is ever consumed.
                return EscalationResponse(id=request.id, action=HumanAction.ABORT)
            if response_path.is_file():
                response = EscalationResponse.model_validate_json(response_path.read_text())
                log_event(
                    self.paths,
                    f"ESCALATION {request.id} answered: {response.action.value}",
                )
                return response
            if deadline is not None and time.monotonic() >= deadline:
                return self._on_timeout(request)
            # abort_event.wait returns the instant trigger_abort() fires, so an
            # abort never waits out a full poll interval.
            self.abort_event.wait(poll)

    def _on_timeout(self, request: EscalationRequest) -> EscalationResponse | None:
        action = self.config.on_timeout
        log_event(
            self.paths,
            f"ESCALATION {request.id} timed out → {action}",
        )
        if action == "autonomous":
            return None
        return EscalationResponse(id=request.id, action=HumanAction(action))


class EscalationError(RuntimeError):
    """The escalation cannot be answered as asked — unknown id, or already answered."""


def answer_escalation(
    paths: RunPaths,
    esc_id: str,
    action: HumanAction | str,
    text: str = "",
) -> Path:
    """Write ``response-<esc_id>.json``; the blocked coroutine picks it up by id.

    Lives beside ``pending_escalations`` because the two share one rule — a
    request is open exactly until its response file exists — and both the CLI's
    ``answer`` subcommand and the Observatory's write endpoint call it, so that
    rule has a single implementation.
    """
    directory = paths.escalations_dir
    request_path = directory / f"request-{esc_id}.json"
    response_path = directory / f"response-{esc_id}.json"
    if not request_path.is_file():
        raise EscalationError(f"no escalation {esc_id} for run {paths.run_id}")
    if response_path.is_file():
        # Answering twice would race the waiting group against two different
        # decisions; the first answer stands.
        raise EscalationError(f"escalation {esc_id} was already answered")
    response = EscalationResponse(id=esc_id, action=HumanAction(action), answer=text)
    atomic_write_text(response_path, response.model_dump_json(indent=2) + "\n")
    return response_path


def pending_escalations(paths: RunPaths) -> list[EscalationRequest]:
    """Requests with no matching response yet — what ``status`` lists and the main
    session's supervision loop watches. Sorted by creation time."""
    directory = paths.escalations_dir
    if not directory.is_dir():
        return []
    pending: list[EscalationRequest] = []
    for request_path in directory.glob("request-*.json"):
        esc_id = request_path.name[len("request-") : -len(".json")]
        if (directory / f"response-{esc_id}.json").is_file():
            continue
        pending.append(EscalationRequest.model_validate_json(request_path.read_text()))
    return sorted(pending, key=lambda req: req.created_at)
