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
    UNAVAILABLE_WARNING,
    build_policy,
    landlock_abi_version,
    landlock_preexec,
    probe_claude_runtime_dirs,
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
    policy = build_policy(worktree=worktree, claude_home=claude_home, project_slug="my-slug")
    preexec_fn, result = landlock_preexec(policy)
    assert result.applied
    return worktree, claude_home, preexec_fn


def _run(preexec_fn, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script], preexec_fn=preexec_fn, capture_output=True, text=True
    )


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
    assert "--disallowedTools" not in plain_call["argv"]
    assert "--settings" not in plain_call["argv"]
