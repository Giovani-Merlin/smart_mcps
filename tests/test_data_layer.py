"""The shared data layer (orchestrator-notes #1/#2): configured data dirs are
symlinked into every worktree and excluded from git; oversized untracked files
are relocated out of a stranded-work commit instead of becoming blobs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.config import WorkspaceConfig
from orchestrator.execution.worktrees import (
    commit_all,
    create_worktree,
    materialize_data_layer,
    read_large_file_registry,
    relocate_large_files,
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
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def make_wt(repo: Path, gid: str, workspace: WorkspaceConfig | None, log=None) -> Path:
    return create_worktree(
        repo,
        run_id="r1",
        group_id=gid,
        name=gid,
        branch=f"orchestrator/r1-{gid}",
        start_point="HEAD",
        workspace=workspace,
        log=log,
    )


# ------------------------------------------------------------------ data dirs


def test_data_dir_is_linked_into_the_worktree_and_shared_both_ways(repo):
    workspace = WorkspaceConfig(data_dirs=["data"])
    (repo / "data").mkdir()
    (repo / "data" / "corpus.zip").write_bytes(b"x" * 10)

    wt1 = make_wt(repo, "g1", workspace)
    link = wt1 / "data"
    assert link.is_symlink() and link.resolve() == (repo / "data").resolve()
    assert (link / "corpus.zip").read_bytes() == b"x" * 10  # repo → worktree

    (link / "model.bin").write_bytes(b"m")  # worktree → repo, no commit involved
    assert (repo / "data" / "model.bin").exists()

    wt2 = make_wt(repo, "g2", workspace)
    assert (wt2 / "data" / "model.bin").exists()  # and → every sibling


def test_data_dir_link_is_invisible_to_git(repo):
    workspace = WorkspaceConfig(data_dirs=["data"])
    wt = make_wt(repo, "g1", workspace)
    (wt / "data" / "big.bin").write_bytes(b"b" * 100)
    assert git(wt, "status", "--porcelain").strip() == ""
    # commit_all — the stranded-work path — therefore has nothing to commit.
    assert commit_all(wt, "recover", repo_root=repo, workspace=workspace) is False


def test_data_dir_is_created_when_missing_and_materialize_is_idempotent(repo):
    workspace = WorkspaceConfig(data_dirs=["inputs/raw"])
    wt = make_wt(repo, "g1", workspace)
    assert (repo / "inputs" / "raw").is_dir()
    assert (wt / "inputs" / "raw").is_symlink()
    materialize_data_layer(wt, repo, workspace)
    materialize_data_layer(wt, repo, workspace)
    assert (wt / "inputs" / "raw").is_symlink()


def test_existing_content_at_the_link_path_is_never_replaced(repo):
    workspace = WorkspaceConfig(data_dirs=["data"])
    wt = make_wt(repo, "g1", None)
    (wt / "data").mkdir()
    (wt / "data" / "precious.txt").write_text("keep me\n")
    events: list[str] = []
    materialize_data_layer(wt, repo, workspace, log=events.append)
    assert not (wt / "data").is_symlink()
    assert (wt / "data" / "precious.txt").read_text() == "keep me\n"
    assert any("already holds content" in e for e in events)


def test_without_workspace_nothing_changes(repo):
    wt = make_wt(repo, "g1", None)
    assert not (wt / "data").exists()
    materialize_data_layer(wt, repo, None)
    assert not (wt / "data").exists()


# ------------------------------------------------------ large-file safety net


def test_large_untracked_file_is_relocated_not_committed(repo):
    workspace = WorkspaceConfig(large_file_bytes=1000, large_file_store=".orchestrator/data")
    wt = make_wt(repo, "g1", workspace)
    (wt / "der_sandmann.pdf").write_bytes(b"p" * 5000)
    (wt / "small.txt").write_text("small\n")
    events: list[str] = []

    assert (
        commit_all(
            wt, "recover(r1): g1 stranded", repo_root=repo, workspace=workspace, log=events.append
        )
        is True
    )

    # The PDF lives in the store; the worktree holds a link; git saw only small.txt.
    store_copy = repo / ".orchestrator" / "data" / "der_sandmann.pdf"
    assert store_copy.read_bytes() == b"p" * 5000
    assert (wt / "der_sandmann.pdf").is_symlink()
    committed = git(wt, "show", "--stat", "--format=", "HEAD")
    assert "small.txt" in committed and "der_sandmann.pdf" not in committed
    assert read_large_file_registry(repo, workspace) == ["der_sandmann.pdf"]
    assert any("relocated der_sandmann.pdf" in e for e in events)
    assert any("committed stranded work" in e for e in events)
    assert git(wt, "status", "--porcelain").strip() == ""  # excluded, not "untracked"


def test_registered_large_files_are_linked_into_later_worktrees(repo):
    workspace = WorkspaceConfig(large_file_bytes=1000)
    wt1 = make_wt(repo, "g1", workspace)
    (wt1 / "models" / "v1").mkdir(parents=True)
    (wt1 / "models" / "v1" / "weights.bin").write_bytes(b"w" * 2000)
    assert relocate_large_files(wt1, repo, workspace) == ["models/v1/weights.bin"]

    wt2 = make_wt(repo, "g2", workspace)
    link = wt2 / "models" / "v1" / "weights.bin"
    assert link.is_symlink() and link.read_bytes() == b"w" * 2000
    assert git(wt2, "status", "--porcelain").strip() == ""


def test_tracked_and_small_files_are_left_alone(repo):
    workspace = WorkspaceConfig(large_file_bytes=1000)
    wt = make_wt(repo, "g1", workspace)
    (wt / "base.txt").write_bytes(b"t" * 5000)  # tracked: the repo's own decision
    (wt / "note.txt").write_bytes(b"n" * 10)
    assert relocate_large_files(wt, repo, workspace) == []
    assert not (wt / "base.txt").is_symlink()


def test_recovery_reentry_relocates_before_committing(repo):
    """The r20260830-211717 shape: a worktree left with a large untracked file
    is re-entered by create_worktree, whose stranded-work commit must not
    carry the blob."""
    workspace = WorkspaceConfig(large_file_bytes=1000)
    wt = make_wt(repo, "g1", workspace)
    (wt / "archive.zip").write_bytes(b"z" * 4000)
    again = make_wt(repo, "g1", workspace)
    assert again == wt
    assert (wt / "archive.zip").is_symlink()
    assert "archive.zip" not in git(wt, "log", "--stat", "--format=", "-1")
