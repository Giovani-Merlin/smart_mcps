"""U7 tests: intensity routing, breaker, generations, surprises (plan Phase B).

Sessions are scripted in-process: StubRunner plays canned final messages per
session, so every scenario (approve, reject-then-approve, reject-forever,
surprise, too-hard) runs with zero subprocesses and zero tokens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.escalation import EscalationPolicy
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.review import (
    GroupFailure,
    MergeConflict,
    ReviewDeps,
    SurpriseBoard,
    make_executor,
)
from orchestrator.execution.scheduler import GroupContext, GroupState, RunAbort
from orchestrator.execution.sessions import RoundResult, RoundUsage, SessionUsage
from orchestrator.model import (
    EscalationKind,
    EscalationRequest,
    EscalationResponse,
    Group,
    HumanAction,
    ReviewIntensity,
    RunManifest,
    Surprise,
    VerificationItem,
)


def coder_report(
    status: str = "completed", surprises: list[dict] | None = None, question: str = ""
) -> str:
    body: dict = {
        "status": status,
        "summary": "round done",
        "verification_results": [{"item_id": "v1", "status": "pass", "notes": ""}],
        "surprises": surprises or [],
    }
    if question:
        body["question"] = question
    return f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


def verdict(
    status: str = "approved", changes: list[str] | None = None, surprises: list[dict] | None = None
) -> str:
    body = {
        "status": status,
        "required_changes": changes or [],
        "surprises": surprises or [],
        "notes": "",
    }
    return f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


class StubRunner:
    """Scripted sessions: fork scripts are keyed by display name; the queue then
    serves that session's resumes in order."""

    def __init__(self, fork_scripts: dict[str, list[str]]):
        self.fork_scripts = {name: list(queue) for name, queue in fork_scripts.items()}
        self.session_queues: dict[str, list[str]] = {}
        self.context_tokens: dict[str, int] = {}
        self.forks: list[str] = []
        self.prompts: dict[str, list[str]] = {}
        self.on_fork = None  # optional hook(name) — lets tests interleave events
        self._counter = 0

    def start_fork(self, *, base_id, prompt, name, cwd, json_schema=None) -> RoundResult:
        if self.on_fork is not None:
            self.on_fork(name)
        self._counter += 1
        session_id = f"sess-{self._counter}"
        self.forks.append(name)
        self.session_queues[session_id] = self.fork_scripts[name]
        self.prompts[session_id] = [prompt]
        return self._round(session_id)

    def resume(self, *, session_id, prompt, cwd, json_schema=None) -> RoundResult:
        self.prompts[session_id].append(prompt)
        return self._round(session_id)

    def usage_of(self, session_id: str) -> SessionUsage:
        return SessionUsage(last_context_tokens=self.context_tokens.get(session_id, 1_000))

    def transcript_path(self, session_id: str) -> Path | None:
        return None

    def _round(self, session_id: str) -> RoundResult:
        text = self.session_queues[session_id].pop(0)
        return RoundResult(session_id=session_id, text=text, usage=RoundUsage(), envelope={})


def make_group(gid: str = "g1", intensity=ReviewIntensity.PAIRED, **overrides) -> Group:
    defaults = dict(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid} v1",
        difficulty=0.5,
        intensity=intensity,
        verification=[VerificationItem(id="v1", description="tests pass")],
    )
    defaults.update(overrides)
    return Group(**defaults)


class StubBroker:
    """Canned operator: returns a scripted response keyed by escalation kind, or
    None (→ autonomous fallback). Records every request it was asked to raise."""

    def __init__(self, responses: dict[EscalationKind, EscalationResponse | None] | None = None):
        self.responses = responses or {}
        self.raised: list[EscalationRequest] = []
        self.aborted = False

    def raise_escalation(self, request: EscalationRequest) -> EscalationResponse | None:
        self.raised.append(request)
        return self.responses.get(request.kind)

    def trigger_abort(self) -> None:
        self.aborted = True


def answer(text: str = "") -> EscalationResponse:
    return EscalationResponse(id="x", action=HumanAction.ANSWER, answer=text)


def skip() -> EscalationResponse:
    return EscalationResponse(id="x", action=HumanAction.SKIP)


def abort() -> EscalationResponse:
    return EscalationResponse(id="x", action=HumanAction.ABORT)


