"""U2 tests: kernel-enforced worker confinement via Landlock (plan Phase B).

The write-denial tests spawn real subprocesses under the built preexec_fn —
there is no way to observe Landlock's effect without exercising the actual
syscalls (mocking them would just test the mock). They are skipped outright
on a kernel without Landlock, since there is nothing to observe; the degrade
path itself is covered separately by monkeypatching the ABI probe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.execution.confinement import (
    ConfinementPolicy,
    UNAVAILABLE_WARNING,
    build_policy,
    landlock_abi_version,
    landlock_preexec,
    probe_claude_runtime_dirs,
    tool_cache_dirs,
    user_cache_dir,
    warn_once,
)
from orchestrator.execution.sessions import SessionRunner

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"

landlock_unavailable = pytest.mark.skipif(
    landlock_abi_version() <= 0, reason="Landlock is unavailable on this kernel"
)


@pytest.fixture
def confined(tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    claude_home = tmp_path / "claude"
    (claude_home / "projects" / "other-slug" / "memory").mkdir(parents=True)
    (claude_home / "shell-snapshots").mkdir(parents=True)
    (claude_home / "shell-snapshots" / "snap.sh").write_text("echo hi\n")
    # system_paths=[] because pytest's tmp_path lives under /tmp, which the real
    # policy allows: with the defaults, the "denied" paths below would be
    # writable via the /tmp rule and these assertions would prove nothing.
    policy = build_policy(
        worktree=worktree, claude_home=claude_home, project_slug="my-slug", system_paths=[]
    )
    preexec_fn, result = landlock_preexec(policy)
    assert result.applied
    return worktree, claude_home, preexec_fn


@pytest.fixture
def confined_with_system(tmp_path: Path):
    """The production policy, system paths included — for asserting a confined
    worker can still run its tools."""
    worktree = tmp_path / "worktree-sys"
    worktree.mkdir()
    claude_home = tmp_path / "claude-sys"
    (claude_home / "shell-snapshots").mkdir(parents=True)
    policy = build_policy(worktree=worktree, claude_home=claude_home, project_slug="my-slug")
    preexec_fn, result = landlock_preexec(policy)
    assert result.applied
    return worktree, claude_home, preexec_fn


def _run(preexec_fn, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], preexec_fn=preexec_fn, capture_output=True, text=True
    )


@landlock_unavailable
def test_confined_subprocess_can_write_the_user_tool_cache(confined_with_system, monkeypatch):
    """`uv`, `pip`, `npm` and friends all write the XDG cache, so a worker that
    cannot is a worker that cannot run its own verification command.

    Observed on run r20260812-202855: g1 and g8 both reported `permission_denied`
    before reading a single source file, with `Failed to initialize cache at
    ~/.cache/uv … Permission denied (os error 13)`.
    """
    _worktree, _claude_home, preexec_fn = confined_with_system
    caches = tool_cache_dirs()
    assert caches, "no tool cache dirs resolved"
    # npm does not use XDG — its cache is ~/.npm — so asserting only the XDG root
    # would still have let `npm install` fail, which is exactly what happened.
    assert any(c == user_cache_dir() for c in caches)
    for cache in caches:
        probe = cache / "smart-mcps-confinement-probe"
        result = _run(preexec_fn, f"echo ok > {probe}")
        try:
            assert result.returncode == 0, f"{cache} unwritable: {result.stderr}"
            assert probe.read_text() == "ok\n"
        finally:
            probe.unlink(missing_ok=True)


@landlock_unavailable
def test_confined_worker_can_commit_inside_a_linked_worktree(tmp_path: Path):
    """The end-to-end thing confinement exists to permit: a worker commits its work.

    A linked worktree's `.git` is a *file* pointing at
    `<repo>/.git/worktrees/<name>`, and objects/refs live in `<repo>/.git` — both
    outside the worktree. Granting only the worktree left `git commit` impossible,
    so on run r20260812-202855 g1 finished U1+U2 with 279 tests passing and could
    not commit a line of it. A group that cannot commit merges empty while
    reporting success, which is the failure mode E exists to catch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a, cwd=repo: subprocess.run(  # noqa: E731
        a, cwd=cwd, capture_output=True, text=True, check=True
    )
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "seed")

    worktree = tmp_path / "wt"
    run("git", "worktree", "add", "-q", "-b", "feature", str(worktree))
    assert (worktree / ".git").is_file(), "expected a linked worktree, not a checkout"

    # `system_paths` is pared to /dev/null (which git opens unconditionally)
    # rather than left at its default: tmp_path lives under /tmp, and the default
    # /tmp rule would make this whole repo writable for reasons unrelated to the
    # git-dir grant under test — the assertion would pass even with the bug.
    commit = (
        f"cd {worktree} && git -c user.email=t@e.com -c user.name=t add -A "
        f"&& git -c user.email=t@e.com -c user.name=t commit -qm 'work'"
    )
    (worktree / "work.txt").write_text("done\n")

    # Without the git dirs — the policy as it shipped — the commit is impossible.
    worktree_only = ConfinementPolicy(read_write=[worktree, Path("/dev/null")])
    denied_fn, denied_result = landlock_preexec(worktree_only)
    assert denied_result.applied
    assert _run(denied_fn, commit).returncode != 0

    policy = build_policy(
        worktree=worktree,
        claude_home=tmp_path / "claude",
        system_paths=[Path("/dev/null")],
    )
    preexec_fn, result = landlock_preexec(policy)
    assert result.applied
    committed = _run(preexec_fn, commit)
    assert committed.returncode == 0, committed.stderr

    log = subprocess.run(
        ["git", "-C", str(worktree), "log", "--oneline", "-1"],
        capture_output=True,
        text=True,
    )
    assert "work" in log.stdout


