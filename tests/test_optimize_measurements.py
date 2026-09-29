"""The optimize loop moves each evaluation's measurements file out of the
worktree, so the merge gate never meets it as an untracked file.

Regression for run r20260927-100604's live item g3-5: the harness wrote
``measurements.json`` into the worktree, the loop read it and left it there,
and every live optimize run failed its first merge gate on the untracked file,
relaunched a second generation (overrunning the evaluation budget) and merged
only after the untracked ladder archived it.
"""

from __future__ import annotations

import asyncio

from orchestrator.execution.kpi import Ledger
from orchestrator.execution.scheduler import GroupState

from tests.test_optimize_executor import (  # noqa: F401 — fixtures are used by name
    _run,
    base_optimize_args,
    candidate_round,
    coder_name,
    fake_claude_home,
    fake_home,
    git,
    make_deps,
    make_group,
    make_runner,
    repo,
    script_session,
)


def test_measurements_file_leaves_the_worktree_after_each_evaluation(tmp_path, repo, fake_home):
    run_dir = tmp_path / "run"
    deps = make_deps(repo, run_dir, repo, make_runner(fake_home))
    group = make_group(base_optimize_args(evaluations=2))
    script_session(
        fake_home,
        coder_name(group.id),
        candidate_round("9", "round1"),
        candidate_round("12", "round2"),
    )

    state, _ctx = asyncio.run(_run(deps, group))

    assert state == GroupState.COMPLETED
    assert not (repo / "measurements.json").exists()
    assert "measurements.json" not in git(repo, "status", "--porcelain", "--untracked-files=all")
    eval_dir = run_dir / "groups" / group.id / "eval"
    archived = sorted(p.parent.name for p in eval_dir.glob("attempt-*/measurements.json"))
    # The launch baseline (attempt-0) and both candidates each left one copy.
    assert archived == ["attempt-0", "attempt-1", "attempt-2"]
    ledger = Ledger.load(run_dir / "groups" / group.id / "ledger.json")
    assert [a.outcome for a in ledger.attempts] == ["keep", "keep"]
