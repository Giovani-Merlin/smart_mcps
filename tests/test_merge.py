"""U8 tests: integration-branch merges on scripted git fixture repos (plan Phase B)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.execution.merge import IntegrationMerger, MergeError, commits_ahead
from orchestrator.execution.review import MergeConflict
from orchestrator.execution.worktrees import (
    WorktreeError,
    create_worktree,
    group_branch,
    remove_worktree,
)
from orchestrator.model import Group, ReviewIntensity


def make_group(gid: str, files: list[str] | None = None) -> Group:
    return Group(
        id=gid,
        name=f"group {gid}",
        summary=f"summary {gid}",
        spec=f"spec {gid}",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=files or [],
    )


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
    (repo / "shared.txt").write_text("original\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def coder_commit(worktree: Path, filename: str, content: str, message: str) -> None:
    (worktree / filename).write_text(content)
    git(worktree, "add", ".")
    git(worktree, "commit", "-m", message)


def group_worktree(repo: Path, merger: IntegrationMerger, group: Group) -> Path:
    """A group's worktree branches from the integration tip at launch (plan U5)."""
    return create_worktree(
        repo,
        group_id=group.id,
        name=group.name,
        branch=group_branch(merger.run_id, group.id),
        start_point=merger.tip(),
    )


def test_groups_merge_in_dependency_order_onto_the_integration_branch(repo):
    merger = IntegrationMerger(repo, "r1")
    main_before = git(repo, "rev-parse", "main").strip()

    g1 = make_group("g1", files=["one.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "one.txt", "g1 work\n", "feat: g1")
    merger.merge_group(g1, wt1)

    g2 = make_group("g2", files=["two.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "two.txt", "g2 work\n", "feat: g2")
    merger.merge_group(g2, wt2)

    tree = git(repo, "ls-tree", "--name-only", merger.branch)
    assert "one.txt" in tree and "two.txt" in tree
    log = git(repo, "log", "--format=%s", merger.branch)
    assert log.index("merge(r1): g2 group g2") < log.index("merge(r1): g1 group g1")
    # the main branch is never touched
    assert git(repo, "rev-parse", "main").strip() == main_before


def test_dependent_worktree_branches_from_tip_and_contains_upstream_work(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1")
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "upstream.txt", "from g1\n", "feat: g1")
    merger.merge_group(g1, wt1)

    wt2 = group_worktree(repo, merger, make_group("g2"))
    assert (wt2 / "upstream.txt").read_text() == "from g1\n"


def test_conflict_leaves_integration_untouched_and_names_both_groups(repo):
    merger = IntegrationMerger(repo, "r1")

    g1 = make_group("g1", files=["shared.txt"])
    wt1 = group_worktree(repo, merger, g1)
    launch_tip = merger.tip()
    coder_commit(wt1, "shared.txt", "g1 version\n", "feat: g1")

    # g2 branched in parallel from the same tip — the collision scenario
    g2 = make_group("g2", files=["shared.txt"])
    wt2 = create_worktree(
        repo,
        group_id="g2",
        name="group g2",
        branch=group_branch("r1", "g2"),
        start_point=launch_tip,
    )
    coder_commit(wt2, "shared.txt", "g2 version\n", "feat: g2")

    merger.merge_group(g1, wt1)
    tip_before = merger.tip()
    with pytest.raises(MergeConflict) as excinfo:
        merger.merge_group(g2, wt2)
    assert "shared.txt" in str(excinfo.value)
    assert excinfo.value.affected_groups == ["g2", "g1"]
    assert merger.tip() == tip_before  # aborted merge left no trace
    integration_wt = merger.ensure()
    assert git(integration_wt, "status", "--porcelain").strip() == ""


def test_worktree_cleanup_runs_only_after_a_successful_merge(repo):
    merger = IntegrationMerger(repo, "r1")

    g1 = make_group("g1", files=["shared.txt"])
    wt1 = group_worktree(repo, merger, g1)
    launch_tip = merger.tip()
    coder_commit(wt1, "shared.txt", "g1 version\n", "feat: g1")
    g2 = make_group("g2", files=["shared.txt"])
    wt2 = create_worktree(
        repo,
        group_id="g2",
        name="group g2",
        branch=group_branch("r1", "g2"),
        start_point=launch_tip,
    )
    coder_commit(wt2, "shared.txt", "g2 version\n", "feat: g2")

    merger.merge_group(g1, wt1)
    assert not wt1.exists()  # cleaned after clean merge
    with pytest.raises(MergeConflict):
        merger.merge_group(g2, wt2)
    assert wt2.exists()  # conflicting group's worktree survives for the rewrite


def test_dirty_worktree_survives_merge_for_inspection(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1")
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "one.txt", "g1 work\n", "feat: g1")
    (wt1 / "scratch.log").write_text("uncommitted leftovers\n")
    merger.merge_group(g1, wt1)  # merge succeeds
    assert wt1.exists()  # cleanup refused to destroy uncommitted state


def test_ensure_is_idempotent_and_creates_branch_from_launch_ref(repo):
    launch = git(repo, "rev-parse", "HEAD").strip()
    merger = IntegrationMerger(repo, "r1")
    first = merger.ensure()
    second = merger.ensure()
    assert first == second
    assert git(repo, "merge-base", merger.branch, "main").strip() == launch


# --------------------------------------------------------------- U1: merge gate


def test_merge_refuses_a_branch_with_zero_commits_ahead(repo):
    merger = IntegrationMerger(repo, "r1")
    tip_before = merger.tip()
    g1 = make_group("g1")
    wt1 = group_worktree(repo, merger, g1)  # cut from the tip, no coder commit made
    branch = group_branch("r1", "g1")
    with pytest.raises(MergeError, match=r"g1.*orchestrator/r1-g1"):
        merger.merge_group(g1, wt1)
    assert merger.tip() == tip_before  # nothing touched
    assert wt1.exists()  # the refused worktree is left in place, not cleaned up


def test_commit_count_must_be_taken_before_the_merge_not_after(repo):
    """Documents why merge_group snapshots the count before merging: after a
    clean merge the branch's commits are reachable from the integration branch
    too, so the same count reads zero and a post-merge check could never
    distinguish a real merge from a no-op (plan U1)."""
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1")
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "one.txt", "g1 work\n", "feat: g1")
    integration_wt = merger.ensure()
    branch = group_branch("r1", "g1")
    assert commits_ahead(integration_wt, merger.branch, branch) == 1

    merger.merge_group(g1, wt1)
    assert commits_ahead(integration_wt, merger.branch, branch) == 0


# ---------------------------------------------------------- U1: branch refresh


def test_refresh_fast_forwards_a_strictly_behind_branch(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1")
    wt1 = group_worktree(repo, merger, g1)  # cut, no commits of its own

    g2 = make_group("g2", files=["two.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "two.txt", "g2 work\n", "feat: g2")
    merger.merge_group(g2, wt2)
    new_tip = merger.tip()

    resumed = create_worktree(
        repo, group_id="g1", name=g1.name, branch=group_branch("r1", "g1"), start_point=new_tip
    )
    assert resumed == wt1
    assert git(resumed, "rev-parse", "HEAD").strip() == new_tip
    # a fast-forward never creates a "refresh(...)" merge commit
    assert "refresh(g1)" not in git(resumed, "log", "--format=%s")


def test_refresh_merges_a_diverged_branch_reaching_both_tips(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1", files=["own.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "own.txt", "g1's own work\n", "feat: g1 own commit")
    own_commit = git(wt1, "rev-parse", "HEAD").strip()

    g2 = make_group("g2", files=["two.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "two.txt", "g2 work\n", "feat: g2")
    merger.merge_group(g2, wt2)
    new_tip = merger.tip()

    refreshed = create_worktree(
        repo, group_id="g1", name=g1.name, branch=group_branch("r1", "g1"), start_point=new_tip
    )
    assert git(refreshed, "merge-base", "--is-ancestor", own_commit, "HEAD").strip() == ""
    assert git(refreshed, "merge-base", "--is-ancestor", new_tip, "HEAD").strip() == ""


def test_refresh_conflict_raises_naming_group_and_paths_and_leaves_head_untouched(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1", files=["shared.txt"])
    wt1 = group_worktree(repo, merger, g1)
    coder_commit(wt1, "shared.txt", "g1 version\n", "feat: g1 edits shared")
    head_before = git(wt1, "rev-parse", "HEAD").strip()

    g2 = make_group("g2", files=["shared.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "shared.txt", "g2 version\n", "feat: g2 edits shared")
    merger.merge_group(g2, wt2)
    new_tip = merger.tip()

    with pytest.raises(WorktreeError, match=r"g1.*shared\.txt"):
        create_worktree(
            repo, group_id="g1", name=g1.name, branch=group_branch("r1", "g1"), start_point=new_tip
        )
    assert git(wt1, "rev-parse", "HEAD").strip() == head_before
    assert not (wt1 / ".git" / "MERGE_HEAD").exists()
    assert git(wt1, "status", "--porcelain").strip() == ""


def test_refresh_preserves_uncommitted_changes_it_cannot_safely_apply_over(repo):
    merger = IntegrationMerger(repo, "r1")
    g1 = make_group("g1", files=["shared.txt"])
    wt1 = group_worktree(repo, merger, g1)
    (wt1 / "shared.txt").write_text("uncommitted local edit\n")  # never committed

    g2 = make_group("g2", files=["shared.txt"])
    wt2 = group_worktree(repo, merger, g2)
    coder_commit(wt2, "shared.txt", "g2 version\n", "feat: g2 edits shared")
    merger.merge_group(g2, wt2)
    new_tip = merger.tip()

    with pytest.raises(WorktreeError, match="g1"):
        create_worktree(
            repo, group_id="g1", name=g1.name, branch=group_branch("r1", "g1"), start_point=new_tip
        )
    assert (wt1 / "shared.txt").read_text() == "uncommitted local edit\n"


def test_refresh_reaches_the_tip_whether_worktree_survived_or_only_branch_did(repo):
    merger = IntegrationMerger(repo, "r1")

    # path A: the worktree still exists (the common interrupt case, since
    # worktrees are never removed on interrupt)
    ga = make_group("ga")
    wta = group_worktree(repo, merger, ga)  # cut, interrupted before any commit
    gx = make_group("gx", files=["x.txt"])
    wtx = group_worktree(repo, merger, gx)
    coder_commit(wtx, "x.txt", "landed while ga was down\n", "feat: gx")
    merger.merge_group(gx, wtx)
    tip_a = merger.tip()

    resumed = create_worktree(
        repo, group_id="ga", name=ga.name, branch=group_branch("r1", "ga"), start_point=tip_a
    )
    assert resumed == wta
    assert (resumed / "x.txt").read_text() == "landed while ga was down\n"

    # path B: only the branch remains (worktree removed by an operator)
    gb = make_group("gb")
    wtb = group_worktree(repo, merger, gb)
    remove_worktree(repo, wtb, force=True)
    gy = make_group("gy", files=["y.txt"])
    wty = group_worktree(repo, merger, gy)
    coder_commit(wty, "y.txt", "landed while gb was down\n", "feat: gy")
    merger.merge_group(gy, wty)
    tip_b = merger.tip()

    reentered = create_worktree(
        repo, group_id="gb", name=gb.name, branch=group_branch("r1", "gb"), start_point=tip_b
    )
    assert (reentered / "y.txt").read_text() == "landed while gb was down\n"