@landlock_unavailable
def test_confined_subprocess_cannot_write_another_slugs_memory_dir(confined):
    worktree, claude_home, preexec_fn = confined
    other_memory = claude_home / "projects" / "other-slug" / "memory"
    result = _run(preexec_fn, f"echo evil > {other_memory}/note.md")
    assert result.returncode != 0
    assert "Permission denied" in result.stderr


@landlock_unavailable
def test_confined_subprocess_can_write_its_worktree_and_own_project_dir(confined):
    worktree, claude_home, preexec_fn = confined
    own_dir = claude_home / "projects" / "my-slug"
    assert _run(preexec_fn, f"echo ok > {worktree}/file.txt").returncode == 0
    assert (worktree / "file.txt").read_text() == "ok\n"
    assert _run(preexec_fn, f"echo ok > {own_dir}/note.txt").returncode == 0
    assert (own_dir / "note.txt").read_text() == "ok\n"


@landlock_unavailable
def test_confinement_boundary_survives_an_intermediate_shell(confined):
    """bash -c ... must be denied too — proof the boundary is inherited by the
    subprocess tree, not a tool-level check any shell command bypasses."""
    worktree, claude_home, preexec_fn = confined
    other_memory = claude_home / "projects" / "other-slug" / "memory"
    denied = _run(preexec_fn, f"bash -c 'echo x > {other_memory}/nested.md'")
    assert denied.returncode != 0
    assert not (other_memory / "nested.md").exists()


@landlock_unavailable
def test_confined_subprocess_can_still_run_git(confined_with_system):
    """The policy has to leave a worker able to do its job.

    `/dev/null` was unwritable as shipped, and git opens it unconditionally —
    `git --version` exited 128, so a confined worker could not have committed
    anything. Caught only by running a real confined process; the rule looked
    present because `landlock_add_rule` fails with EINVAL on a non-directory
    when the mask carries directory rights, and that error is ignored.
    """
    _, _, preexec_fn = confined_with_system
    assert _run(preexec_fn, "git --version").returncode == 0
    assert _run(preexec_fn, "echo x > /dev/null").returncode == 0


@landlock_unavailable
def test_confined_subprocess_can_write_claude_runtime_scratch(confined):
    """`claude` writes `shell-snapshots/`, `sessions/`, `file-history/` and more
    as it runs. Those were read-only as shipped, which hardens nothing — the
    asset under guard is `projects/<other-slug>/memory` — and breaks the worker."""
    _, claude_home, preexec_fn = confined
    snapshot = claude_home / "shell-snapshots" / "written-at-runtime.sh"
    assert _run(preexec_fn, f"echo ok > {snapshot}").returncode == 0
    assert snapshot.read_text() == "ok\n"


@landlock_unavailable
def test_confined_subprocess_cannot_write_outside_its_worktree(confined, tmp_path):
    """The operator's own checkout is outside the boundary too, not just memory."""
    outside = tmp_path / "operator-checkout"
    outside.mkdir()
    assert _run(preexec_fn := confined[2], f"echo x > {outside}/stolen.txt").returncode != 0
    assert not (outside / "stolen.txt").exists()
    assert preexec_fn is not None


def test_landlock_degrades_with_one_warning_and_the_round_still_runs(tmp_path, capsys, monkeypatch):
    import orchestrator.execution.confinement as confinement_mod

    monkeypatch.setattr(confinement_mod, "landlock_abi_version", lambda: 0)
    policy = build_policy(worktree=tmp_path, claude_home=tmp_path / "claude")
    preexec_fn, result = landlock_preexec(policy)
    assert preexec_fn is None
    assert not result.applied
    assert result.warning == UNAVAILABLE_WARNING

    already_warned = warn_once(result, already_warned=False)
    assert already_warned is True
    assert "Landlock is unavailable" in capsys.readouterr().err

    # A second round must not repeat the warning.
    already_warned = warn_once(result, already_warned=already_warned)
    assert capsys.readouterr().err == ""

    # And the round itself must still run to completion (never fails a group).
    env = {"FAKE_CLAUDE_HOME": str(tmp_path / "fake-home")}
    (tmp_path / "fake-home" / "sessions").mkdir(parents=True)
    runner = SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env=env,
        transcript_root=tmp_path / "fake-home" / "projects",
        confine=True,
    )
    with (tmp_path / "fake-home" / "script.jsonl").open("w") as fh:
        fh.write(json.dumps({"result": "ok"}) + "\n")
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.text == "ok"


