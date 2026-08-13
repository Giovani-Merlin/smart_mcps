"""The cwd-vs-`--repo` contract (plan P8).

Every other test in this suite passes an *absolute* plan path together with an
explicit `--repo`, and none of them ever changes directory — `grep -rn
"monkeypatch.chdir\\|os.chdir" tests/*.py` returned nothing before this file
existed. So the whole class of "works from the repo, fails from anywhere else"
defect had zero coverage, and `group docs/plan.md --repo <other repo>` failed
from another directory with a plan that plainly existed: `group` passed its
argument through verbatim, and the `Path.is_file()` downstream resolved it
against the *process* cwd. `run`/`resume` re-anchored correctly; only `group`
did not.

An orchestrator is driven from another repo by design (it is installed as a
`uv tool`), so "which directory am I standing in" must never change an outcome.
Hence every test here runs from a foreign cwd.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import main
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.model import ReviewIntensity
from test_cli import make_group, write_run_artifacts
from test_e2e_stub import (
    coder_entry,
    name_of,
    script_session,
    state_of,
    write_config,
)
from test_e2e_stub import fake_home as _fake_home
from test_e2e_stub import repo as _repo
from test_grouper_pipeline import StubLlm, codegraph_response

# The toy git repo and scripted-claude home are the stub tier's fixtures; they
# are re-exported rather than re-declared so this file cannot drift from the
# harness the rest of the E2E suite runs against. Aliased on import because a
# test taking `repo` as a parameter otherwise reads as a redefinition.
repo = _repo
fake_home = _fake_home


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """A directory that is not the target repo and knows nothing about it."""
    foreign = tmp_path / "some-other-place"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    return foreign


def _stub_client(repo_root):
    return CodegraphClient(repo_root=repo_root, runner=codegraph_response)


# ------------------------------------------------------------------ `group`


def test_group_resolves_a_repo_relative_plan_from_a_foreign_cwd(repo, elsewhere):
    """The exact invocation that failed: a path relative to `--repo`, typed from
    somewhere else entirely."""
    exit_code = main(
        ["group", "plan.md", "--repo", str(repo)],
        llm_runner=StubLlm(),
        client=_stub_client(repo),
    )
    assert exit_code == 0
    grouping = repo / ".orchestrator" / "groupings" / "plan" / "groups.json"
    assert grouping.is_file()
    assert json.loads(grouping.read_text())["groups"]


def test_group_still_prefers_a_path_that_resolves_against_cwd(repo, elsewhere):
    """cwd keeps winning when it resolves.

    An operator standing in one repo and pointing `--repo` at another must get
    the file they typed, not a same-named file inside the target repo. Both
    plans are pre-mapped (so the task ids come from the plan text itself rather
    than from the stubbed mapper) and one task is renamed, which is what makes
    the two distinguishable in the output.
    """
    from test_grouper_pipeline import GREENFIELD_PLAN

    (repo / "both.md").write_text(GREENFIELD_PLAN)
    (elsewhere / "both.md").write_text(GREENFIELD_PLAN.replace("t4-docs", "t4-from-cwd"))

    exit_code = main(
        ["group", "both.md", "--repo", str(repo), "--name", "from-cwd"],
        llm_runner=StubLlm(),
        client=_stub_client(repo),
    )
    assert exit_code == 0
    grouping = json.loads(
        (repo / ".orchestrator" / "groupings" / "from-cwd" / "groups.json").read_text()
    )
    tasks = {task for group in grouping["groups"] for task in group["tasks"]}
    assert "t4-from-cwd" in tasks  # the cwd-relative plan, not the repo's
    assert "t4-docs" not in tasks


def test_group_reports_the_path_the_operator_typed_when_it_resolves_nowhere(
    repo, elsewhere, capsys
):
    """A plan that exists in neither place still fails — naming what was typed,
    not a rewritten absolute path the operator never mentioned."""
    exit_code = main(
        ["group", "no-such-plan.md", "--repo", str(repo)],
        llm_runner=StubLlm(),
        client=_stub_client(repo),
    )
    assert exit_code == 1
    assert "no-such-plan.md" in capsys.readouterr().err


def test_repo_defaults_to_cwd_when_the_flag_is_omitted(repo, monkeypatch):
    """The other half of the contract: `--repo` defaults to `Path.cwd()`, so
    standing *inside* the repo must need no flag at all."""
    monkeypatch.chdir(repo)
    exit_code = main(
        ["group", "plan.md"],
        llm_runner=StubLlm(),
        client=_stub_client(repo),
    )
    assert exit_code == 0
    assert (repo / ".orchestrator" / "groupings" / "plan" / "groups.json").is_file()


# ------------------------------------------------------------------- full run


def test_a_full_stub_run_from_a_foreign_cwd_writes_everything_under_repo(
    repo, fake_home, elsewhere
):
    """Nothing a run produces may land in the directory it was launched from.

    Driven entirely from `elsewhere`: run artifacts, worktrees and the
    integration branch all belong to `--repo`.
    """
    run_id = "r-foreign"
    write_run_artifacts(repo, [make_group("g1", intensity=ReviewIntensity.SELF_VERIFY)])
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "work\n"}, commit="g1: scripted work"),
    )

    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 0
    assert state_of(repo, run_id)["groups"]["g1"]["state"] == "completed"
    assert (repo / ".orchestrator" / "runs" / run_id / "manifest.json").is_file()

    # `status` reads the same run from the same foreign cwd.
    assert main(["status", run_id, "--repo", str(repo)]) == 0

    # And the launch directory is untouched — no stray `.orchestrator`,
    # `.worktrees`, or anything else.
    assert list(elsewhere.iterdir()) == []
