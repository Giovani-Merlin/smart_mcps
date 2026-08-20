"""U8/U9 tests: push, draft PR, and teardown of exactly what is provably merged.

The push step is stubbed in every test except the two dedicated to it: this
sandbox denies the hardlink git's local-transport push relies on to finalize
objects (`link()` returns EXDEV even within one filesystem, verified against a
push between two freshly created directories with no orchestrator code
involved at all) — a real GitHub remote uses smart-HTTP and never touches that
path, so this is a property of the sandbox, not of `_push_integration_branch`.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

import orchestrator.execution.finish as finish_module
from orchestrator.execution.finish import (
    FinishError,
    _delete_branch_if_merged,
    finish_run,
    run_is_finishable,
)
from orchestrator.execution.manifest import ManifestStore, RunPaths, atomic_write_text
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.execution.worktrees import create_worktree, group_branch, worktree_path
from orchestrator.model import (
    Group,
    GroupingResult,
    GroupManifestEntry,
    ReviewIntensity,
    RunManifest,
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result.stdout


def make_group(gid: str) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture(autouse=True)
def no_push(request, monkeypatch):
    """Stub the push step for every test except the ones named for it — see the
    module docstring for why a literal `git push` cannot succeed here."""
    if (
        "pushes_the_integration_branch" in request.node.name
        or "push_integration_branch" in request.node.name
    ):
        yield
        return
    monkeypatch.setattr(finish_module, "_push_integration_branch", lambda repo_root, run_id: None)
    yield


def add_origin(repo: Path, *, github_url: bool) -> None:
    url = "git@github.com:org/repo.git" if github_url else "/tmp/some/local/remote.git"
    git(repo, "remote", "add", "origin", url)


def write_fake_gh(bin_dir: Path, *, body_capture: Path | None = None) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    body_line = f'cat > "{body_capture}"' if body_capture is not None else "cat > /dev/null"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "auth" ]; then exit 0; fi\n'
        'if [ "$1" = "pr" ] && [ "$2" = "create" ]; then\n'
        f"  {body_line}\n"
        '  echo "https://github.com/org/repo/pull/1"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def setup_run(
    repo: Path,
    run_id: str,
    groups: list[Group],
    *,
    launch_branch: str | None = "main",
) -> tuple[RunPaths, IntegrationMerger]:
    paths = RunPaths(repo, run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    grouping = GroupingResult(plan_path="plan.md", groups=groups)
    atomic_write_text(paths.groups_path, grouping.model_dump_json())
    merger = IntegrationMerger(repo, run_id)
    merger.ensure()

    manifest = RunManifest(
        run_id=run_id,
        plan_path="plan.md",
        base_session_id="base-1",
        launch_branch=launch_branch,
        groups={
            g.id: GroupManifestEntry(group_id=g.id, group_name=g.name, summary=f"did {g.id}")
            for g in groups
        },
    )
    ManifestStore(paths).save(manifest)
    return paths, merger


def merge_group_cleanly(repo: Path, run_id: str, merger: IntegrationMerger, group: Group) -> Path:
    wt = create_worktree(
        repo,
        run_id=run_id,
        group_id=group.id,
        name=group.name,
        branch=group_branch(run_id, group.id),
        start_point=merger.tip(),
    )
    (wt / f"{group.id}.txt").write_text("work\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", f"{group.id} work")
    merger.merge_group(group, wt)
    return wt


def write_state(paths: RunPaths, entries: dict[str, GroupRunState]) -> None:
    state = RunState(run_id=paths.run_id, groups=entries)
    atomic_write_text(paths.state_path, state.model_dump_json(indent=2) + "\n")


# ------------------------------------------------------------------ push


def test_push_integration_branch_invokes_git_push_and_raises_on_failure(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    class Ok:
        returncode = 0
        stderr = ""

    def fake_run_ok(cmd, **kwargs):
        calls.append(cmd)
        return Ok()

    monkeypatch.setattr(finish_module.subprocess, "run", fake_run_ok)
    finish_module._push_integration_branch(tmp_path, "r1")
    assert calls[0][:2] == ["git", "push"]
    assert calls[0][-1] == "orchestrator/run-r1:orchestrator/run-r1"

    class Fail:
        returncode = 1
        stderr = "remote rejected"

    monkeypatch.setattr(finish_module.subprocess, "run", lambda cmd, **kwargs: Fail())
    with pytest.raises(FinishError, match="remote rejected"):
        finish_module._push_integration_branch(tmp_path, "r1")


def test_finish_pushes_the_integration_branch(repo, tmp_path, monkeypatch):
    run_id = "r1"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})

    pushed: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        finish_module,
        "_push_integration_branch",
        lambda repo_root, run_id: pushed.append((repo_root, run_id)),
    )

    finish_run(repo, run_id, announce=lambda _m: None)
    assert pushed == [(repo, run_id)]


# ------------------------------------------------------------------- PR


def test_finish_opens_a_draft_pr_against_the_launch_branch(repo, tmp_path, monkeypatch):
    run_id = "r2"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch="main")
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=True)

    bin_dir = tmp_path / "fakebin"
    body_capture = tmp_path / "pr-body.txt"
    write_fake_gh(bin_dir, body_capture=body_capture)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = finish_run(repo, run_id, announce=lambda _m: None)
    assert result.pr_url == "https://github.com/org/repo/pull/1"
    assert result.pr_skip_reason is None


def test_pr_body_lists_groups_state_summary_sessions_and_unmerged(repo, tmp_path, monkeypatch):
    run_id = "r3"
    merged = make_group("g1")
    unmerged = make_group("g2")
    paths, merger = setup_run(repo, run_id, [merged, unmerged], launch_branch="main")
    merge_group_cleanly(repo, run_id, merger, merged)
    # g2 never merges — its branch just exists, diverged from the tip.
    create_worktree(
        repo,
        run_id=run_id,
        group_id=unmerged.id,
        name=unmerged.name,
        branch=group_branch(run_id, unmerged.id),
        start_point=merger.tip(),
    )
    write_state(
        paths,
        {
            merged.id: GroupRunState(state=GroupState.COMPLETED),
            unmerged.id: GroupRunState(state=GroupState.FAILED, failure="boom"),
        },
    )
    add_origin(repo, github_url=True)
    bin_dir = tmp_path / "fakebin"
    body_capture = tmp_path / "pr-body.txt"
    write_fake_gh(bin_dir, body_capture=body_capture)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    finish_run(repo, run_id, announce=lambda _m: None)

    body = body_capture.read_text()
    assert "g1" in body and "did g1" in body and "completed" in body
    assert "g2" in body and "did g2" in body and "failed" in body
    assert "Unmerged groups:" in body and "g2" in body.split("Unmerged groups:")[1]


def test_detached_head_run_still_pushes_and_skips_pr(repo, tmp_path, monkeypatch):
    run_id = "r4"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=True)  # even if gh were reachable

    pushed = []
    monkeypatch.setattr(
        finish_module,
        "_push_integration_branch",
        lambda repo_root, run_id: pushed.append(run_id),
    )

    result = finish_run(repo, run_id, announce=lambda _m: None)
    assert result.pr_url is None
    assert "detached HEAD" in result.pr_skip_reason
    assert pushed == [run_id]


def test_gh_absent_never_blocks_and_reports_the_reason(repo, tmp_path, monkeypatch):
    run_id = "r5"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch="main")
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=True)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # no fake gh anywhere on PATH

    announced = []
    result = finish_run(repo, run_id, announce=announced.append)

    assert result.pr_url is None
    branch = finish_module.integration_branch(run_id)
    tip = git(repo, "rev-parse", branch).strip()
    expected = f"integration branch {branch} is ready at {tip}; could not open a PR ("
    assert any(m.startswith(expected) for m in announced)


def test_non_github_remote_never_blocks(repo, tmp_path, monkeypatch):
    run_id = "r6"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch="main")
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)
    bin_dir = tmp_path / "fakebin"
    write_fake_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = finish_run(repo, run_id, announce=lambda _m: None)
    assert result.pr_url is None
    assert "GitHub" in result.pr_skip_reason


# ---------------------------------------------------------------- teardown


def test_teardown_removes_merged_worktree_keeps_unmerged(repo, tmp_path):
    run_id = "r8"
    merged = make_group("g1")
    unmerged = make_group("g2")
    paths, merger = setup_run(repo, run_id, [merged, unmerged], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, merged)
    unmerged_wt = create_worktree(
        repo,
        run_id=run_id,
        group_id=unmerged.id,
        name=unmerged.name,
        branch=group_branch(run_id, unmerged.id),
        start_point=merger.tip(),
    )
    write_state(
        paths,
        {
            merged.id: GroupRunState(state=GroupState.COMPLETED),
            unmerged.id: GroupRunState(state=GroupState.FAILED, failure="boom"),
        },
    )
    add_origin(repo, github_url=False)

    result = finish_run(repo, run_id, announce=lambda _m: None)

    merged_wt = worktree_path(repo, run_id, merged.id, merged.name)
    assert not merged_wt.exists()
    listing = git(repo, "worktree", "list", "--porcelain")
    assert str(merged_wt) not in listing

    assert unmerged_wt.is_dir()
    assert unmerged.id in result.unmerged
    assert str(unmerged_wt) in git(repo, "worktree", "list", "--porcelain")

    # the integration branch/worktree are never touched
    integration_wt = worktree_path(repo, run_id, "integration", "integration")
    assert integration_wt.is_dir()
    assert git(repo, "rev-parse", "--verify", merger.branch).strip()


def test_leftover_patch_written_before_force_removal(repo, tmp_path):
    run_id = "r9"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)

    wt = create_worktree(
        repo,
        run_id=run_id,
        group_id=group.id,
        name=group.name,
        branch=group_branch(run_id, group.id),
        start_point=merger.tip(),
    )
    (wt / "g1.txt").write_text("committed\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "g1 work")
    branch_tip = git(wt, "rev-parse", "HEAD").strip()

    # merge the branch into the integration branch directly (bypassing
    # IntegrationMerger, so the worktree survives the merge) then leave an
    # uncommitted change behind.
    integration_wt = merger.ensure()
    git(integration_wt, "merge", "--no-ff", "-m", "manual merge", group_branch(run_id, group.id))
    (wt / "leftover.txt").write_text("uncommitted\n")

    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)

    finish_run(repo, run_id, announce=lambda _m: None)

    patch_path = paths.group_dir(group.id) / "leftover.patch"
    assert patch_path.is_file()
    content = patch_path.read_text()
    assert content.strip()
    assert "leftover.txt" in content

    # applies cleanly to the branch tip
    apply_dir = tmp_path / "apply-check"
    git(repo, "worktree", "add", str(apply_dir), branch_tip)
    result = subprocess.run(
        ["git", "apply", "--check", str(patch_path)], cwd=apply_dir, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------- branch delete


def test_delete_branch_if_merged_keeps_a_branch_git_considers_unmerged(repo):
    run_id = "r10"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    branch = group_branch(run_id, group.id)
    wt = create_worktree(
        repo,
        run_id=run_id,
        group_id=group.id,
        name=group.name,
        branch=branch,
        start_point=merger.tip(),
    )
    (wt / "g1.txt").write_text("never merged\n")
    git(wt, "add", ".")
    git(wt, "commit", "-m", "g1 work")

    integration_wt = merger.ensure()
    deleted = _delete_branch_if_merged(integration_wt, branch)
    assert deleted is False
    assert git(repo, "rev-parse", "--verify", branch).strip()


def test_delete_branch_if_merged_deletes_a_merged_branch(repo):
    run_id = "r11"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    integration_wt = merger.ensure()
    branch = group_branch(run_id, group.id)
    assert _delete_branch_if_merged(integration_wt, branch) is True
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch], cwd=repo, capture_output=True
    )
    assert result.returncode != 0  # the branch is gone


# ------------------------------------------------------------ auto-gate


def test_run_is_finishable_true_only_when_every_group_completed_and_merged(repo, tmp_path):
    run_id = "r12"
    merged = make_group("g1")
    unmerged = make_group("g2")
    paths, merger = setup_run(repo, run_id, [merged, unmerged], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, merged)
    unmerged_wt = create_worktree(
        repo,
        run_id=run_id,
        group_id=unmerged.id,
        name=unmerged.name,
        branch=group_branch(run_id, unmerged.id),
        start_point=merger.tip(),
    )
    (unmerged_wt / "g2.txt").write_text("never merged\n")
    git(unmerged_wt, "add", ".")
    git(unmerged_wt, "commit", "-m", "g2 work")

    write_state(
        paths,
        {
            merged.id: GroupRunState(state=GroupState.COMPLETED),
            unmerged.id: GroupRunState(state=GroupState.FAILED, failure="boom"),
        },
    )
    ok, bad = run_is_finishable(repo, run_id)
    assert ok is False
    assert bad == [unmerged.id]

    write_state(
        paths,
        {
            merged.id: GroupRunState(state=GroupState.COMPLETED),
            unmerged.id: GroupRunState(state=GroupState.COMPLETED),
        },
    )
    ok, bad = run_is_finishable(repo, run_id)
    assert ok is False  # g2's branch never merged, despite the state saying COMPLETED
    assert bad == [unmerged.id]

    merge_group_cleanly(repo, run_id, merger, unmerged)
    write_state(
        paths,
        {
            merged.id: GroupRunState(state=GroupState.COMPLETED),
            unmerged.id: GroupRunState(state=GroupState.COMPLETED),
        },
    )
    ok, bad = run_is_finishable(repo, run_id)
    assert ok is True
    assert bad == []
