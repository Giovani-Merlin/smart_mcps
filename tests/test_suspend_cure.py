"""U5 tests: a `SuspendCured` raised at a worker call site is warm-resumed in
place — coder and reviewer alike — counted and capped per generation (plan
U5). Sessions are scripted in-process via `StubRunner` (see `test_review_loop`),
zero subprocesses, zero tokens.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.review import make_executor
from orchestrator.execution.scheduler import GroupContext, GroupState, Scheduler
from orchestrator.execution.sessions import SuspendCured

from test_review_loop import Harness, StubRunner, coder_report, make_group, verdict


class CuringStubRunner(StubRunner):
    """A `StubRunner` whose *Nth* `resume()` call (1-indexed, across every
    session) raises `SuspendCured` instead of returning — modelling a
    liveness probe having just killed that child — then behaves exactly like
    `StubRunner` on every other call, including the very next one (the
    review loop's own warm-resume recovery).
    """

    def __init__(self, fork_scripts, *, cure_on_resume_call: int, on_cure=None):
        super().__init__(fork_scripts)
        self._cure_on_resume_call = cure_on_resume_call
        self._on_cure = on_cure
        self._resume_calls = 0
        self.cured_session_id: str | None = None

    def resume(self, *, session_id, prompt, cwd, json_schema=None, on_turn=None):
        self._resume_calls += 1
        if self._resume_calls == self._cure_on_resume_call:
            self.cured_session_id = session_id
            if self._on_cure is not None:
                self._on_cure()
            raise SuspendCured(
                f"claude exited 137 (--resume {session_id}): killed as a suspend cure",
                session_id,
            )
        return super().resume(
            session_id=session_id, prompt=prompt, cwd=cwd, json_schema=json_schema, on_turn=on_turn
        )


def _scheduled_context(harness: Harness, group, scheduler: Scheduler) -> GroupContext:
    """A real `GroupContext` wired to a real `Scheduler` (for `record_cure`'s
    persistence to `state.json`) but driving the group directly through
    `make_executor`, bypassing the scheduler's own admission loop — the same
    thing `Scheduler._run_group` does, minus the DAG bookkeeping this test
    does not exercise."""
    return GroupContext(
        group=group,
        generation=1,
        set_state=lambda state: scheduler.set_state(group.id, state),
        set_generation=lambda generation: scheduler.set_generation(group.id, generation),
        record_cure=lambda: scheduler.record_cure(group.id),
    )


async def _never_called_executor(ctx) -> GroupState:  # pragma: no cover
    raise AssertionError("scheduler.run() was never invoked by this test")


@pytest.mark.asyncio
async def test_warm_resume_after_a_suspend_cure_on_the_coders_first_resume_call(tmp_path):
    """[g8-3] The revision resume (the group's first `.resume()` call) is
    cured; the review loop warm-resumes the same coder session in place, and
    the group completes in generation 1 with the cure counted."""
    runner = CuringStubRunner(
        {
            "r1-g1-coder-g1": [coder_report(), coder_report()],
            "r1-g1-reviewer-g1": [
                verdict("changes_required", ["fix x"]),
                verdict("approved"),
            ],
        },
        cure_on_resume_call=1,
    )
    harness = Harness(tmp_path, runner)
    group = make_group()
    paths = RunPaths(tmp_path, "r1")
    scheduler = Scheduler(groups=[group], paths=paths, executor=_never_called_executor)
    runner._on_cure = lambda: scheduler.record_cure(group.id)
    ctx = _scheduled_context(harness, group, scheduler)

    final_state = await make_executor(harness.deps)(ctx)

    assert final_state == GroupState.COMPLETED
    assert runner.cured_session_id is not None
    # Exactly one coder session in the manifest — the cure never forked a
    # fresh generation.
    sessions = harness.manifest.groups["g1"].sessions
    coder_sessions = [s for s in sessions if s.name == "r1-g1-coder-g1"]
    assert len(coder_sessions) == 1

    state = json.loads(paths.state_path.read_text())
    entry = state["groups"]["g1"]
    assert entry["cures"] == {"1": 1}
    assert entry["reentry_count"] == 0

    lines = paths.event_log_path.read_text().splitlines()
    warm_lines = [line for line in lines if "warm-resuming session" in line]
    assert len(warm_lines) == 1
    assert f"warm-resuming session {runner.cured_session_id} after suspend cure" in warm_lines[0]


@pytest.mark.asyncio
async def test_warm_resume_after_a_suspend_cure_on_the_reviewers_first_resume_call(tmp_path):
    """[g8-4] The coder's own revision resume succeeds normally; the
    reviewer's re-review (its first `.resume()` call) is cured. The review
    loop re-issues it with the re-review prompt, the verdict still lands, and
    the generation is unchanged."""
    runner = CuringStubRunner(
        {
            "r1-g1-coder-g1": [coder_report(), coder_report()],
            "r1-g1-reviewer-g1": [
                verdict("changes_required", ["fix x"]),
                verdict("approved"),
            ],
        },
        cure_on_resume_call=2,  # 1st resume = coder revision (succeeds); 2nd = re-review (cured)
    )
    harness = Harness(tmp_path, runner)
    group = make_group()
    paths = RunPaths(tmp_path, "r1")
    scheduler = Scheduler(groups=[group], paths=paths, executor=_never_called_executor)
    runner._on_cure = lambda: scheduler.record_cure(group.id)
    ctx = _scheduled_context(harness, group, scheduler)

    final_state = await make_executor(harness.deps)(ctx)

    assert final_state == GroupState.COMPLETED
    assert runner.cured_session_id == runner.session_ids["r1-g1-reviewer-g1"]

    sessions = harness.manifest.groups["g1"].sessions
    reviewer_sessions = [s for s in sessions if s.name == "r1-g1-reviewer-g1"]
    assert len(reviewer_sessions) == 1
    assert harness.generations == []  # no breaker respawn — same generation throughout

    state = json.loads(paths.state_path.read_text())
    entry = state["groups"]["g1"]
    assert entry["cures"] == {"1": 1}
    assert entry["reentry_count"] == 0

    lines = paths.event_log_path.read_text().splitlines()
    warm_lines = [line for line in lines if "warm-resuming session" in line]
    assert len(warm_lines) == 1