class Harness:
    """Deps + context wiring shared by every scenario."""

    def __init__(
        self,
        tmp_path: Path,
        runner: StubRunner,
        *,
        breaker=None,
        execution=None,
        broker: StubBroker | None = None,
        policy: EscalationPolicy | None = None,
    ):
        self.runner = runner
        self.store = ManifestStore(RunPaths(tmp_path, "r1"))
        self.manifest = RunManifest(run_id="r1", plan_path="p.md", base_session_id="base-0")
        self.board = SurpriseBoard()
        self.merged: list[str] = []
        self.rewritten: list[list[Surprise]] = []
        self.merge_failures: list[MergeConflict] = list()
        self.states: list[GroupState] = []
        self.generations: list[int] = []
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir(exist_ok=True)

        def merge_group(group: Group, workspace: Path) -> None:
            if self.merge_failures:
                raise self.merge_failures.pop(0)
            self.merged.append(group.id)

        def rewrite_spec(group: Group, surprises: list[Surprise]) -> Group:
            self.rewritten.append(surprises)
            return group.model_copy(update={"spec": group.spec.replace("v1", "v2")})

        self.deps = ReviewDeps(
            run_id="r1",
            runner=runner,
            store=self.store,
            manifest=self.manifest,
            base_session_id="base-0",
            breaker=breaker or BreakerConfig(),
            execution=execution or ExecutionConfig(),
            board=self.board,
            workspace_for=lambda group: self.workspace,
            merge_group=merge_group,
            rewrite_spec=rewrite_spec,
            base_ref_for=lambda group: "main",
            broker=broker,
            policy=policy,
        )

    def context(self, group: Group) -> GroupContext:
        return GroupContext(
            group=group,
            generation=1,
            set_state=self.states.append,
            set_generation=self.generations.append,
        )

    async def run(self, group: Group) -> GroupState:
        return await make_executor(self.deps)(self.context(group))


@pytest.mark.asyncio
async def test_self_verify_group_never_creates_a_reviewer_session(tmp_path):
    # AE7: below d_review — coder self-verifies, straight to merge.
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group(intensity=ReviewIntensity.SELF_VERIFY))
    assert state == GroupState.COMPLETED
    assert runner.forks == ["r1-g1-coder-g1"]
    assert harness.merged == ["g1"]
    roles = [s.role.value for s in harness.manifest.groups["g1"].sessions]
    assert roles == ["coder"]


