"""U8/U9 tests: push, PR, and teardown of exactly what is provably merged.

The push step is stubbed in every test except the two dedicated to it: this
sandbox denies the hardlink git's local-transport push relies on to finalize
objects (`link()` returns EXDEV even within one filesystem, verified against a
push between two freshly created directories with no orchestrator code
involved at all) — a real GitHub remote uses smart-HTTP and never touches that
path, so this is a property of the sandbox, not of `_push_integration_branch`.
"""

from __future__ import annotations

import json
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
from orchestrator.execution.worktrees import (
    create_worktree,
    group_branch,
    integration_branch,
    worktree_path,
)
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


def write_fake_gh(
    bin_dir: Path,
    *,
    body_capture: Path | None = None,
    argv_capture: Path | None = None,
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    body_line = f'cat > "{body_capture}"' if body_capture is not None else "cat > /dev/null"
    # The fake `gh` succeeds whatever flags it is handed, so only an argv
    # capture can pin which flags `finish` actually passes (e.g. --draft).
    argv_line = f'printf \'%s\\n\' "$@" > "{argv_capture}"' if argv_capture is not None else ":"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "auth" ]; then exit 0; fi\n'
        'if [ "$1" = "pr" ] && [ "$2" = "create" ]; then\n'
        f"  {argv_line}\n"
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


def test_finish_opens_a_ready_for_review_pr_against_the_launch_branch(repo, tmp_path, monkeypatch):
    run_id = "r2"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch="main")
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=True)

    bin_dir = tmp_path / "fakebin"
    body_capture = tmp_path / "pr-body.txt"
    argv_capture = tmp_path / "pr-argv.txt"
    write_fake_gh(bin_dir, body_capture=body_capture, argv_capture=argv_capture)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = finish_run(repo, run_id, announce=lambda _m: None)
    assert result.pr_url == "https://github.com/org/repo/pull/1"
    assert result.pr_skip_reason is None

    argv = argv_capture.read_text().splitlines()
    assert "--draft" not in argv
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--head") + 1] == integration_branch(run_id)


def test_pr_body_has_the_five_headings_in_order_and_lists_groups_and_unmerged(
    repo, tmp_path, monkeypatch
):
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
    headings = ["## Motivation", "## Changes", "## Risks", "## Testing", "## Handoff"]
    positions = [body.index(h) for h in headings]
    assert positions == sorted(positions)
    assert "g1" in body and "g2" in body
    assert "Unmerged group" in body and "g2" in body.split("Unmerged group")[1]
    # g2's real (non-stale) failure landed the run in trouble, so the body
    # carries a postmortem naming the failure verbatim.
    assert "## Postmortem" in body
    assert "boom" in body


def test_pr_body_with_no_trouble_omits_postmortem(repo, tmp_path, monkeypatch):
    run_id = "r3b"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch="main")
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=True)
    bin_dir = tmp_path / "fakebin"
    body_capture = tmp_path / "pr-body.txt"
    write_fake_gh(bin_dir, body_capture=body_capture)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    finish_run(repo, run_id, announce=lambda _m: None)

    body = body_capture.read_text()
    assert "## Postmortem" not in body


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


def test_teardown_finds_a_rewritten_groups_worktree_via_spec_gen(repo, tmp_path):
    """r20260830-163212 P0: a speccer-rewritten group's worktree is slugged from
    the rewritten name while groups.json keeps the grouper's — a finish that
    resolves the name bare no-ops on teardown and leaks the worktree."""
    run_id = "r8rw"
    original = make_group("g1")
    rewritten = original.model_copy(update={"name": "rewritten slice", "spec": "new spec"})
    paths, merger = setup_run(repo, run_id, [original], launch_branch=None)
    atomic_write_text(
        paths.group_dir("g1") / "spec-gen1.json", rewritten.model_dump_json(indent=2) + "\n"
    )
    # the worktree lands under the *rewritten* slug, as workspace_for creates it
    rewritten_wt = merge_group_cleanly(repo, run_id, merger, rewritten)
    write_state(paths, {"g1": GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)

    finish_run(repo, run_id, announce=lambda _m: None)

    assert not rewritten_wt.exists()
    assert str(rewritten_wt) not in git(repo, "worktree", "list", "--porcelain")


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


def test_scratch_and_heartbeat_archived_under_run_dir_gone_from_worktree(repo, tmp_path):
    from orchestrator.execution.heartbeat import heartbeat_path
    from orchestrator.execution.prompting import REVIEW_SCRATCH_DIRNAME

    run_id = "r9b"
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

    # merge directly so the worktree (and its scratch litter) survives to teardown
    integration_wt = merger.ensure()
    git(integration_wt, "merge", "--no-ff", "-m", "manual merge", group_branch(run_id, group.id))

    scratch_dir = wt / REVIEW_SCRATCH_DIRNAME
    scratch_dir.mkdir()
    (scratch_dir / "notes.txt").write_text("reviewer scratch\n")

    hb_path = heartbeat_path(paths, group.id)
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text("{}")

    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)

    finish_run(repo, run_id, announce=lambda _m: None)

    archived = paths.review_scratch_archive_dir(group.id) / "notes.txt"
    assert archived.is_file()
    assert archived.read_text() == "reviewer scratch\n"
    assert not wt.exists()  # the worktree (and any scratch inside it) is gone

    # the heartbeat already lived under the run dir, never in the worktree
    assert hb_path.is_file()


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


