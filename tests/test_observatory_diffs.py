"""U29 tests: per-generation and per-group diffs on scripted git fixture repos."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.worktrees import create_worktree, group_branch, remove_worktree
from orchestrator.model import GroupManifestEntry, RunManifest, SessionEntry, SessionRole
from orchestrator.observatory.artifacts import (
    DIFF_TRUNCATE_BYTES,
    generation_diff,
    group_diff,
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


def commit(worktree: Path, filename: str, content: str, message: str, when: datetime) -> str:
    (worktree / filename).write_text(content)
    git(worktree, "add", ".")
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S%z")
    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=worktree,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        },
    )
    assert result.returncode == 0, result.stderr
    return git(worktree, "rev-parse", "HEAD").strip()


def paths_for(repo: Path, run_id: str) -> RunPaths:
    return RunPaths(repo, run_id)


def write_manifest(paths: RunPaths, group_id: str, sessions: list[SessionEntry]) -> None:
    manifest = RunManifest(
        run_id=paths.run_id,
        plan_path="plan.md",
        groups={
            group_id: GroupManifestEntry(
                group_id=group_id, group_name=group_id, summary="s", sessions=sessions
            )
        },
    )
    ManifestStore(paths).save(manifest)


def coder_session(generation: int, started_at: datetime) -> SessionEntry:
    return SessionEntry(
        session_id=f"sess-g{generation}",
        role=SessionRole.CODER,
        generation=generation,
        started_at=started_at.isoformat(),
    )


# ------------------------------------------------------------------ group_diff


def test_group_diff_is_the_range_from_the_fork_point_to_the_branch_tip(repo):
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    now = datetime.now(UTC)
    commit(wt, "one.txt", "g1 work\n", "feat: g1", now)

    result = group_diff(paths_for(repo, "r1"), "g1")

    assert result.available
    assert "one.txt" in result.diff
    assert "+g1 work" in result.diff
    assert not result.truncated


def test_group_diff_reports_a_torn_down_branch_as_unavailable_not_an_error(repo):
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    commit(wt, "one.txt", "g1 work\n", "feat: g1", datetime.now(UTC))
    from orchestrator.model import Group, ReviewIntensity

    merger.merge_group(
        Group(
            id="g1",
            name="g1",
            summary="s",
            spec="s",
            difficulty=0.1,
            intensity=ReviewIntensity.SELF_VERIFY,
            files=["one.txt"],
        ),
        wt,
    )
    # Simulate finish.py's teardown: the branch is deleted once merged.
    git(repo, "branch", "-D", branch)

    result = group_diff(paths_for(repo, "r1"), "g1")

    assert result.available is False
    assert "torn down" in result.reason


def test_group_diff_truncates_a_large_diff_and_states_it(repo):
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    big = "x\n" * 200_000
    commit(wt, "big.txt", big, "feat: big file", datetime.now(UTC))

    result = group_diff(paths_for(repo, "r1"), "g1")

    assert result.available
    assert result.truncated is True
    assert result.total_bytes is not None and result.total_bytes > DIFF_TRUNCATE_BYTES
    assert len(result.diff.encode("utf-8")) <= DIFF_TRUNCATE_BYTES


# ------------------------------------------------------------- generation_diff


def test_generation_diff_returns_only_that_generations_commits(repo):
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    t0 = datetime.now(UTC) - timedelta(hours=2)
    t1 = t0 + timedelta(minutes=10)
    commit(wt, "gen1.txt", "gen1 work\n", "feat: gen1", t1)

    t2 = t0 + timedelta(hours=1)
    t3 = t2 + timedelta(minutes=10)
    commit(wt, "gen2.txt", "gen2 work\n", "feat: gen2", t3)

    paths = paths_for(repo, "r1")
    write_manifest(
        paths,
        "g1",
        [coder_session(1, t0), coder_session(2, t2)],
    )

    gen1 = generation_diff(paths, "g1", 1)
    assert gen1.available
    assert "gen1.txt" in gen1.diff
    assert "gen2.txt" not in gen1.diff

    gen2 = generation_diff(paths, "g1", 2)
    assert gen2.available
    assert "gen2.txt" in gen2.diff
    assert "gen1.txt" not in gen2.diff


def test_generation_diff_is_stable_after_the_generation_ends(repo):
    """The final diff, not a running feed: computed twice, it must not change."""
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    t0 = datetime.now(UTC) - timedelta(hours=1)
    commit(wt, "gen1.txt", "gen1 work\n", "feat: gen1", t0 + timedelta(minutes=5))

    paths = paths_for(repo, "r1")
    write_manifest(paths, "g1", [coder_session(1, t0)])

    first = generation_diff(paths, "g1", 1)
    time.sleep(0.01)
    second = generation_diff(paths, "g1", 1)
    assert first.diff == second.diff
    assert first.to_ref == second.to_ref


def test_generation_diff_missing_timing_data_is_unavailable_not_a_guess(repo):
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    paths = paths_for(repo, "r1")
    write_manifest(paths, "g1", [])

    result = generation_diff(paths, "g1", 1)
    assert result.available is False
    assert "no recorded start time" in result.reason


def test_generation_diff_reports_torn_down_branch_as_unavailable(repo):
    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    remove_worktree(repo, wt)
    git(repo, "branch", "-D", branch)

    paths = paths_for(repo, "r1")
    write_manifest(paths, "g1", [coder_session(1, datetime.now(UTC))])

    result = generation_diff(paths, "g1", 1)
    assert result.available is False
    assert "torn down" in result.reason


# ---------------------------------------------------------------- HTTP routes


def test_diff_endpoints_are_wired_through_the_app(repo, tmp_path):
    import yaml
    from fastapi.testclient import TestClient

    from orchestrator.observatory.app import create_app

    merger = IntegrationMerger(repo, "r1")
    branch = group_branch("r1", "g1")
    wt = create_worktree(
        repo, run_id="r1", group_id="g1", name="g1", branch=branch, start_point=merger.tip()
    )
    t0 = datetime.now(UTC)
    commit(wt, "one.txt", "g1 work\n", "feat: g1", t0)
    paths = paths_for(repo, "r1")
    write_manifest(paths, "g1", [coder_session(1, t0)])
    # A run directory has to exist for resolve_run to find it.
    (repo / ".orchestrator" / "runs" / "r1").mkdir(parents=True, exist_ok=True)

    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump({"projects": [{"name": "proj", "repo": str(repo)}]}))
    client = TestClient(create_app(registry_path=registry))

    group_resp = client.get("/api/projects/proj/runs/r1/groups/g1/diff")
    assert group_resp.status_code == 200
    assert group_resp.json()["available"] is True
    assert "one.txt" in group_resp.json()["diff"]

    gen_resp = client.get("/api/projects/proj/runs/r1/groups/g1/generations/1/diff")
    assert gen_resp.status_code == 200
    assert gen_resp.json()["available"] is True, gen_resp.json()
    assert "one.txt" in gen_resp.json()["diff"]
