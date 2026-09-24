"""The `run` recipe's executor: confined Run Children, artifact registration,
commit_paths enforcement, retry/from-start resume, and timeout/triage (plan U8).
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.confinement import build_policy, landlock_abi_version, landlock_preexec
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.run_child import boot_id, launch, process_alive
from orchestrator.execution.run_executor import make_executor
from orchestrator.execution.scheduler import GroupFailure, GroupState
from orchestrator.model import Group, ReviewIntensity

pytestmark = pytest.mark.skipif(landlock_abi_version() <= 0, reason="Landlock unavailable")


@pytest.fixture(autouse=True)
def fake_claude_home(tmp_path, monkeypatch):
    """``_confinement_preexec`` derives ``claude_home`` from ``Path.home()`` —
    pointed at a writable fake here so the real (and in this sandbox,
    read-only) ``~/.claude`` is never touched by a test."""
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
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def make_group(recipe_args: dict, gid: str = "g7") -> Group:
    return Group(
        id=gid,
        name="run recipe test",
        summary="a run group",
        spec="run this",
        difficulty=0.0,
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="run",
        recipe_args=recipe_args,
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


def make_deps(
    repo_root: Path,
    run_dir: Path,
    workspace: Path,
    *,
    triage=None,
    broker=None,
    policy=None,
    merge_group=None,
):
    paths = RunPaths(repo_root, "rtest", run_dir=run_dir)
    store = SimpleNamespace(paths=paths)

    def default_merge(group: Group, wt: Path) -> str:
        return git(wt, "rev-parse", "HEAD").strip()

    return SimpleNamespace(
        run_id="rtest",
        store=store,
        workspace_for=lambda group: workspace,
        merge_group=merge_group or default_merge,
        triage=triage,
        broker=broker,
        policy=policy,
        artifacts=ArtifactManifestStore(paths),
        workspace_config=None,
    )


async def _run(deps, group, generation=0):
    ctx = FakeContext(group, generation)
    executor = make_executor(deps)
    state = await executor(ctx)
    return state, ctx


# ------------------------------------------------------------------- g7-1


def test_python_command_writes_measurements_and_completes(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {
        "commands": [
            {
                "cmd": (
                    'python3 -c "import json,pathlib; '
                    "pathlib.Path('score.json').write_text(json.dumps({'score': 0.9}))\""
                ),
                "wall_clock_min": 1,
            }
        ],
        "outputs": ["score.json"],
        "measurements": "score.json",
        "commit_paths": ["score.json"],
    }
    group = make_group(args)
    deps = make_deps(repo, run_dir, repo)

    state, ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    manifest = deps.artifacts.load()
    entry = manifest.entries["g7"]
    assert entry.measurements == {"score": 0.9}
    assert entry.status == "complete"
    expected_sha = hashlib.sha256((repo / "score.json").read_bytes()).hexdigest()
    assert entry.sha256["score.json"] == expected_sha
    assert GroupState.RUNNING in ctx.states


# ------------------------------------------------------------------- g7-2


def test_timeout_kills_process_group_and_calls_triage_once(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {"commands": [{"cmd": "sleep 30", "wall_clock_min": 0.05}]}
    group = make_group(args)
    calls = []

    def fake_triage(prompt: str) -> dict:
        calls.append(prompt)
        return {"verdict": "work_failure", "diagnosis": "the command hung"}

    deps = make_deps(repo, run_dir, repo, triage=fake_triage)

    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, group))

    assert len(calls) == 1
    assert "timed out" in calls[0].lower() or "exceeded" in calls[0].lower()
    time.sleep(0.3)
    # No `sleep` process left in /proc under this group's run dir's state.
    state_files = list((run_dir / "groups" / "g7" / "run").glob("attempt-1/1.state.json"))
    assert state_files
    from orchestrator.execution.run_child import RunChildState

    state = RunChildState.model_validate_json(state_files[0].read_text())
    assert not process_alive(state.pid)


# ------------------------------------------------------------------- g7-3


def test_re_adoption_after_simulated_crash(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {"commands": [{"cmd": "sleep 3", "wall_clock_min": 1}]}
    group = make_group(args)
    deps = make_deps(repo, run_dir, repo)

    attempt_dir = run_dir / "groups" / "g7" / "run" / "attempt-1"
    attempt_dir.mkdir(parents=True)
    proc = launch(
        "sleep 3",
        cwd=repo,
        exit_path=attempt_dir / "1.exit",
        out_path=attempt_dir / "1.out",
        err_path=attempt_dir / "1.err",
        preexec_fn=None,
    )
    from datetime import UTC, datetime

    from orchestrator.execution.run_child import RunChildState, proc_cmdline_head, proc_starttime

    state = RunChildState(
        n=1,
        pid=proc.pid,
        starttime=proc_starttime(proc.pid),
        cmdline_head=proc_cmdline_head(proc.pid),
        started_at=datetime.now(UTC).isoformat(),
        started_monotonic=time.monotonic(),
        boot_id=boot_id(),
    )
    (attempt_dir / "1.state.json").write_text(state.model_dump_json())
    # "drop the executor" (simulated crash): we never call wait_exit or track
    # `proc` in a new executor — construct one fresh, as a resumed run would.

    result_state, ctx = asyncio.run(_run(deps, group))
    assert result_state == GroupState.COMPLETED
    proc.wait(timeout=1)  # reap: this test is proc's direct parent


# ------------------------------------------------------------------- g7-4


def test_confinement_narrower_than_worker_profile(tmp_path):
    worktree = tmp_path / "worktree"
    data_dir = tmp_path / "data"
    extra_dir = tmp_path / "extra"
    undeclared_dir = tmp_path / "undeclared"
    for d in (worktree, data_dir, extra_dir, undeclared_dir):
        d.mkdir()
    claude_home = tmp_path / "claude_home"
    claude_home.mkdir()

    policy = build_policy(
        worktree=worktree,
        claude_home=claude_home,
        system_paths=[],
        extra_write=[data_dir, extra_dir],
    )
    preexec_fn, result = landlock_preexec(policy)
    assert result.applied

    def probe(target: Path) -> int:
        proc = subprocess.run(
            ["sh", "-c", f"echo hi > {target / 'probe.txt'}"],
            preexec_fn=preexec_fn,
        )
        return proc.returncode

    assert probe(worktree) == 0
    assert probe(data_dir) == 0
    assert probe(extra_dir) == 0
    assert probe(undeclared_dir) != 0


# ------------------------------------------------------------------- g7-6


def test_command_editing_tracked_file_outside_commit_paths_fails_naming_it(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {
        "commands": [{"cmd": "echo changed > README.md", "wall_clock_min": 1}],
        "commit_paths": ["score.json"],
    }
    group = make_group(args)
    calls = []
    deps = make_deps(
        repo,
        run_dir,
        repo,
        triage=lambda p: calls.append(p) or {"verdict": "work_failure", "diagnosis": "bad diff"},
    )

    with pytest.raises(GroupFailure) as exc:
        asyncio.run(_run(deps, group))
    assert "README.md" in str(exc.value)
    git(repo, "checkout", "--", "README.md")


# ------------------------------------------------------------------- g7-9


def test_cancelling_the_task_kills_the_child(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {"commands": [{"cmd": "sleep 30", "wall_clock_min": 5}]}
    group = make_group(args)
    deps = make_deps(repo, run_dir, repo)
    ctx = FakeContext(group)
    executor = make_executor(deps)

    async def go():
        task = asyncio.ensure_future(executor(ctx))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    state_path = run_dir / "groups" / "g7" / "run" / "attempt-1" / "1.state.json"
    from orchestrator.execution.run_child import RunChildState

    state = RunChildState.model_validate_json(state_path.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and process_alive(state.pid):
        time.sleep(0.1)
    assert not process_alive(state.pid)


# ------------------------------------------------------------------- g7-10


def test_retry_resumes_only_failed_commands_and_from_start_reruns_all(tmp_path, repo):
    run_dir = tmp_path / "run"
    marker = repo / "marker.txt"

    args = {
        "commands": [
            {"cmd": f"echo one >> {marker}", "wall_clock_min": 1},
            {"cmd": "exit 1", "wall_clock_min": 1},
            {"cmd": f"echo three >> {marker}", "wall_clock_min": 1},
        ],
        "commit_paths": ["marker.txt"],
    }
    group = make_group(args)
    deps = make_deps(
        repo,
        run_dir,
        repo,
        triage=lambda p: {"verdict": "work_failure", "diagnosis": "command 2 failed"},
    )

    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, group))
    assert marker.read_text().splitlines() == ["one"]

    # Operator fixes command 2's underlying cause and retries: only commands
    # 2 and 3 relaunch — command 1 is never relaunched.
    args2 = dict(args)
    args2["commands"] = [
        args["commands"][0],
        {"cmd": f"echo two >> {marker}", "wall_clock_min": 1},
        args["commands"][2],
    ]
    group2 = make_group(args2)
    state, _ctx = asyncio.run(_run(deps, group2, generation=1))
    assert state == GroupState.COMPLETED
    assert marker.read_text().splitlines() == ["one", "two", "three"]


def test_retry_from_start_reruns_every_command(tmp_path, repo):
    run_dir = tmp_path / "run"
    marker = repo / "marker2.txt"
    args = {
        "commands": [
            {"cmd": f"echo one >> {marker}", "wall_clock_min": 1},
            {"cmd": "exit 1", "wall_clock_min": 1},
        ],
        "commit_paths": ["marker2.txt"],
    }
    group = make_group(args)
    deps = make_deps(
        repo, run_dir, repo, triage=lambda p: {"verdict": "work_failure", "diagnosis": "boom"}
    )

    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, group))
    assert marker.read_text().splitlines() == ["one"]

    note_path = run_dir / "groups" / "g7" / "run" / "retry-note.txt"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text("operator retry --from-start: fixed the environment")

    args2 = dict(args)
    args2["commands"] = [args["commands"][0], {"cmd": f"echo two >> {marker}", "wall_clock_min": 1}]
    group2 = make_group(args2)
    state, _ctx = asyncio.run(_run(deps, group2, generation=1))
    assert state == GroupState.COMPLETED
    # Command 1 ran again (from-start), so "one" appears twice, then "two" once.
    assert marker.read_text().splitlines() == ["one", "one", "two"]


# ------------------------------------------------------------------- g7-12


def test_missing_declared_output_goes_to_triage(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {
        "commands": [{"cmd": "true", "wall_clock_min": 1}],
        "outputs": ["never-written.txt"],
    }
    group = make_group(args)
    calls = []
    deps = make_deps(
        repo,
        run_dir,
        repo,
        triage=lambda p: (
            calls.append(p) or {"verdict": "work_failure", "diagnosis": "output missing"}
        ),
    )
    with pytest.raises(GroupFailure) as exc:
        asyncio.run(_run(deps, group))
    assert "never-written.txt" in str(exc.value)
    assert len(calls) == 1


def test_missing_measurements_file_completes_with_note(tmp_path, repo):
    run_dir = tmp_path / "run"
    args = {
        "commands": [{"cmd": "true", "wall_clock_min": 1}],
        "measurements": "absent.json",
    }
    group = make_group(args)
    deps = make_deps(repo, run_dir, repo)
    state, _ctx = asyncio.run(_run(deps, group))
    assert state == GroupState.COMPLETED
    entry = deps.artifacts.load().entries["g7"]
    assert "measurements_missing" in entry.summary
    assert entry.measurements == {}
