"""The `optimize` recipe's executor (plan U10): one candidate, one
evaluation, one decision per round, against a real git repo, real Landlock
confinement, and a real (scripted) `claude` subprocess (`fake_claude.py`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config import BreakerConfig, ExecutionConfig, OptimizeRecipeConfig, RecipesConfig
from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.confinement import landlock_abi_version
from orchestrator.execution.kpi import Attempt, Ledger
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.optimize_executor import make_executor
from orchestrator.execution.scheduler import GroupFailure, GroupState
from orchestrator.execution.sessions import SessionRunner, session_display_name
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.model import (
    EscalationResponse,
    Group,
    ReviewIntensity,
    RunManifest,
)

pytestmark = pytest.mark.skipif(landlock_abi_version() <= 0, reason="Landlock unavailable")

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def fake_claude_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    (home / "scripts").mkdir()
    return home


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


SCORE_SH = '#!/bin/sh\necho "{\\"score\\": $(cat value.txt)}" > measurements.json\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "score.sh").write_text(SCORE_SH)
    (repo / "value.txt").write_text("5\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def make_runner(fake_home: Path) -> SessionRunner:
    return SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env={"FAKE_CLAUDE_HOME": str(fake_home)},
        transcript_root=fake_home / "projects",
        confine=False,  # fake_claude.py writes its own bookkeeping outside the worktree
    )


def script_session(fake_home: Path, name: str, *entries: dict) -> None:
    with (fake_home / "scripts" / f"{name}.jsonl").open("a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def calls(fake_home: Path) -> list[dict]:
    path = fake_home / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def optimize_report(status: str = "completed", candidate: str = "", surprises=None) -> str:
    body = {
        "status": status,
        "summary": "round done",
        "verification_results": [],
        "surprises": surprises or [],
        "candidate": candidate,
    }
    return f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


def verdict_entry(status: str = "approved", notes: str = "") -> dict:
    body = {"status": status, "required_changes": [], "surprises": [], "notes": notes}
    return {"result": f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'}


def candidate_round(value: str, msg: str = "candidate", extra_files: dict | None = None) -> dict:
    files = {"value.txt": f"{value}\n"}
    if extra_files:
        files.update(extra_files)
    return {
        "result": optimize_report(candidate=f"set value to {value}"),
        "files": files,
        "commit": msg,
        # A value equal to the champion's is an empty commit now that the loop
        # archives measurements.json instead of leaving it for `git add -A` to
        # sweep into every candidate (r20260927 g3-5).
        "allow_empty": True,
    }


def make_group(recipe_args: dict, gid: str = "g10", files: list[str] | None = None) -> Group:
    return Group(
        id=gid,
        name="optimize recipe test",
        summary="an optimize group",
        spec="optimize this",
        difficulty=0.0,
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="optimize",
        recipe_args=recipe_args,
        files=files if files is not None else ["value.txt"],
        verification=[],
    )


def base_optimize_args(**overrides) -> dict:
    args = {
        "commands": [{"cmd": "sh scripts/score.sh", "wall_clock_min": 1}],
        "measurements": "measurements.json",
        "kpi": {
            "key": "score",
            "direction": "max",
            "min_effect": 1,
            "harness_paths": ["scripts/score.sh"],
        },
        "evaluations": 4,
    }
    args.update(overrides)
    return args


class FakeContext:
    def __init__(self, group: Group, generation: int = 1):
        self.group = group
        self.generation = generation
        self.states: list[GroupState] = []

    def set_state(self, state: GroupState) -> None:
        self.states.append(state)

    def set_generation(self, generation: int) -> None:
        self.generation = generation

    def record_cure(self) -> int:
        return 0


class StubBroker:
    def __init__(self, response: EscalationResponse | None = None):
        self.response = response
        self.raised: list = []

    def raise_escalation(self, request):
        self.raised.append(request)
        return self.response

    def trigger_abort(self) -> None:
        pass


class CapsExhaustedOnlyPolicy:
    source = "operator"

    def should_escalate(self, kind) -> bool:
        from orchestrator.model import EscalationKind

        return kind == EscalationKind.CAPS_EXHAUSTED


def make_deps(
    repo_root: Path,
    run_dir: Path,
    workspace: Path,
    runner: SessionRunner,
    *,
    broker=None,
    policy=None,
    recipes_config: RecipesConfig | None = None,
):
    paths = RunPaths(repo_root, "rtest", run_dir=run_dir)
    store = ManifestStore(paths)

    def default_merge(group: Group, wt: Path) -> str:
        return git(wt, "rev-parse", "HEAD").strip()

    return SimpleNamespace(
        run_id="rtest",
        runner=runner,
        store=store,
        manifest=RunManifest(run_id="rtest", plan_path="plan.md"),
        base_context="",
        base_session_id=None,
        fork_base_session=False,
        breaker=BreakerConfig(),
        execution=ExecutionConfig(),
        board=SurpriseBoard(),
        workspace_for=lambda group: workspace,
        merge_group=default_merge,
        rewrite_spec=lambda group, surprises: group,
        base_ref_for=lambda group: "main",
        broker=broker,
        policy=policy,
        preflight_baseline=None,
        provisioning_failure_for=None,
        activity=None,
        liveness=None,
        triage=None,
        workspace_config=None,
        recipes_config=recipes_config or RecipesConfig(enabled=["optimize"]),
        artifacts=ArtifactManifestStore(paths),
        groups_by_id={},
    )


async def _run(deps, group, generation: int = 1):
    ctx = FakeContext(group, generation)
    executor = make_executor(deps)
    state = await executor(ctx)
    return state, ctx


def coder_name(gid: str, generation: int = 1) -> str:
    return session_display_name("rtest", gid, "coder", generation)


def reviewer_name(gid: str, generation: int = 1) -> str:
    return session_display_name("rtest", gid, "reviewer", generation)


# ---------------------------------------------------------------- happy path


def test_keep_or_revert_sequence_matches_the_ledger_and_merges_the_champion(
    tmp_path, repo, fake_home
):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=4))
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("5", "round1"),
        candidate_round("9", "round2"),
        candidate_round("9", "round3"),
        candidate_round("2", "round4"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    outcomes = [a.outcome for a in ledger.attempts]
    assert outcomes == ["inconclusive", "keep", "inconclusive", "discard"]
    assert len(ledger.attempts) == 4
    assert (repo / "value.txt").read_text().strip() == "9"


# ------------------------------------------------------- harness / mutable region


def test_candidate_editing_harness_is_a_mutable_violation_and_discarded(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=1))
    # A mutable violation is discarded unscored and never spends the
    # evaluation cap, so a second, clean candidate is what actually ends
    # this generation.
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("9", "round1", extra_files={"scripts/score.sh": SCORE_SH + "\n"}),
        candidate_round("9", "round2"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    assert ledger.attempts[0].outcome == "discard"
    assert "scripts/score.sh" in ledger.attempts[0].why
    assert not (run_dir / "groups" / group.id / "eval" / "attempt-1").exists()


def test_candidate_touching_file_outside_files_is_a_mutable_violation(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=1))
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("9", "round1", extra_files={"other.txt": "surprise\n"}),
        candidate_round("9", "round2"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    assert ledger.attempts[0].outcome == "discard"
    assert "other.txt" in ledger.attempts[0].why
    assert not (run_dir / "groups" / group.id / "eval" / "attempt-1").exists()


def test_harness_edited_outside_the_loop_between_evaluations_fails_naming_the_path(
    tmp_path, repo, fake_home
):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=1))
    script_session(fake_home, coder_name(group.id), candidate_round("9", "round1"))

    state, _ctx = asyncio.run(_run(deps, group))
    assert state == GroupState.COMPLETED

    # Tamper with the harness directly (no coder involved) after the loop
    # established its baseline hash.
    (repo / "scripts" / "score.sh").write_text(SCORE_SH + "# tampered\n")

    deps2 = make_deps(repo, run_dir, repo, runner)
    group2 = make_group(base_optimize_args(evaluations=1))
    with pytest.raises(GroupFailure) as exc:
        asyncio.run(_run(deps2, group2, generation=2))
    assert "scripts/score.sh" in str(exc.value)


# ----------------------------------------------------------------- zero hit / patience


def test_zero_hit_loop_ends_completed_with_ledger_artifact_and_no_merge(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=3))
    # Distinct values each round (a repeat would be an empty commit) that
    # never clear the threshold against the unchanging champion of 5.
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("5", "round1"),
        candidate_round("3", "round2"),
        candidate_round("4", "round3"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    assert len(ledger.attempts) == 3
    assert all(a.outcome != "keep" for a in ledger.attempts)
    entry = deps.artifacts.load().entries[group.id]
    assert entry.schema_name == "Ledger"
    assert (repo / "value.txt").read_text().strip() == "5"


def test_patience_exhausted_raises_escalation_with_ledger_and_none_ends_completed(
    tmp_path, repo, fake_home
):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    broker = StubBroker(response=None)
    policy = CapsExhaustedOnlyPolicy()
    deps = make_deps(
        repo,
        run_dir,
        repo,
        runner,
        broker=broker,
        policy=policy,
        recipes_config=RecipesConfig(
            enabled=["optimize"], optimize=OptimizeRecipeConfig(patience=2)
        ),
    )
    group = make_group(base_optimize_args(evaluations=100))
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("5", "round1"),
        candidate_round("3", "round2"),
        candidate_round("4", "round3"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    assert len(broker.raised) == 1
    prompt = broker.raised[0].prompt
    assert "patience" in prompt
    assert "| round |" in prompt  # the ledger table
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    assert len(ledger.attempts) == 3
    entry = deps.artifacts.load().entries[group.id]
    assert entry.schema_name == "Ledger"


def test_consecutive_reverts_exhausted_raises_escalation_before_patience(tmp_path, repo, fake_home):
    """`consecutive_reverts` counts only discard/crash — a run of candidates
    that are actively ruled out, never `inconclusive`. Set below `patience`
    it fires first on a straight run of discards."""
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    broker = StubBroker(response=None)
    policy = CapsExhaustedOnlyPolicy()
    deps = make_deps(
        repo,
        run_dir,
        repo,
        runner,
        broker=broker,
        policy=policy,
        recipes_config=RecipesConfig(
            enabled=["optimize"], optimize=OptimizeRecipeConfig(patience=4, consecutive_reverts=2)
        ),
    )
    group = make_group(base_optimize_args(evaluations=100))
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("3", "round1"),
        candidate_round("2", "round2"),
        candidate_round("1", "round3"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    assert len(broker.raised) == 1
    prompt = broker.raised[0].prompt
    assert "reverts" in prompt
    assert "| round |" in prompt  # the ledger table
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    outcomes = [a.outcome for a in ledger.attempts]
    assert outcomes == ["discard", "discard", "discard"]
    entry = deps.artifacts.load().entries[group.id]
    assert entry.schema_name == "Ledger"


# --------------------------------------------------------------------- promising


def _seed_noisy_ledger(run_dir: Path, gid: str) -> None:
    """Two prior attempts whose deltas spread — establishes a noise floor > 0
    before the round under test runs, so a clearing candidate is `promising`,
    not an outright `keep`."""
    ledger_path = run_dir / "groups" / gid / "ledger.json"
    ledger = Ledger()
    for round_no, delta in ((1, 0.0), (2, 4.0)):
        ledger.attempts.append(
            Attempt(
                round_no=round_no,
                candidate_commit="0" * 40,
                kpi_value=5.0 + delta,
                guard_values={},
                delta=delta,
                noise_floor=0.0,
                outcome="inconclusive",
                harness_hash="seed",
                why="seeded",
                at="2026-01-01T00:00:00+00:00",
            )
        )
    ledger.save(ledger_path)


def test_promising_candidate_confirmed_by_reviewer_is_kept(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    (run_dir / "groups" / "g10").mkdir(parents=True)
    _seed_noisy_ledger(run_dir, "g10")
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=2))
    script_session(fake_home, coder_name(group.id), candidate_round("9", "round1"))
    script_session(fake_home, reviewer_name(group.id), verdict_entry("approved"))

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    last = ledger.attempts[-1]
    assert last.outcome == "keep"
    assert (repo / "value.txt").read_text().strip() == "9"
    reviewer_calls = [c for c in calls(fake_home) if "cheat reviewer" in c.get("prompt", "")]
    assert reviewer_calls


def test_promising_candidate_rejected_by_cheat_reviewer_is_discarded(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    (run_dir / "groups" / "g10").mkdir(parents=True)
    _seed_noisy_ledger(run_dir, "g10")
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=1))
    script_session(fake_home, coder_name(group.id), candidate_round("9", "round1"))
    script_session(
        fake_home,
        reviewer_name(group.id),
        verdict_entry("changes_required", notes="looks tuned to the seed"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    last = ledger.attempts[-1]
    assert last.outcome == "discard"
    assert "looks tuned to the seed" in last.why
    assert (repo / "value.txt").read_text().strip() == "5"
def test_first_successful_evaluation_writes_the_harness_pin(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    runner = make_runner(fake_home)
    deps = make_deps(repo, run_dir, repo, runner)
    group = make_group(base_optimize_args(evaluations=1))
    script_session(fake_home, coder_name(group.id), candidate_round("9", "round1"))

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    pin = run_dir / "groups" / group.id / "eval" / "harness.sha256"
    assert pin.is_file()
