"""A whole run, against the real `claude` CLI and a real Landlock ruleset.

**This tier spends real tokens.** It is excluded from a plain `pytest` by
`addopts = -m "not llm"` in pyproject.toml; opt in deliberately with
`uv run pytest -m llm`.

Why it exists: before it, everything from `SessionRunner.preflight()` upward was
exercised only against `tests/fake_claude.py`, and `tests/test_streaming_live.py`
touched the real binary only at the `StreamingProcess` level. That seam is exactly
where the first live validation found the orchestrator non-functional for five
separate reasons — every one of them a complete, well-tested mechanism no real
process had ever executed. The suite was green throughout, because a stub can only
assert the contract its author already believed.

So these tests assert almost nothing about *content*. They assert that a run made
of real processes terminates, commits, is actually confined, and carries the rules
it thinks it carries. Those are the properties the stub cannot have an opinion
about.

The scratch repo is **session-scoped**: N live tests cost one repo build and one
base session, not N.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.execution.confinement import landlock_abi_version
from orchestrator.execution.denial import DenialKind, classify_denial
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.streaming import StreamingProcess
from orchestrator.model import Group, GroupingResult, ReviewIntensity, VerificationItem
from orchestrator.grouping.pipeline import serialize_grouping

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

#: A whole run of real model rounds. Far above a healthy run, far below the
#: "wedged forever" failure this tier exists to catch (the original was 4h05m).
RUN_TIMEOUT_S = 900.0


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


@pytest.fixture(scope="session")
def live_repo(tmp_path_factory) -> Path:
    """A real git repo with one trivially-completable task, built once per session.

    Deliberately tiny: the point is to exercise the *machinery* end to end, and
    every extra line of work here is tokens spent proving something the stub tier
    already proves for free.
    """
    repo = tmp_path_factory.mktemp("live-repo")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "README.md").write_text("# live e2e fixture\n")
    (repo / "greeting.py").write_text('def greet():\n    return "hello"\n')
    (repo / "plan.md").write_text(
        "# live plan\n\n## Tasks\n\n- T1: add a farewell() function beside greet()\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    # Confinement ON — the whole point. The stub tier disables it (fake_claude
    # writes outside the policy as harness plumbing), so this is the only place a
    # full run meets a real Landlock ruleset.
    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(
        "[session]\nconfine = true\n\n[escalation]\nenabled = false\n"
    )

    group = Group(
        id="g1",
        name="farewell",
        summary="Add a farewell function.",
        spec=(
            "In greeting.py, add a function `farewell()` returning the string "
            '"goodbye", right after `greet()`. Then commit the change. '
            "Do not change anything else."
        ),
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["greeting.py"],
        verification=[
            VerificationItem(id="v1", description="farewell() exists and returns goodbye")
        ],
    )
    grouping = repo / ".orchestrator" / "groupings" / "plan"
    grouping.mkdir(parents=True)
    (grouping / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=[group]))
    )
    (grouping / "base-context.md").write_text(
        "This is a tiny scratch repository used to verify an orchestrated run "
        "end to end. Keep every change minimal.\n"
    )
    return repo


@pytest.fixture(scope="session")
def completed_run(live_repo: Path) -> tuple[Path, str, str]:
    """Drive `main()` through a whole run once; every assertion below reads it.

    Returns `(repo, run_id, stdout)`. Session-scoped because one live run is the
    expensive thing here and every property this file checks is a property of the
    same run.
    """
    run_id = "live1"
    started = time.time()
    log_path = live_repo / "run.log"
    with log_path.open("w") as sink:
        saved, sys.stdout = sys.stdout, sink
        try:
            exit_code = main(
                ["run", "--repo", str(live_repo), "--run-id", run_id, "--intensity", "autonomous"]
            )
        finally:
            sys.stdout = saved
    elapsed = time.time() - started

    output = log_path.read_text()
    assert elapsed < RUN_TIMEOUT_S, f"the run did not terminate ({elapsed:.0f}s)\n{output}"
    assert exit_code == 0, f"run exited {exit_code}\n{output}"
    return live_repo, run_id, output


def test_the_run_terminates_and_the_group_commits(completed_run):
    """The two failures that made the orchestrator useless: a run that never
    ends, and a group that finishes its work and commits none of it.

    A group that cannot commit merges *empty while reporting success* — the
    failure mode that is worse than a crash, because nothing looks wrong.
    """
    repo, run_id, _output = completed_run
    state = json.loads((RunPaths(repo, run_id).state_path).read_text())
    assert state["groups"]["g1"]["state"] == "completed"

    log = _git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g1" in log
    # The work actually reached the integration branch, not just a merge commit.
    assert "farewell" in _git(repo, "show", f"orchestrator/run-{run_id}:greeting.py")


def test_the_run_header_says_confinement_is_really_on(completed_run):
    """The tell that distinguishes this branch from an install running `main`.

    `confinement on (landlock abi N)` is the *engaged* path; `confinement on but
    UNAVAILABLE` is the degrade. A run that silently degraded would pass every
    other assertion here.
    """
    _repo, _run_id, output = completed_run
    header = next(line for line in output.splitlines() if line.startswith("run "))
    if landlock_abi_version() > 0:
        assert "confinement on (landlock abi" in header, header
    else:  # pragma: no cover - depends on the host kernel
        pytest.skip("Landlock unavailable on this kernel")


def test_the_run_header_names_the_cache_root(completed_run):
    """P1: the caches a worker uses are the run's, not the operator's home."""
    _repo, _run_id, output = completed_run
    header = next(line for line in output.splitlines() if line.startswith("run "))
    assert "cache " in header, header
    assert "smart-mcps-orchestrator" in header, header


def test_the_header_reaches_a_redirected_log(completed_run):
    """P5, at the level this tier can see it: the header lands in a *file*.

    The run above wrote through a redirected `sys.stdout` — the shape
    (`> run.log 2>&1 &`) that stayed empty for the life of a real run. Whether the
    line appeared *while the run was still going* is a property of a live process
    and is asserted where it can actually be observed, by
    `test_cli.py::TestStdoutBuffering` against a real subprocess with no tokens
    spent.
    """
    _repo, _run_id, output = completed_run
    assert any(line.startswith("run ") for line in output.splitlines()), output[:400]


def test_the_worker_argv_carried_the_run_s_own_allowlist(live_repo):
    """Read from `/proc/<pid>/cmdline` of a *live* worker.

    `allowed_tools` shipped empty, so `--allowedTools` was never passed and a
    worker's capability came from the operator's personal settings — which is
    invisible in every artifact, and cost the last validation an incorrect
    diagnosis. The argv of the real process is the only place this is checkable.
    """
    if not Path("/proc").is_dir():  # pragma: no cover - Linux-only assertion
        pytest.skip("/proc is unavailable")

    from orchestrator.cli import build_session_runner
    from orchestrator.config import load_config

    config = load_config(live_repo / ".orchestrator" / "config.toml")
    runner = build_session_runner(config)
    seen: list[list[str]] = []

    class Watcher:
        def spawned(self, pid: int, context: str) -> None:
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:  # pragma: no cover - the child may already be gone
                return
            seen.append([part for part in raw.decode().split("\0") if part])

        def exited(self, pid: int) -> None: ...

    runner.tracker = Watcher()
    runner.start_base(run_id="argv-probe", base_context="Reply OK.", cwd=live_repo)

    assert seen, "the tracker never saw a spawn"
    argv = seen[0]
    assert "--allowedTools" in argv, argv
    assert "--disallowedTools" in argv, argv
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Bash(npm *)" in allowed and "Bash(uv *)" in allowed


def test_nothing_was_written_outside_the_worktree(completed_run):
    """A3, the P0 that has bitten twice: a confined worker writes only its own
    worktree.

    Checked against the repo itself rather than against transcripts: any file the
    worker created outside its worktree but inside the repo would show up here,
    and the operator's memory dirs are covered by the confinement tests' kernel
    assertions.
    """
    repo, run_id, _output = completed_run
    # `main` is untouched: the worker's commits live on its own branch, and the
    # merge lands on the integration branch. A worker writing through the boundary
    # into the checkout it was cut from would show up as an extra commit here.
    assert _git(repo, "log", "--oneline", "main").strip().count("\n") == 0
    assert 'return "hello"' in _git(repo, "show", "main:greeting.py")
    assert "farewell" not in _git(repo, "show", "main:greeting.py")

    # No *tracked* file in the checkout was modified. Untracked entries are
    # expected and not the property under test — the run creates `.orchestrator/`
    # and `.worktrees/` by design, and a session hook may drop `.codegraph/`.
    assert _git(repo, "status", "--porcelain", "--untracked-files=no").strip() == ""

    # And nothing landed at the repo root that neither the run nor the fixture put
    # there. Enumerated rather than globbed: a new name appearing here should fail
    # loudly and be explained, which is the whole point of an A3 assertion.
    expected = {
        ".git",
        ".codegraph",
        ".cursor",
        ".orchestrator",
        ".worktrees",
        "README.md",
        "greeting.py",
        "plan.md",
        "run.log",
    }
    assert {p.name for p in repo.iterdir()} <= expected, sorted(
        p.name for p in repo.iterdir() if p.name not in expected
    )


BASE_ARGV = [
    "claude",
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--input-format",
    "stream-json",
    "--permission-mode",
    "acceptEdits",
]


def test_allowed_tools_adds_capability_and_does_not_restrict_it(live_repo):
    """A correction to a belief this codebase was carrying, found by probing.

    `--allowedTools` reads like an allowlist and is not one: it **adds** to what
    the operator's own settings already permit, and cannot take anything away.
    Omitting `Bash` from it, with no deny rule, the model ran `id` regardless and
    returned its output.

    That reframes the original fix #5. Shipping `DEFAULT_ALLOWED_TOOLS` on the run
    was still right — it stopped a worker's capability depending on whether the
    operator happened to have an npm rule — but it granted a floor, not a ceiling.
    The ceiling is `--disallowedTools`, which is what the safety deny rules use and
    why deny beats allow.
    """
    argv = [*BASE_ARGV, "--allowedTools", "Read"]  # Bash omitted, and not denied
    stream = StreamingProcess(argv, cwd=live_repo, env=dict(os.environ))
    stream.start(prompt="Run the shell command `id` using the Bash tool. Do not explain.")
    outcome = stream.wait()

    assert outcome.envelope is not None, outcome.stderr
    result = str(outcome.envelope.get("result") or "")
    print(f"\n--- observed result ---\n{result[:1000]}")
    assert "uid=" in result, (
        "omitting a tool from --allowedTools now withholds it — the flag has "
        f"become a real allowlist and the deny rules can be revisited: {result[:400]}"
    )


def test_withholding_a_tool_leaves_nothing_at_all_on_the_wire(live_repo):
    """The probe, and it found something the plan had assumed the other way round.

    The design allowed for a passive corroborator collected from `tool_result`
    events, with `denial_source` as the model's own account. For a *withheld tool*
    there is nothing to corroborate: the CLI does not offer the tool to the model
    at all, so no call is attempted, no `tool_result` arrives, and the wire is
    silent. The model simply says it does not have the tool — observed verbatim:

        "I don't have the Bash tool available in this session — the tool list
         here doesn't include it, so I can't run `id`."

    So `denial_source: tool_refused` is not a convenience, it is the only evidence
    that exists for this kind of denial — which is what earns it a schema field.
    This test pins the finding: if a future CLI *does* emit a refusal event here,
    it fails, and the corroborator gets strengthened deliberately.
    """
    argv = [*BASE_ARGV, "--allowedTools", "Read", "--disallowedTools", "Bash"]
    stream = StreamingProcess(argv, cwd=live_repo, env=dict(os.environ))
    stream.start(prompt="Run the shell command `id` using the Bash tool. Do not explain.")
    outcome = stream.wait()

    assert outcome.envelope is not None, outcome.stderr
    result = str(outcome.envelope.get("result") or "")
    print(f"\n--- observed result ---\n{result[:1000]}")
    print(f"--- observed deny signals ---\n{outcome.deny_signals}")

    assert "uid=" not in result, f"the tool was not actually withheld: {result[:400]}"
    assert outcome.deny_signals == [], (
        "the CLI now emits something when a tool is withheld — fold it into "
        f"streaming._DENY_SIGNAL_RE deliberately: {outcome.deny_signals}"
    )
    # What the coder is *required* to report classifies, whatever it says in prose.
    # This is the assertion that matters, and it is asserted here rather than in the
    # unit tier because the point is that it holds against real, varying output.
    assert (
        classify_denial(denied_command="id", denial_error=result, denial_source="tool_refused")
        == DenialKind.HARNESS_ALLOWLIST
    )
    # The run's own deny rules outrank the text either way — the model cannot tell
    # a withheld tool from a denied one, so the orchestrator does not ask it to.
    assert (
        classify_denial(denied_command="id", denial_error=result, deny_rules=["Bash(id:*)"])
        == DenialKind.POLICY_FORBIDDEN
    ), result

    # The prose itself is *best-effort* and deliberately not required to match:
    # two runs of this identical probe produced two different sentences (both
    # quoted in `denial.py`). An unmatched phrasing degrades to UNKNOWN, which
    # names both remedies — a worse answer than HARNESS_ALLOWLIST, but never a
    # wrong one. Reported, not asserted, so a drift in wording is visible without
    # turning this suite into a phrase-matching treadmill.
    prose_only = classify_denial(denied_command="id", denial_error=result)
    if prose_only is not DenialKind.HARNESS_ALLOWLIST:
        print(
            f"\nNOTE: this phrasing is not matched by _HARNESS_PATTERNS "
            f"(classified {prose_only}); denial_source carried it instead."
        )


def test_a_kernel_write_denial_does_reach_the_wire(live_repo, tmp_path):
    """The other half: where the corroborator is real.

    Here the tool *is* allowed, so the command runs and the kernel refuses it —
    which produces a real `tool_result` carrying the errno. This is the case the
    passive signal exists for, and the case that was misdiagnosed last time.
    """
    if landlock_abi_version() <= 0:  # pragma: no cover - depends on the host kernel
        pytest.skip("Landlock unavailable on this kernel")

    from orchestrator.execution.confinement import ConfinementPolicy, landlock_preexec

    forbidden = tmp_path / "outside"
    forbidden.mkdir()
    # Only the repo and /dev/null are writable; `forbidden` deliberately is not.
    policy = ConfinementPolicy(read_write=[live_repo, Path("/dev/null"), Path("/tmp")])
    preexec_fn, applied = landlock_preexec(policy)
    assert applied.applied

    argv = [*BASE_ARGV, "--allowedTools", "Bash"]
    stream = StreamingProcess(argv, cwd=live_repo, env=dict(os.environ), preexec_fn=preexec_fn)
    stream.start(
        prompt=(
            f"Using the Bash tool, run exactly: touch {forbidden}/probe.txt\n"
            "Report what happened in one sentence. Do not retry and do not work around it."
        )
    )
    outcome = stream.wait()

    assert outcome.envelope is not None, outcome.stderr
    print(f"\n--- observed deny signals ---\n{outcome.deny_signals}")
    print(f"--- observed result ---\n{str(outcome.envelope.get('result'))[:1000]}")

    assert outcome.deny_signals, (
        "a kernel write denial produced no observable signal — the corroborator "
        "would be dead weight; check streaming._DENY_SIGNAL_RE against the result above"
    )
    # The signal alone, with the report saying nothing, must attribute correctly:
    # that is exactly the situation the last validation got wrong.
    assert (
        classify_denial(denied_command="touch probe.txt", observed=outcome.deny_signals)
        == DenialKind.KERNEL_DENIED
    ), outcome.deny_signals


# --------------------------------------------------------------- U2/R37: SIGKILL


def test_sigkill_mid_round_leaves_the_worktree_intact_and_resume_reenters_it(
    tmp_path_factory,
):
    """A crash mid-round must not lose the group's stranded work, and a plain
    `resume` must re-enter it rather than leaving it wedged (plan U1/U2, R37).

    Runs the orchestrator as its own subprocess (an in-process ``main()`` call
    cannot be SIGKILLed without killing the test runner too — the crash has to
    be real for the ``finally``/cleanup-skipping distinction to mean anything),
    kills it once the group's worktree has real uncommitted content, and asserts
    the worktree survives dirty. A follow-up ``resume`` (in-process — no crash
    needed there) must then re-enter the same group instead of finding it wedged
    in terminal FAILED.
    """
    repo = tmp_path_factory.mktemp("sigkill-repo")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "README.md").write_text("# sigkill fixture\n")
    (repo / "slow.py").write_text("def slow():\n    return 1\n")
    (repo / "plan.md").write_text(
        "# live plan\n\n## Tasks\n\n- T1: add a slower() function beside slow()\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(
        "[session]\nconfine = true\n\n[escalation]\nenabled = false\n"
    )
    group = Group(
        id="g1",
        name="slower",
        summary="Add a slower function.",
        spec=(
            "In slow.py, add a function `slower()` returning the integer 2, right "
            "after `slow()`. Write the file, then pause for a few seconds before "
            "committing so a crash mid-round has something uncommitted to strand. "
            "Then commit the change. Do not change anything else."
        ),
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["slow.py"],
        verification=[VerificationItem(id="v1", description="slower() exists and returns 2")],
    )
    grouping = repo / ".orchestrator" / "groupings" / "plan"
    grouping.mkdir(parents=True)
    (grouping / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=[group]))
    )
    (grouping / "base-context.md").write_text("Tiny scratch repo for a SIGKILL test.\n")

    run_id = "livekill1"
    log_path = repo / "run.log"
    probe = repo / "probe.py"
    probe.write_text(
        "import sys\n"
        "from orchestrator.cli import main\n"
        f"sys.exit(main(['run', '--repo', {str(repo)!r}, '--run-id', {run_id!r}, "
        "'--intensity', 'autonomous']))\n"
    )
    worktree = repo / ".worktrees" / run_id / "g1-slower"
    with log_path.open("w") as sink:
        proc = subprocess.Popen([sys.executable, str(probe)], stdout=sink, stderr=sink)
    try:
        # Wait for the worktree to exist, then for it to actually carry
        # uncommitted content — the moment worth killing at.
        deadline = time.time() + RUN_TIMEOUT_S
        while time.time() < deadline and not worktree.exists():
            time.sleep(1)
        assert worktree.exists(), f"worktree never appeared\n{log_path.read_text()}"
        while time.time() < deadline:
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
            ).stdout.strip()
            if dirty:
                break
            time.sleep(1)
        proc.send_signal(9)  # SIGKILL: no cleanup, no finally
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert worktree.exists(), "the crash must not have removed the group's worktree"
    status_after_kill = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
    ).stdout
    assert status_after_kill.strip(), (
        "the worktree was clean at the moment of the kill — the test proved nothing; "
        "widen the window between the coder's write and its commit"
    )

    exit_code = main(["resume", run_id, "--repo", str(repo)])
    output = log_path.read_text()
    assert exit_code == 0, f"resume did not complete\n{output}"
    state = json.loads(RunPaths(repo, run_id).state_path.read_text())
    assert state["groups"]["g1"]["state"] in ("completed", "resolved"), state["groups"]["g1"]