def test_runtime_allowlist_is_derived_from_an_executed_probe(tmp_path):
    claude_home = tmp_path / "claude"
    for name in ("shell-snapshots", "cache", "plugins", "sessions"):
        (claude_home / name).mkdir(parents=True)
    (claude_home / "projects" / "some-slug").mkdir(parents=True)

    probed = probe_claude_runtime_dirs(claude_home)
    assert probed  # non-empty
    assert "shell-snapshots" in probed
    assert "projects" not in probed  # handled separately, own-slug only


def test_disallowed_tools_and_settings_appear_in_argv_when_configured(tmp_path):
    env = {"FAKE_CLAUDE_HOME": str(tmp_path / "fake-home")}
    (tmp_path / "fake-home" / "sessions").mkdir(parents=True)
    runner = SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env=env,
        transcript_root=tmp_path / "fake-home" / "projects",
        disallowed_tools=["Bash(git stash:*)"],
        settings='{"foo": "bar"}',
        safety_deny=False,
    )
    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    calls_path = tmp_path / "fake-home" / "calls.jsonl"
    call = json.loads(calls_path.read_text().splitlines()[0])
    argv = call["argv"]
    assert argv[argv.index("--disallowedTools") + 1] == "Bash(git stash:*)"
    assert argv[argv.index("--settings") + 1] == '{"foo": "bar"}'

    plain_env = {"FAKE_CLAUDE_HOME": str(tmp_path / "fake-home-2")}
    (tmp_path / "fake-home-2" / "sessions").mkdir(parents=True)
    plain_runner = SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env=plain_env,
        transcript_root=tmp_path / "fake-home-2" / "projects",
    )
    plain_runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    plain_call = json.loads((tmp_path / "fake-home-2" / "calls.jsonl").read_text().splitlines()[0])
    assert "--settings" not in plain_call["argv"]
    # An unconfigured runner is *not* rule-free: the safety deny rules are always
    # present, and only `safety_deny=False` removes the flag entirely.
    plain_rules = plain_call["argv"][plain_call["argv"].index("--disallowedTools") + 1]
    assert "memory" in plain_rules
    assert "Bash(git stash:*)" in plain_rules


def test_streaming_argv_carries_verbose(tmp_path):
    """`--print --output-format=stream-json` without `--verbose` is rejected by the
    real CLI with exit 1, so no worker could ever spawn. Observed live on run
    r20260812-161122; the stub now enforces the same pairing, and this pins the
    flag itself so a refactor of `_call` cannot drop it silently again."""
    env = {"FAKE_CLAUDE_HOME": str(tmp_path / "fake-home")}
    (tmp_path / "fake-home" / "sessions").mkdir(parents=True)
    runner = SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env=env,
        transcript_root=tmp_path / "fake-home" / "projects",
    )
    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    argv = json.loads((tmp_path / "fake-home" / "calls.jsonl").read_text().splitlines()[0])["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_safety_deny_rules_are_added_without_being_asked_for(tmp_path):
    """A runner nobody configured still denies the two things that have actually
    caused damage: repo-global git mutators, and writes to operator memory."""
    runner = SessionRunner(transcript_root=tmp_path / "home" / "projects")
    rules = runner.effective_disallowed_tools()

    memory_rules = [r for r in rules if "memory" in r]
    assert memory_rules, "operator memory must be denied by default"
    for tool in ("Edit", "Write", "MultiEdit"):
        assert any(r.startswith(f"{tool}(") for r in memory_rules)
    # Every project's memory, not just the worker's own slug: the observed
    # incident had a worker in one project write into another project's memory.
    assert all("/**/memory/**" in r for r in memory_rules)
    assert any("git stash" in r for r in rules)

    assert (
        SessionRunner(
            transcript_root=tmp_path / "home" / "projects", safety_deny=False
        ).effective_disallowed_tools()
        == []
    )


def test_configured_rules_survive_and_are_not_duplicated(tmp_path):
    runner = SessionRunner(
        transcript_root=tmp_path / "home" / "projects",
        disallowed_tools=["Bash(git stash:*)", "Bash(rm:*)"],
    )
    rules = runner.effective_disallowed_tools()
    assert rules[:2] == ["Bash(git stash:*)", "Bash(rm:*)"]  # operator rules keep precedence
    assert len(rules) == len(set(rules))
    assert rules.count("Bash(git stash:*)") == 1  # also produced by the git deny list


def test_cli_built_runner_is_confined_and_carries_safety_rules(tmp_path, monkeypatch):
    """The regression that let the whole boundary sit dead for a release.

    Landlock, the git deny list and `--settings` were all built and unit-tested,
    but `cli.py` constructed `SessionRunner` without passing any of them, so no
    real run was ever confined. Every mechanism test passed throughout. This
    asserts the *wiring*: what the CLI actually builds from a default config.
    """
    from orchestrator.cli import build_session_runner
    from orchestrator.config import OrchestratorConfig

    config = OrchestratorConfig()
    assert config.session.confine is True, "confinement must be on by default"

    runner = build_session_runner(config)
    assert runner.confine is True
    assert any("memory" in r for r in runner.effective_disallowed_tools())