# -------------------------------------------- legacy (pre-U2) worktree layout


def _demote_to_legacy(repo: Path, current: Path, legacy: Path) -> Path:
    """Move a worktree back to the pre-U2, run-unscoped path, the way a run
    started before U2 landed has them on disk."""
    legacy.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "move", str(current), str(legacy))
    return legacy


def test_finish_adopts_a_legacy_integration_worktree(repo, tmp_path):
    """A run started before U2's run-scoping must still be finishable.

    Regression for the crash observed finishing r20260819-crashrec on
    2026-08-20: `finish` constructed the run-scoped integration path, handed the
    non-existent directory to git as cwd, and died with a bare
    `FileNotFoundError: [Errno 2]` *after* pushing and opening the PR — so the
    run was left half-finished with no teardown.
    """
    run_id = "r-legacy-int"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    legacy_integration = _demote_to_legacy(
        repo,
        worktree_path(repo, run_id, "integration", "integration"),
        repo / ".worktrees" / f"run-{run_id}-integration",
    )
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)

    result = finish_run(repo, run_id, announce=lambda _m: None)

    assert result.unmerged == []
    # The merged group's branch was deleted, which only works if the merge check
    # ran with HEAD at the integration tip — i.e. in the adopted worktree.
    assert result.kept_branches == []
    assert group_branch(run_id, group.id) not in git(repo, "branch", "--list")
    assert legacy_integration.is_dir()


def test_finish_tears_down_a_legacy_group_worktree(repo, tmp_path):
    """The same fallback on the group side: constructing the run-scoped path
    would silently no-op and strand the worktree forever.

    A clean merge already removes the group worktree (merge.py), so the case
    that reaches teardown is one recreated afterwards — a `retry`, or an
    operator inspecting the branch — which on a pre-U2 run lands at the legacy
    path.
    """
    run_id = "r-legacy-grp"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    legacy_group = repo / ".worktrees" / f"{group.id}-group-{group.id}"
    git(repo, "worktree", "add", str(legacy_group), group_branch(run_id, group.id))
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)

    finish_run(repo, run_id, announce=lambda _m: None)

    assert not legacy_group.exists()
    assert str(legacy_group) not in git(repo, "worktree", "list", "--porcelain")


def test_finish_names_the_missing_integration_worktree(repo, tmp_path):
    """Neither layout present is an operator-legible FinishError, not Errno 2."""
    run_id = "r-no-int"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    integration_wt = worktree_path(repo, run_id, "integration", "integration")
    git(repo, "worktree", "remove", "--force", str(integration_wt))
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    add_origin(repo, github_url=False)

    with pytest.raises(FinishError, match="integration worktree"):
        finish_run(repo, run_id, announce=lambda _m: None)


# --------------------------------------------------------------- residue (U12)


def test_finish_announces_pending_surprise_residue_with_bucket_and_reason(repo, tmp_path):
    """A surprise still marked for a group that already completed is reported
    with the "already completed" reason (plan U12) — the group's checkpoints
    are all behind it, so nothing will ever consume this bucket."""
    run_id = "r-residue"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})
    atomic_write_text(
        paths.surprises_path,
        json.dumps(
            {"g1": [{"kind": "other", "description": "late finding", "affected_groups": ["g1"]}]}
        ),
    )

    messages: list[str] = []
    finish_run(repo, run_id, announce=messages.append)

    residue = next(m for m in messages if "surprises pending" in m)
    assert "g1: 1 pending" in residue
    assert "already completed" in residue


def test_finish_reports_none_pending_for_an_empty_board(repo, tmp_path):
    run_id = "r-no-residue"
    group = make_group("g1")
    paths, merger = setup_run(repo, run_id, [group], launch_branch=None)
    merge_group_cleanly(repo, run_id, merger, group)
    write_state(paths, {group.id: GroupRunState(state=GroupState.COMPLETED)})

    messages: list[str] = []
    finish_run(repo, run_id, announce=messages.append)

    residue = next(m for m in messages if "surprises pending" in m)
    assert "none pending" in residue
