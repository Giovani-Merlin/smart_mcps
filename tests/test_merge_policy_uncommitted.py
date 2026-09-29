"""The merge ladder commits a recipe's declared outputs a worker left
uncommitted, and names untracked offenders file by file.

Regression for run r20260927-100604's live item g3-4 (second attempt): a real
research worker wrote its Findings Artifact under a fresh ``docs/research/``
and reported without committing it. The merge policy read the default
``git status --porcelain``, which collapses that directory to ``docs/`` — no
file glob can match it — and the refusal named ``docs/`` instead of the file.
"""

from __future__ import annotations

import pytest

from orchestrator.execution.artifacts import ArtifactManifestStore
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.review import make_executor
from orchestrator.execution.scheduler import GroupFailure, GroupState
from orchestrator.model import ReviewIntensity

from tests.test_merge_policy import (  # noqa: F401 — fixtures are used by name
    _context,
    _deps,
    _make_worktree,
    git,
    repo,
    stub_recipe,
)
from tests.test_review_loop import StubRunner, coder_report, make_group


@pytest.mark.asyncio
async def test_uncommitted_declared_output_is_committed_and_merged(repo, tmp_path, stub_recipe):
    merger = IntegrationMerger(repo, "r1")
    wt = _make_worktree(repo, merger)
    (wt / "docs" / "research" / "x.md").write_text("finding\n")  # never committed

    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    artifact_store = ArtifactManifestStore(RunPaths(repo, "r1", run_dir=tmp_path / "run"))
    deps = _deps(repo, tmp_path, runner, wt, merger, artifacts=artifact_store)
    group = make_group("g1", intensity=ReviewIntensity.SELF_VERIFY, recipe="stub")

    state = await make_executor(deps)(_context(group))

    assert state == GroupState.COMPLETED
    assert artifact_store.load().entries["g1"].paths == ["docs/research/x.md"]
    log = git(repo, "log", "--format=%s", merger.branch)
    assert "stub(g1): commit declared output left uncommitted" in log


@pytest.mark.asyncio
async def test_untracked_offender_in_a_new_directory_is_named_by_file(repo, tmp_path, stub_recipe):
    merger = IntegrationMerger(repo, "r1")
    wt = _make_worktree(repo, merger)
    (wt / "docs" / "research" / "x.md").write_text("finding\n")
    (wt / "notes").mkdir()
    (wt / "notes" / "a.txt").write_text("out of scope\n")

    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    deps = _deps(repo, tmp_path, runner, wt, merger)
    group = make_group("g1", intensity=ReviewIntensity.SELF_VERIFY, recipe="stub")

    with pytest.raises(GroupFailure) as excinfo:
        await make_executor(deps)(_context(group))
    message = str(excinfo.value)
    assert "notes/a.txt" in message
    assert "docs/research/x.md" not in message
    # Only the declared output was staged; the offender stays untracked.
    assert "?? notes/" in git(wt, "status", "--porcelain")
