"""U7 tests: intensity routing, breaker, generations, surprises (plan Phase B).

Sessions are scripted in-process: StubRunner plays canned final messages per
session, so every scenario (approve, reject-then-approve, reject-forever,
surprise, too-hard) runs with zero subprocesses and zero tokens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.config import BreakerConfig, ExecutionConfig
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.review import (
    GroupFailure,
    MergeConflict,
    ReviewDeps,
    SurpriseBoard,
    make_executor,
)
from orchestrator.execution.scheduler import GroupContext, GroupState
from orchestrator.execution.sessions import RoundResult, RoundUsage, SessionUsage
from orchestrator.model import (
    Group,
    ReviewIntensity,
    RunManifest,
    Surprise,
    VerificationItem,
)


def coder_report(status: str = "completed", surprises: list[dict] | None = None) -> str:
    body = {
        "status": status,
        "summary": "round done",
        "verification_results": [{"item_id": "v1", "status": "pass", "notes": ""}],
        "surprises": surprises or [],
    }
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


class Harness:
    """Deps + context wiring shared by every scenario."""

    def __init__(self, tmp_path: Path, runner: StubRunner, *, breaker=None, execution=None):
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