@pytest.mark.asyncio
async def test_paired_approval_merges_and_persists_report_and_verdict(tmp_path):
    runner = StubRunner(
        {"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict("approved")]}
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    group_dir = harness.store.paths.group_dir("g1")
    assert (group_dir / "report-g1-r1.json").is_file()
    assert (group_dir / "verdict-g1-r1.json").is_file()
    # reviewer got a pointer to the report, not the report body
    reviewer_first_prompt = runner.prompts["sess-2"][0]
    assert str(group_dir / "report-g1-r1.json") in reviewer_first_prompt
    assert "round done" not in reviewer_first_prompt


@pytest.mark.asyncio
async def test_changes_required_under_thresholds_resumes_same_session(tmp_path):
    # AE3: warm continuation — no new manifest entry, same coder session.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report(), coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["fix x"]), verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    sessions = harness.manifest.groups["g1"].sessions
    assert [s.name for s in sessions] == ["r1-g1-coder-g1", "r1-g1-reviewer-g1"]
    assert all(s.retirement_reason is None for s in sessions)
    revision_prompt = runner.prompts["sess-1"][1]
    assert "fix x" in revision_prompt and "verdict-g1-r1.json" in revision_prompt


@pytest.mark.asyncio
async def test_round_threshold_trips_breaker_into_generation_two(tmp_path):
    # AE4: gen-1 retired with a reason; gen-2 forked fresh from base with a handoff.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["fix y"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(
        tmp_path, runner, breaker=BreakerConfig(max_rounds_per_generation=1, max_generations=3)
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    sessions = harness.manifest.groups["g1"].sessions
    coder_g1 = next(s for s in sessions if s.name == "r1-g1-coder-g1")
    assert (
        coder_g1.retirement_reason is not None and "round threshold" in coder_g1.retirement_reason
    )
    assert any(s.name == "r1-g1-coder-g2" and s.generation == 2 for s in sessions)
    handoff = runner.prompts["sess-3"][0]  # gen-2 coder's first prompt
    assert "generation 2" in handoff and "fix y" in handoff
    assert harness.generations == [2]


@pytest.mark.asyncio
async def test_token_threshold_trips_independently_of_round_count(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["fix z"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(
        tmp_path,
        runner,
        breaker=BreakerConfig(
            max_rounds_per_generation=10, max_generations=3, context_token_limit=150_000
        ),
    )
    runner.context_tokens["sess-1"] = 200_000  # over the limit after round 1
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    coder_g1 = harness.manifest.groups["g1"].sessions[0]
    assert coder_g1.retirement_reason is not None and "context tokens" in coder_g1.retirement_reason


@pytest.mark.asyncio
async def test_generation_cap_fails_the_group_instead_of_respawning(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["never good enough"])],
        }
    )
    harness = Harness(
        tmp_path, runner, breaker=BreakerConfig(max_rounds_per_generation=1, max_generations=1)
    )
    with pytest.raises(GroupFailure, match="generation cap"):
        await harness.run(make_group())
    # the retirement reason still lands in the manifest for the operator
    assert harness.manifest.groups["g1"].sessions[0].retirement_reason is not None


@pytest.mark.asyncio
async def test_too_hard_moves_group_to_rewriting_not_another_round(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("too_hard")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert GroupState.REWRITING in harness.states
    assert len(harness.rewritten) == 1
    # the respawned coder got the rewritten spec, not a handoff
    assert "spec g1 v2" in runner.prompts["sess-3"][0]


@pytest.mark.asyncio
async def test_surprise_during_review_blocks_pending_approval(tmp_path):
    # A group already in review when a surprise names it moves to rewriting
    # instead of merging — even though its reviewer said approved.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("approved")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    surprise = Surprise(
        kind="interface_mismatch", description="g3 changed the API", affected_groups=["g1"]
    )

    def mark_during_review(name: str) -> None:
        if "reviewer-g1" in name:
            harness.board.mark(surprise, source_group="g3")

    runner.on_fork = mark_during_review
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert GroupState.REWRITING in harness.states
    assert harness.rewritten == [[surprise]]
    assert harness.merged == ["g1"]  # merged only after the rewrite cycle


@pytest.mark.asyncio
async def test_upstream_surprise_rewrites_dependent_before_launch(tmp_path):
    # AE5 (rewrite half): the mark is consumed before any session is forked.
    runner = StubRunner(
        {"r1-g2-coder-g1": [coder_report()], "r1-g2-reviewer-g1": [verdict("approved")]}
    )
    harness = Harness(tmp_path, runner)
    surprise = Surprise(
        kind="missing_dependency", description="g1 renamed the helper", affected_groups=["g2"]
    )
    harness.board.mark(surprise, source_group="g1")
    state = await harness.run(make_group("g2"))
    assert state == GroupState.COMPLETED
    assert harness.rewritten == [[surprise]]
    assert runner.forks[0] == "r1-g2-coder-g1"  # still generation 1
    assert "spec g2 v2" in runner.prompts["sess-1"][0]


@pytest.mark.asyncio
async def test_coder_surprises_fan_out_to_other_groups_but_not_itself(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [
                coder_report(
                    surprises=[
                        {
                            "kind": "interface_mismatch",
                            "description": "changed shared enum",
                            "affected_groups": ["g1", "g2", "g3"],
                        }
                    ]
                )
            ],
            "r1-g1-reviewer-g1": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED  # its own surprise must not self-rewrite
    assert harness.board.pending_for("g2") and harness.board.pending_for("g3")
    assert not harness.board.pending_for("g1")


@pytest.mark.asyncio
async def test_paired_plus_runs_one_mandatory_extra_verification_pass(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("approved"), verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group(intensity=ReviewIntensity.PAIRED_PLUS))
    assert state == GroupState.COMPLETED
    reviewer_prompts = runner.prompts["sess-2"]
    assert len(reviewer_prompts) == 2 and "extra verification pass" in reviewer_prompts[1].lower()
    assert (harness.store.paths.group_dir("g1") / "verdict-g1-r1-extra.json").is_file()


@pytest.mark.asyncio
async def test_merge_conflict_routes_to_rewriting_and_fans_out_a_surprise(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("approved")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(MergeConflict("g1 conflicts with g0", ["g0", "g2"]))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert GroupState.MERGING in harness.states and GroupState.REWRITING in harness.states
    assert harness.merged == ["g1"]
    assert harness.board.pending_for("g2")  # conflict surprise reached others


@pytest.mark.asyncio
async def test_rewrite_cap_fails_the_group(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("too_hard")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("too_hard")],
        }
    )
    harness = Harness(tmp_path, runner, execution=ExecutionConfig(max_rewrites=1))
    with pytest.raises(GroupFailure, match="rewrite cap"):
        await harness.run(make_group())


@pytest.mark.asyncio
async def test_blocked_coder_report_escalates_to_rewriting(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report("blocked")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert len(harness.rewritten) == 1


# ------------------------------------------------------------ HITL escalation (Phase D)


def on_stuck(source: str = "workers_via_orchestrator") -> EscalationPolicy:
    return EscalationPolicy("on_stuck", source)


@pytest.mark.asyncio
async def test_needs_input_answer_resumes_the_coder_warm_and_completes(tmp_path):
    # The coder-question channel: escalate → answer → resume the SAME session with
    # the answer, no extra manifest entry, clarification uncounted by the breaker.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [
                coder_report("needs_input", question="Which cache?"),
                coder_report(),
            ],
            "r1-g1-reviewer-g1": [verdict("approved")],
        }
    )
    broker = StubBroker({EscalationKind.CODER_QUESTION: answer("use the LRU")})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED

    # exactly one escalation, of the question kind
    assert [req.kind for req in broker.raised] == [EscalationKind.CODER_QUESTION]
    # no extra session was recorded for the warm resume
    assert [s.name for s in harness.manifest.groups["g1"].sessions] == [
        "r1-g1-coder-g1",
        "r1-g1-reviewer-g1",
    ]
    # the coder's second prompt carried the operator's answer
    assert "use the LRU" in runner.prompts["sess-1"][1]
    # the question report persisted as a q-artifact, distinct from round artifacts
    assert (harness.store.paths.group_dir("g1") / "report-g1-q1.json").is_file()
    assert harness.rewritten == []  # a clarification never rewrites the spec


