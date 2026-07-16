"""U8 tests: integration-branch merges on scripted git fixture repos (plan Phase B)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.review import MergeConflict
from orchestrator.execution.worktrees import create_worktree, group_branch
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
