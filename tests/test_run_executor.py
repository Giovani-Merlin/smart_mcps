"""The `run` recipe's executor: confined Run Children, artifact registration,
commit_paths enforcement, retry/from-start resume, and timeout/triage (plan U8).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.confinement import build_policy, landlock_abi_version, landlock_preexec
from orchestrator.execution.manifest import ManifestStore, RunPaths, latest_report
from orchestrator.execution.run_child import boot_id, launch, process_alive
from orchestrator.execution.run_executor import make_executor
from orchestrator.execution.scheduler import GroupFailure, GroupState
from orchestrator.execution.surprises import SurpriseBoard
from orchestrator.model import (
    Group,
    ReviewIntensity,
    RunManifest,
    SessionRole,
    Surprise,
    VerificationItem,
)

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


def make_group(
    recipe_args: dict, gid: str = "g7", verification: list[VerificationItem] | None = None
) -> Group:
    return Group(
        id=gid,
        name="run recipe test",
        summary="a run group",
        spec="run this",
        difficulty=0.0,
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="run",
        recipe_args=recipe_args,
        verification=verification or [],
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


# ----------------------------------------------- no commit_paths: merge skipped


def test_unit_that_commits_nothing_completes_without_merging(tmp_path, repo):
    # A run unit with no commit_paths "never commits" (plan). The real merge
    # refuses a branch with no commits ahead, so the executor must skip it —
    # found by the driver's live g7-7 probe on r20260924-134934.
    git(repo, "branch", "orchestrator/run-rtest")
    run_dir = tmp_path / "run"
    group = make_group({"commands": [{"cmd": "true", "wall_clock_min": 1}]})

    def refusing_merge(group: Group, wt: Path) -> str:
        raise AssertionError("merge_group called for a unit that committed nothing")

    deps = make_deps(repo, run_dir, repo, merge_group=refusing_merge)

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    head = git(repo, "rev-parse", "HEAD").strip()
    assert deps.artifacts.load().entries["g7"].status == "complete"
    assert git(repo, "rev-parse", "orchestrator/run-rtest").strip() == head


# ------------------------------------------------------- heartbeat label


def test_heartbeat_label_advances_while_a_command_runs(tmp_path, repo, monkeypatch):
    """r20260924: the run recipe's `command 1/1 · 0s/60s` label froze for the
    whole command because it was marked once at launch. Every poll must
    relabel it, and only the first one may restart the phase."""
    from orchestrator.execution import run_executor as mod
    from orchestrator.execution.heartbeat import RoundHeartbeat

    monkeypatch.setattr(mod, "DEFAULT_POLL_INTERVAL_S", 0.05)
    marked: list[str] = []
    relabelled: list[str] = []
    real_mark = RoundHeartbeat.mark_phase
    real_relabel = RoundHeartbeat.relabel_phase

    def spy_mark(self, phase):
        marked.append(phase)
        real_mark(self, phase)

    def spy_relabel(self, phase):
        relabelled.append(phase)
        real_relabel(self, phase)

    monkeypatch.setattr(RoundHeartbeat, "mark_phase", spy_mark)
    monkeypatch.setattr(RoundHeartbeat, "relabel_phase", spy_relabel)

    args = {"commands": [{"cmd": "sleep 1.5", "wall_clock_min": 1}], "commit_paths": []}
    deps = make_deps(repo, tmp_path / "run", repo)
    state, _ctx = asyncio.run(_run(deps, make_group(args)))

    assert state == GroupState.COMPLETED
    command_labels = [m for m in marked if m.startswith("command 1/1 · ")]
    assert len(command_labels) == 1, marked
    assert command_labels[0].endswith("/60s")
    assert len(set(relabelled)) >= 2, relabelled
    assert all(
        label.startswith("command 1/1 · ") and label.endswith("/60s") for label in relabelled
    )


# ---------------------------------------------------- r20260925-101742 g5


def test_exit_file_is_writable_when_the_run_dir_is_outside_tmp(tmp_path, repo, monkeypatch):
    """The launch wrapper writes ``<n>.exit`` under the run dir, which lives in
    the main checkout's ``.orchestrator/`` — outside the worktree and outside
    ``/tmp``. r20260925-101742 g5 ran a one-second failure to its 20-minute cap
    because that write was denied. Dropping the system rules here is what makes
    a ``tmp_path`` run dir reproduce the production layout."""
    import orchestrator.execution.run_executor as mod

    monkeypatch.setattr(mod, "system_write_paths", lambda: [])
    run_dir = tmp_path / "run"
    args = {"commands": [{"cmd": "true", "wall_clock_min": 0.1}]}
    deps = make_deps(repo, run_dir, repo)

    state, _ = asyncio.run(_run(deps, make_group(args)))

    assert state == GroupState.COMPLETED
    exit_file = run_dir / "groups" / "g7" / "run" / "attempt-1" / "1.exit"
    assert exit_file.read_text().strip() == "0"


def test_child_env_points_toolchain_caches_under_the_worker_cache_root(tmp_path, repo):
    """A confined Run Child only gets write access to the orchestrator-owned
    cache root, so its environment must point every toolchain there — the
    same overlay a coder session gets. Otherwise ``uv run`` fails on its first
    open of ``~/.cache/uv`` (r20260925-101742 g5)."""
    from orchestrator.execution.confinement import default_cache_root

    monkeypatch_env = {"VIRTUAL_ENV": "/nonexistent/venv"}
    for key, value in monkeypatch_env.items():
        os.environ[key] = value
    try:
        run_dir = tmp_path / "run"
        args = {
            "commands": [
                {
                    "cmd": 'sh -c \'echo "$UV_CACHE_DIR" > uv.txt; echo "${VIRTUAL_ENV:-unset}" > venv.txt; echo probe > "$UV_CACHE_DIR/probe.txt"\'',
                    "wall_clock_min": 0.1,
                }
            ],
            "outputs": ["uv.txt", "venv.txt"],
            "commit_paths": ["uv.txt", "venv.txt"],
        }
        deps = make_deps(repo, run_dir, repo)

        state, _ = asyncio.run(_run(deps, make_group(args)))
    finally:
        for key in monkeypatch_env:
            os.environ.pop(key, None)

    assert state == GroupState.COMPLETED
    recorded = Path((repo / "uv.txt").read_text().strip())
    assert recorded == default_cache_root() / "uv"
    assert (recorded / "probe.txt").read_text().strip() == "probe"
    assert (repo / "venv.txt").read_text().strip() == "unset"


def test_triage_prompt_carries_the_failing_command_output(tmp_path, repo):
    """The triage call diagnoses from the prompt alone; on r20260925-101742 it
    blamed a slow sampler while the command's stderr said ``Permission denied``
    in its first line. The tails go into the prompt."""
    run_dir = tmp_path / "run"
    args = {
        "commands": [
            {
                "cmd": "sh -c 'echo partial-out; echo boom-on-stderr >&2; exit 3'",
                "wall_clock_min": 0.1,
            }
        ]
    }
    calls = []

    def fake_triage(prompt: str) -> dict:
        calls.append(prompt)
        return {"verdict": "work_failure", "diagnosis": "seen"}

    deps = make_deps(repo, run_dir, repo, triage=fake_triage)

    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, make_group(args)))

    assert len(calls) == 1
    assert "boom-on-stderr" in calls[0]
    assert "partial-out" in calls[0]


def test_output_sha256_survives_the_merge_tearing_the_workspace_down(tmp_path, repo):
    """The merge removes the group's worktree (and its data-dir links) before
    the artifact is registered; hashing afterwards found no file, and
    r20260925-101742 g5 recorded ``sha256: {}`` beside full measurements."""
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
    expected = {}

    def merge_then_teardown(group: Group, wt: Path) -> str:
        expected["sha"] = hashlib.sha256((wt / "score.json").read_bytes()).hexdigest()
        (wt / "score.json").unlink()
        return git(wt, "rev-parse", "HEAD").strip()

    deps = make_deps(repo, run_dir, repo, merge_group=merge_then_teardown)

    state, _ = asyncio.run(_run(deps, make_group(args)))

    assert state == GroupState.COMPLETED
    entry = deps.artifacts.load().entries["g7"]
    assert entry.sha256 == {"score.json": expected["sha"]}


# ------------------------------------------ runner session, report, surprises
# r20260925-101742: a run group was invisible around the executor — no session
# in the manifest ("0 tokens across 0 session(s)", "Elapsed: n/a"), no report
# for the facts (items `unverified`, unit not landed), and a surprise aimed at
# it died undelivered.

TRUE_CMD = {"cmd": "true", "wall_clock_min": 1}


def _runner_sessions(deps):
    manifest = deps.store.load()
    entry = manifest.groups.get("g7")
    return [] if entry is None else [s for s in entry.sessions if s.role == SessionRole.RUNNER]


def test_completed_run_group_records_a_runner_session_with_start_and_end(tmp_path, repo):
    deps = make_deps(repo, tmp_path / "run", repo)
    state, _ = asyncio.run(_run(deps, make_group({"commands": [TRUE_CMD]}), generation=2))
    assert state == GroupState.COMPLETED
    sessions = _runner_sessions(deps)
    assert len(sessions) == 1
    (session,) = sessions
    assert session.session_id == "g7-run-a1"
    assert session.name == "rtest-g7-runner-g2"
    assert session.generation == 2
    assert session.started_at is not None and session.ended_at is not None
    assert session.ended_at >= session.started_at
    # The in-memory manifest the deps carry is the one persisted.
    assert deps.manifest.groups["g7"].sessions[0].ended_at == session.ended_at


def test_failed_run_group_still_closes_the_runner_session(tmp_path, repo):
    deps = make_deps(repo, tmp_path / "run", repo)
    with pytest.raises(GroupFailure):
        asyncio.run(_run(deps, make_group({"commands": [{"cmd": "false", "wall_clock_min": 1}]})))
    (session,) = _runner_sessions(deps)
    assert session.ended_at is not None


def test_completed_run_group_writes_a_synthetic_report_for_its_declared_commands(tmp_path, repo):
    verification = [
        VerificationItem(id="g7-1", description="The smoke passes. Run: `true` Pass: exit 0."),
        VerificationItem(
            id="g7-2", description="The sampler is fast. Run: `python3 bench.py` Pass: < 1s."
        ),
        VerificationItem(id="g7-3", description="The output exists."),
    ]
    deps = make_deps(repo, tmp_path / "run", repo)
    group = make_group({"commands": [TRUE_CMD]}, verification=verification)
    state, _ = asyncio.run(_run(deps, group))
    assert state == GroupState.COMPLETED
    assert (deps.store.paths.group_dir("g7") / "report-g0-r1.json").is_file()
    report = latest_report(deps.store.paths, "g7")
    assert report is not None
    assert report["status"] == "completed" and report["source"] == "run_recipe"
    assert report["summary"].startswith("run recipe: 1 command(s)")
    assert [r["item_id"] for r in report["verification_results"]] == ["g7-1"]
    (result,) = report["verification_results"]
    assert result["status"] == "pass"
    assert "run recipe attempt 1: `true` exit 0" in result["notes"]


def test_pending_surprises_are_consumed_at_start_logged_and_folded_into_the_summary(tmp_path, repo):
    deps = make_deps(repo, tmp_path / "run", repo)
    deps.board.mark(
        Surprise(kind="other", description="the fixture  changed\nshape", affected_groups=["g7"]),
        source_group="g2",
    )
    deps.board.mark(
        Surprise(kind="other", description="x" * 300, affected_groups=["g7"]), source_group="g3"
    )
    state, _ = asyncio.run(_run(deps, make_group({"commands": [TRUE_CMD]})))
    assert state == GroupState.COMPLETED
    assert deps.board.pending_for("g7") == []
    log = (deps.store.paths.run_dir / "logs" / "run.log").read_text()
    assert (
        "group g7: consumed surprise [other] (run recipe, no coder; noted in the artifact "
        "summary): the fixture changed shape"
    ) in log
    summary = deps.artifacts.load().entries["g7"].summary
    assert "; surprises consumed: [other] the fixture changed shape; [other] xxx" in summary
    assert "x" * 300 not in summary and "…" in summary
    report = latest_report(deps.store.paths, "g7")
    assert "surprises consumed" in report["summary"]