@pytest.mark.asyncio
async def test_reviewer_too_hard_skip_fails_the_group(tmp_path):
    runner = StubRunner(
        {"r1-g1-coder-g1": [coder_report()], "r1-g1-reviewer-g1": [verdict("too_hard")]}
    )
    broker = StubBroker({EscalationKind.REVIEWER_TOO_HARD: skip()})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    with pytest.raises(GroupFailure, match="operator skipped"):
        await harness.run(make_group())
    assert [req.kind for req in broker.raised] == [EscalationKind.REVIEWER_TOO_HARD]
    assert harness.rewritten == []  # skip fails outright, no rewrite


@pytest.mark.asyncio
async def test_blocked_coder_abort_raises_run_abort_and_trips_the_broker(tmp_path):
    runner = StubRunner({"r1-g1-coder-g1": [coder_report("blocked")]})
    broker = StubBroker({EscalationKind.CODER_BLOCKED: abort()})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    with pytest.raises(RunAbort):
        await harness.run(make_group())
    assert broker.aborted is True  # siblings' waiters were released before unwinding


@pytest.mark.asyncio
async def test_caps_exhausted_answer_grants_one_more_generation(tmp_path):
    # generation cap of 1 would fail the group; the operator's answer grants gen-2.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["again"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    broker = StubBroker({EscalationKind.CAPS_EXHAUSTED: answer("try a smaller diff")})
    harness = Harness(
        tmp_path,
        runner,
        breaker=BreakerConfig(max_rounds_per_generation=1, max_generations=1),
        broker=broker,
        policy=on_stuck(),
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.generations == [2]
    assert [req.kind for req in broker.raised] == [EscalationKind.CAPS_EXHAUSTED]
    # the grant's guidance rode into the generation-2 handoff
    assert "try a smaller diff" in runner.prompts["sess-3"][0]


@pytest.mark.asyncio
async def test_orchestrator_only_downgrades_needs_input_to_the_rewrite_path(tmp_path):
    # With orchestrator_only, a coder question never becomes a warm resume: it is
    # escalated as coder_blocked and the answer guides a rewrite instead.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report("needs_input", question="Which cache?")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    broker = StubBroker({EscalationKind.CODER_BLOCKED: answer("use the LRU")})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck("orchestrator_only"))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    # downgraded: the question surfaced as coder_blocked, never coder_question
    assert [req.kind for req in broker.raised] == [EscalationKind.CODER_BLOCKED]
    # no warm resume happened (the first coder ran exactly one prompt)
    assert len(runner.prompts["sess-1"]) == 1
    # the operator's guidance folded into the rewrite
    assert len(harness.rewritten) == 1
    descriptions = [s.description for s in harness.rewritten[0]]
    assert any("[operator] use the LRU" in d for d in descriptions)


# ------------------------------------------------------------ lifecycle log (R10–R12)


def run_log_lines(harness: Harness) -> list[str]:
    return harness.store.paths.event_log_path.read_text().splitlines()


@pytest.mark.asyncio
async def test_autonomous_run_writes_the_full_lifecycle_log(tmp_path):
    # R10/R11: broker=None, policy=None — the same run.log lines as a HITL run:
    # worktree, round start/end, every verdict (both changes_required cycles), the
    # generation retirement and its follow-up fork, merge attempt/result, completed.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report(), coder_report()],
            "r1-g1-reviewer-g1": [
                verdict("changes_required", ["fix a"]),
                verdict("changes_required", ["fix b"]),
            ],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(
        tmp_path, runner, breaker=BreakerConfig(max_rounds_per_generation=2, max_generations=3)
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    assert harness.deps.broker is None and harness.deps.policy is None

    lines = run_log_lines(harness)
    expected_in_order = [
        f"group g1: worktree ready at {harness.workspace}",
        "group g1 generation 1: coder launched",
        "group g1 generation 1 round 1: started",
        "group g1 generation 1 round 1: reviewer verdict changes_required",
        "group g1 generation 1 round 1: ended (changes_required)",
        "group g1 generation 1 round 2: started",
        "group g1 generation 1 round 2: reviewer verdict changes_required",
        "group g1 generation 1 round 2: ended (changes_required)",
        "group g1 generation 1: coder retired (round threshold reached (2 rounds this generation))",
        "group g1 generation 2: coder launched",  # the follow-up fork
        "group g1 generation 2 round 1: started",
        "group g1 generation 2 round 1: reviewer verdict approved",
        "group g1 generation 2 round 1: ended (approved)",
        "group g1: merge attempt",
        "group g1: merged into the integration branch",
        "group g1: completed",
    ]
    cursor = 0
    for expected in expected_in_order:
        remaining = lines[cursor:]
        matches = [i for i, line in enumerate(remaining) if line.endswith(expected)]
        assert matches, f"missing (or out of order) lifecycle line: {expected!r}\ngot: {lines}"
        cursor += matches[0] + 1
    # R12: plain timestamped append-lines — the existing log_event format, no JSON.
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}T[^ ]+  ", line) for line in lines)


@pytest.mark.asyncio
async def test_autonomous_merge_conflict_writes_the_conflict_line(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("approved")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    harness.merge_failures.append(MergeConflict("g1 conflicts with g0", ["g0"]))
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    lines = run_log_lines(harness)
    assert any(line.endswith("group g1: merge conflict (g1 conflicts with g0)") for line in lines)
    # the retry after the rewrite still logged its attempt and result
    assert sum(line.endswith("group g1: merge attempt") for line in lines) == 2
    assert any(line.endswith("group g1: merged into the integration branch") for line in lines)


@pytest.mark.asyncio
async def test_paired_plus_extra_pass_verdict_is_logged(tmp_path):
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report()],
            "r1-g1-reviewer-g1": [verdict("approved"), verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group(intensity=ReviewIntensity.PAIRED_PLUS))
    assert state == GroupState.COMPLETED
    lines = run_log_lines(harness)
    assert any(line.endswith("reviewer verdict approved (extra pass)") for line in lines)


@pytest.mark.asyncio
async def test_autonomous_policy_never_consults_the_broker(tmp_path):
    # Regression guard: a broker is present but the policy is autonomous, so the
    # blocked/too_hard/conflict paths run exactly as they did pre-Phase-D.
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report("blocked")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    broker = StubBroker({EscalationKind.CODER_BLOCKED: abort()})  # would abort if consulted
    harness = Harness(
        tmp_path,
        runner,
        broker=broker,
        policy=EscalationPolicy("autonomous", "workers_via_orchestrator"),
    )
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED  # autonomous rewrite → gen-2 → approved
    assert broker.raised == []  # the policy short-circuited before any escalation
    assert len(harness.rewritten) == 1
