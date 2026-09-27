"""The `evaluate` recipe's executor: run's declared commands plus a
hash-checked harness and an optional smoke gate (plan U8).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.confinement import landlock_abi_version
from orchestrator.execution.evaluate_executor import make_executor
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.scheduler import GroupFailure, GroupState
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.model import Group, ReviewIntensity, RunManifest

pytestmark = pytest.mark.skipif(landlock_abi_version() <= 0, reason="Landlock unavailable")


@pytest.fixture(autouse=True)
def fake_claude_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "score.sh").write_text(
        '#!/bin/sh\necho \'{"score": 3, "guard": 1}\' > measurements.json\n'
    )
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def make_group(recipe_args: dict, gid: str = "g11") -> Group:
    return Group(
        id=gid,
        name="evaluate recipe test",
        summary="an evaluate group",
        spec="evaluate this",
        difficulty=0.0,
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="evaluate",
        recipe_args=recipe_args,
        verification=[],
    )


class FakeContext:
    def __init__(self, group: Group, generation: int = 0):
        self.group = group
        self.generation = generation
        self.states: list[GroupState] = []

    def set_state(self, state: GroupState) -> None:
        self.states.append(state)

    def set_generation(self, generation: int) -> None:
        self.generation = generation

    def record_cure(self) -> int:
        return 0


def make_deps(repo_root: Path, run_dir: Path, workspace: Path, *, triage=None, merge_group=None):
    paths = RunPaths(repo_root, "rtest", run_dir=run_dir)
    store = ManifestStore(paths)

    def default_merge(group: Group, wt: Path) -> str:
        return git(wt, "rev-parse", "HEAD").strip()

    return SimpleNamespace(
        run_id="rtest",
        store=store,
        manifest=RunManifest(run_id="rtest", plan_path="plan.md"),
        board=SurpriseBoard(),
        workspace_for=lambda group: workspace,
        merge_group=merge_group or default_merge,
        triage=triage,
        broker=None,
        policy=None,
        artifacts=ArtifactManifestStore(paths),
        workspace_config=None,
    )


async def _run(deps, group, generation=0):
    ctx = FakeContext(group, generation)
    executor = make_executor(deps)
    state = await executor(ctx)
    return state, ctx


def base_args(**overrides) -> dict:
    args = {
        "commands": [{"cmd": "sh scripts/score.sh", "wall_clock_min": 1}],
        "measurements": "measurements.json",
        "commit_paths": ["measurements.json"],
        "kpi": {
            "key": "score",
            "direction": "max",
            "harness_paths": ["scripts/score.sh"],
        },
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------- happy path


def test_evaluate_group_completes_with_kpi_value_and_recorded_harness_hash(tmp_path, repo):
    run_dir = tmp_path / "run"
    deps = make_deps(repo, run_dir, repo)
    group = make_group(base_args())

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    entry = deps.artifacts.load().entries["g11"]
    assert entry.schema_name == "EvaluationRecord"
    assert entry.measurements["score"] == 3
    assert entry.status == "complete"
    hash_path = run_dir / "groups" / "g11" / "run" / "eval" / "harness.sha256"
    assert hash_path.is_file()


# ----------------------------------------------------- harness tamper on retry


def test_editing_harness_between_attempts_fails_the_second_attempt_naming_it(tmp_path, repo):
    run_dir = tmp_path / "run"
    deps = make_deps(repo, run_dir, repo)
    group = make_group(base_args())

    state, _ctx = asyncio.run(_run(deps, group))
    assert state == GroupState.COMPLETED

    # Tamper with the harness after the first evaluation established the
    # baseline hash, then re-invoke the same group (an operator retry).
    (repo / "scripts" / "score.sh").write_text(
        '#!/bin/sh\necho \'{"score": 4, "guard": 1}\' > measurements.json\n'
    )

    with pytest.raises(GroupFailure) as exc:
        asyncio.run(_run(deps, make_group(base_args()), generation=1))
    assert "scripts/score.sh" in str(exc.value)


# ------------------------------------------------------------- missing KPI key


def test_harness_json_lacking_the_kpi_key_fails_naming_it(tmp_path, repo):
    (repo / "scripts" / "score.sh").write_text(
        "#!/bin/sh\necho '{\"other\": 1}' > measurements.json\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "swap harness")
    run_dir = tmp_path / "run"
    calls = []
    deps = make_deps(
        repo,
        run_dir,
        repo,
        triage=lambda p: calls.append(p) or {"verdict": "work_failure", "diagnosis": "no score"},
    )
    group = make_group(base_args())

    with pytest.raises(GroupFailure) as exc:
        asyncio.run(_run(deps, group))
    assert "score" in str(exc.value)
    assert len(calls) == 1


# ------------------------------------------------------------------- smoke gate


def test_failing_smoke_command_fails_with_no_result_for_command_one(tmp_path, repo):
    run_dir = tmp_path / "run"
    calls = []
    deps = make_deps(
        repo,
        run_dir,
        repo,
        triage=lambda p: (
            calls.append(p) or {"verdict": "work_failure", "diagnosis": "smoke failed"}
        ),
    )
    group = make_group(base_args(**{"kpi": {**base_args()["kpi"], "smoke": "exit 1"}}))

    with pytest.raises(GroupFailure) as exc:
        asyncio.run(_run(deps, group))
    assert "smoke failed" in str(exc.value)
    assert len(calls) == 1
    attempt_dir = run_dir / "groups" / "g11" / "run" / "attempt-1"
    assert not (attempt_dir / "1.result.json").is_file()


def test_passing_smoke_command_lets_the_real_commands_run(tmp_path, repo):
    run_dir = tmp_path / "run"
    deps = make_deps(repo, run_dir, repo)
    group = make_group(base_args(**{"kpi": {**base_args()["kpi"], "smoke": "true"}}))

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    entry = deps.artifacts.load().entries["g11"]
    assert entry.measurements["score"] == 3
