"""U10 E2E: the full loop against the scripted claude stub, entirely offline.

Every scenario drives the real CLI (``main``) on a toy git fixture repo with
``tests/fake_claude.py`` as the claude binary (via ``[session] claude_bin`` in the
fixture's config.toml) and per-name session scripts — zero live CLI calls, zero
tokens (plan R24). The happy path goes plan → group (stubbed LLM + codegraph) →
run → merge and asserts the analyzer contract (origin AE6); the failure scenarios
(rejection/breaker, surprise, merge conflict, reject-forever) each end in their
documented terminal state.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.execution.escalation import pending_escalations
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.grouping.graphing import CodegraphClient
from orchestrator.model import (
    EscalationResponse,
    GroupingResult,
    HumanAction,
    ReviewIntensity,
)
from test_cli import make_group, write_run_artifacts
from test_grouper_pipeline import PLAN_TEXT, StubLlm, codegraph_response

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
REPO_DIR_NAME = "toyrepo"


# ------------------------------------------------------------------ fixtures


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "fake-home"
    (home / "sessions").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_HOME", str(home))
    monkeypatch.delenv("FAKE_CLAUDE_HIDE_FLAGS", raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Toy target repo: a real git repo whose files match the canned codegraph
    responses reused from test_grouper_pipeline (server.py / test_server.py)."""
    repo = tmp_path / REPO_DIR_NAME
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "e2e@test")
    git(repo, "config", "user.name", "e2e")
    (repo / "CLAUDE.md").write_text("# Conventions\n\nUse ruff line 100.\n")
    (repo / "server.py").write_bytes(b"def real_fn():\n    pass\n" * 20)
    (repo / "test_server.py").write_bytes(b"def test_real_fn():\n    pass\n" * 10)
    (repo / "plan.md").write_text(PLAN_TEXT)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def write_config(repo: Path, fake_home: Path, extra: str = "") -> None:
    (repo / ".orchestrator").mkdir(exist_ok=True)
    (repo / ".orchestrator" / "config.toml").write_text(
        "[session]\n"
        f'claude_bin = ["{sys.executable}", "{FAKE_CLAUDE}"]\n'
        f'transcript_root = "{fake_home}/projects"\n'
        f"{extra}"
    )


# ------------------------------------------------------------------ scripting


def name_of(run_id: str, gid: str, role: str, generation: int = 1) -> str:
    return f"{run_id}-{gid}-{role}-g{generation}"


def script_session(fake_home: Path, name: str, *entries: dict) -> None:
    with (fake_home / "scripts" / f"{name}.jsonl").open("a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def coder_entry(
    status: str = "completed",
    surprises: list[dict] | None = None,
    files: dict[str, str] | None = None,
    commit: str | None = None,
    question: str = "",
    **extra,
) -> dict:
    body: dict = {
        "status": status,
        "summary": "scripted round",
        "verification_results": [{"item_id": "v1", "status": "pass", "notes": ""}],
        "surprises": surprises or [],
    }
    if question:
        body["question"] = question
    entry: dict = {
        "result": f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>',
        **extra,
    }
    if files:
        entry["files"] = files
    if commit:
        entry["commit"] = commit
    return entry


def verdict_entry(status: str = "approved", changes: list[str] | None = None, **extra) -> dict:
    body = {"status": status, "required_changes": changes or [], "surprises": [], "notes": ""}
    return {
        "result": f'<run-report status="{status}">\n{json.dumps(body)}\n</run-report>',
        **extra,
    }


def calls_of(fake_home: Path) -> list[dict]:
    path = fake_home / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def named_calls(fake_home: Path, name: str) -> list[dict]:
    return [c for c in calls_of(fake_home) if _flag(c["argv"], "--name") == name]


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def state_of(repo: Path, run_id: str) -> dict:
    return json.loads((repo / ".orchestrator" / "runs" / run_id / "state.json").read_text())


def manifest_of(repo: Path, run_id: str) -> dict:
    return json.loads((repo / ".orchestrator" / "runs" / run_id / "manifest.json").read_text())


# ------------------------------------------------------------------ scenarios


def test_full_run_happy_path_with_warm_rejection(repo, fake_home, capsys):
    """plan → group → run → merge, with one reviewer reject-then-approve warm
    round; asserts AE6 (manifest↔transcript join) and the identity-block contract."""
    write_config(repo, fake_home)
    exit_code = main(
        ["group", str(repo / "plan.md"), "--repo", str(repo)],
        llm_runner=StubLlm(),
        client=CodegraphClient(repo_root=repo, runner=codegraph_response),
    )
    assert exit_code == 0
    grouping = json.loads(
        (repo / ".orchestrator" / "groupings" / "plan" / "groups.json").read_text()
    )
    gids = [group["id"] for group in grouping["groups"]]
    assert gids  # the toy plan produced at least one group

    run_id = "r1"
    for index, gid in enumerate(gids):
        commit = {"files": {f"{gid}.out": f"work of {gid}\n"}, "commit": f"{gid}: scripted work"}
        if index == 0:  # first group: reviewer rejects once, then approves warm
            script_session(
                fake_home, name_of(run_id, gid, "coder"), coder_entry(**commit), coder_entry()
            )
            script_session(
                fake_home,
                name_of(run_id, gid, "reviewer"),
                verdict_entry("changes_required", ["tighten the tests"]),
                verdict_entry("approved"),
            )
        else:
            script_session(fake_home, name_of(run_id, gid, "coder"), coder_entry(**commit))
            script_session(fake_home, name_of(run_id, gid, "reviewer"), verdict_entry("approved"))

    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--review-intensity", "paired"],
        llm_runner=StubLlm(),
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "all groups completed" in out

    # every group completed at generation 1; the warm reject never respawned
    state = state_of(repo, run_id)
    assert all(entry["state"] == "completed" for entry in state["groups"].values())
    assert all(entry["generation"] == 1 for entry in state["groups"].values())
    assert state["live_pids"] == {}

    # AE6: the manifest joins every session to a transcript the stub produced,
    # with names and summaries present
    manifest = manifest_of(repo, run_id)
    assert manifest["base_session_id"]
    assert set(manifest["groups"]) == set(gids)
    for gid in gids:
        entry = manifest["groups"][gid]
        assert entry["group_name"] and entry["summary"]
        roles = [session["role"] for session in entry["sessions"]]
        assert roles == ["coder", "reviewer"]
        for session in entry["sessions"]:
            assert session["name"].startswith(f"{run_id}-{gid}-")
            assert session["transcript_path"] is not None
            assert Path(session["transcript_path"]).is_file()
        assert all(session["retirement_reason"] is None for session in entry["sessions"])

    # identity-block contract: every worker fork's first prompt opens with the
    # <run-manifest> block (never an injected-looking prefix), and workers ran in
    # worktrees whose paths keep the repo dir name as a substring
    forks = [call for call in calls_of(fake_home) if "--fork-session" in call["argv"]]
    assert len(forks) == 2 * len(gids)
    for call in forks:
        assert call["prompt"].startswith(f'<run-manifest run_id="{run_id}"')
        assert REPO_DIR_NAME in call["cwd"] and ".worktrees" in call["cwd"]

    # the base session was created once, from the repo root, named <run_id>-base
    base_calls = named_calls(fake_home, f"{run_id}-base")
    assert len(base_calls) == 1
    assert "--fork-session" not in base_calls[0]["argv"]

    # one --no-ff merge commit per group on the integration branch; clean group
    # worktrees were removed after merging
    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    for gid in gids:
        assert f"merge({run_id}): {gid}" in log
    remaining = [p.name for p in (repo / ".worktrees").iterdir()]
    assert remaining == [f"run-{run_id}-integration"]

    # verdict artifacts of the warm loop persisted round-by-round
    group_dir = repo / ".orchestrator" / "runs" / run_id / "groups" / gids[0]
    assert (group_dir / "verdict-g1-r1.json").is_file()
    assert (group_dir / "verdict-g1-r2.json").is_file()

    # the run snapshotted its DAG (ADR 0002): `.orchestrator/groups.json` is shared
    # and rewritten by every planning cycle, so the Observatory reads this copy
    snapshot_path = repo / ".orchestrator" / "runs" / run_id / "groups.json"
    assert snapshot_path.is_file()
    snapshot = GroupingResult.model_validate_json(snapshot_path.read_text())
    assert [group.id for group in snapshot.groups] == gids

    # status reports the finished run
    exit_code = main(["status", run_id, "--repo", str(repo)])
    assert exit_code == 0
    status_out = capsys.readouterr().out
    assert f"{gids[0]}: completed" in status_out
    assert name_of(run_id, gids[0], "coder") in status_out


def test_group_cli_premapped_greenfield_plan_skips_mapper_and_orders_groups(repo, capsys):
    """The smoke1 scenario, automated: `group` on a pre-mapped greenfield plan
    produces dependency-ordered groups.json with the mapper LLM skipped — no
    hand-editing of groups.json required."""
    from test_grouper_pipeline import GREENFIELD_PLAN as greenfield_plan

    plan = repo / "greenfield-plan.md"
    plan.write_text(greenfield_plan)
    llm = StubLlm()
    exit_code = main(
        ["group", str(plan), "--repo", str(repo)],
        llm_runner=llm,
        client=CodegraphClient(repo_root=repo, runner=codegraph_response),
    )
    assert exit_code == 0
    grouping = json.loads(
        (repo / ".orchestrator" / "groupings" / "greenfield-plan" / "groups.json").read_text()
    )

    # the flags record the deterministic fast path and the prospective files
    assert any("mapper LLM skipped" in flag for flag in grouping["flags"])
    assert any("retained as prospective" in flag for flag in grouping["flags"])
    assert [title for title, _ in llm.prompts if title == "mapper_output"] == []

    # groups.json dependencies realize the plan's depends_on: every group not
    # holding the scaffold depends on the scaffold's group
    by_task = {task: group for group in grouping["groups"] for task in group["tasks"]}
    assert by_task["t2-items-api"]["id"] == by_task["t3-items-ui"]["id"]
    scaffold_gid = by_task["t1-scaffold"]["id"]
    for group in grouping["groups"]:
        if group["id"] != scaffold_gid:
            assert scaffold_gid in group["dependencies"]

    # prospective files ship in Group.files so workers create them
    assert "app/items.py" in by_task["t2-items-api"]["files"]


def test_breaker_trip_respawns_generation_two_and_completes(repo, fake_home, capsys):
    run_id = "r2"
    write_run_artifacts(repo, [make_group("g1")])
    write_config(repo, fake_home, "[breaker]\nmax_rounds_per_generation = 1\nmax_generations = 2\n")
    script_session(fake_home, name_of(run_id, "g1", "coder"), coder_entry())
    script_session(
        fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("changes_required", ["fix y"])
    )
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder", generation=2),
        coder_entry(files={"g1.out": "done\n"}, commit="g1: generation-2 work"),
    )
    script_session(
        fake_home, name_of(run_id, "g1", "reviewer", generation=2), verdict_entry("approved")
    )

    exit_code = main(["run", "--repo", str(repo), "--run-id", run_id], llm_runner=StubLlm())
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g1"]["generation"] == 2

    manifest = manifest_of(repo, run_id)
    sessions = {session["name"]: session for session in manifest["groups"]["g1"]["sessions"]}
    retired = sessions[name_of(run_id, "g1", "coder")]
    assert retired["retirement_reason"] and "round threshold" in retired["retirement_reason"]
    # the generation-2 coder was forked fresh from base with a condensed handoff
    handoff = named_calls(fake_home, name_of(run_id, "g1", "coder", generation=2))[0]
    assert "generation 2" in handoff["prompt"]
    assert "fix y" in handoff["prompt"]


def test_surprise_rewrites_dependent_group_before_launch(repo, fake_home):
    run_id = "r3"
    write_run_artifacts(
        repo,
        [
            make_group("g1", files=["g1.txt"]),
            make_group("g2", dependencies=["g1"]),
        ],
    )
    write_config(repo, fake_home)
    surprise = {
        "kind": "interface_mismatch",
        "description": "g1 renamed the shared helper API",
        "affected_groups": ["g2"],
    }
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(surprises=[surprise], files={"g1.txt": "one\n"}, commit="g1: work"),
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"g2.txt": "two\n"}, commit="g2: work"),
    )
    script_session(fake_home, name_of(run_id, "g2", "reviewer"), verdict_entry("approved"))

    stub = StubLlm()
    exit_code = main(["run", "--repo", str(repo), "--run-id", run_id], llm_runner=stub)
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert state["groups"]["g2"]["state"] == "completed"
    assert state["groups"]["g2"]["generation"] == 1  # rewritten before launch, no respawn

    # the speccer rewrite saw the surprise as context, and the relaunched coder
    # got the rewritten spec
    speccer_prompts = [prompt for title, prompt in stub.prompts if title == "speccer_output"]
    assert len(speccer_prompts) == 1
    assert "g1 renamed the shared helper API" in speccer_prompts[0]
    coder_prompt = named_calls(fake_home, name_of(run_id, "g2", "coder"))[0]["prompt"]
    assert "Full spec for g2." in coder_prompt


def test_merge_conflict_routes_group_through_rewrite_to_completion(repo, fake_home):
    run_id = "r4"
    write_run_artifacts(
        repo,
        [
            # Deliberately *disjoint* declarations, even though both coders go on
            # to write conflict.txt: plan U9's exclusion keys off declared files,
            # so two groups declaring conflict.txt could no longer run at once
            # and the race below would be impossible to stage. An undeclared
            # collision like this one is exactly what U9 cannot prevent — and
            # what the conflict→rewrite path still has to catch.
            make_group("g1", files=["g1.out"]),
            make_group("g2", files=["g2.out"]),
        ],
    )
    write_config(repo, fake_home)
    # g1 races ahead and merges conflict.txt first; g2's revision round is delayed
    # so its (approved) merge attempt happens strictly after g1's merge landed
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"conflict.txt": "g1 version\n"}, commit="g1: claim the file"),
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"conflict.txt": "g2 version\n"}, commit="g2: claim the file"),
        coder_entry(delay_s=1.5),
    )
    script_session(
        fake_home,
        name_of(run_id, "g2", "reviewer"),
        verdict_entry("changes_required", ["stall one round"]),
        verdict_entry("approved"),
    )
    # after the conflict the rewritten g2 respawns at generation 2 and lands a
    # version identical to the integration side (add/add resolves cleanly)
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder", generation=2),
        coder_entry(
            files={"conflict.txt": "g1 version\n", "g2.out": "g2 done\n"},
            commit="g2: align with integration",
        ),
    )
    script_session(
        fake_home, name_of(run_id, "g2", "reviewer", generation=2), verdict_entry("approved")
    )

    stub = StubLlm()
    # Concurrency>1 is required for the race: under the serial default the groups
    # stack (g2 branches from the tip that already has g1's conflict.txt) and no
    # conflict ever arises — which is the whole point of serial. Force parallel to
    # exercise the conflict→rewrite path. Escalation defaults on (plan U2); this
    # scenario tests the conflict→rewrite path itself, not HITL, so it stays headless.
    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            run_id,
            "--concurrency",
            "2",
            "--intensity",
            "autonomous",
        ],
        llm_runner=stub,
    )
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g2"]["state"] == "completed"
    assert state["groups"]["g2"]["generation"] == 2

    # the rewrite saw the conflict as escalation context
    speccer_prompts = [prompt for title, prompt in stub.prompts if title == "speccer_output"]
    assert len(speccer_prompts) == 1
    assert "merge_conflict" in speccer_prompts[0] and "conflict.txt" in speccer_prompts[0]

    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g1" in log and f"merge({run_id}): g2" in log
    integration = git(repo, "show", f"orchestrator/run-{run_id}:conflict.txt")
    assert integration == "g1 version\n"


def test_reject_forever_fails_group_and_strands_dependent(repo, fake_home, capsys):
    run_id = "r5"
    write_run_artifacts(repo, [make_group("g1"), make_group("g2", dependencies=["g1"])])
    write_config(repo, fake_home, "[breaker]\nmax_rounds_per_generation = 1\nmax_generations = 1\n")
    script_session(fake_home, name_of(run_id, "g1", "coder"), coder_entry())
    script_session(
        fake_home,
        name_of(run_id, "g1", "reviewer"),
        verdict_entry("changes_required", ["never good enough"]),
    )

    # Escalation defaults on (plan U2); this scenario tests the reject-forever /
    # generation-cap path itself, not HITL, so it stays headless.
    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            run_id,
            "--sequential",
            "--intensity",
            "autonomous",
        ],
        llm_runner=StubLlm(),
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "did not complete" in err
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "failed"
    assert "generation cap" in state["groups"]["g1"]["failure"]
    assert state["groups"]["g2"]["state"] == "pending"  # stranded, never launched
    assert named_calls(fake_home, name_of(run_id, "g2", "coder")) == []


def test_resume_completes_interrupted_run_without_new_base_session(repo, fake_home):
    run_id = "r6"
    groups = [
        make_group("g1", intensity=ReviewIntensity.SELF_VERIFY),
        make_group("g2", intensity=ReviewIntensity.SELF_VERIFY),
    ]
    write_run_artifacts(repo, groups)
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "one\n"}, commit="g1: work"),
    )
    # g2's coder dies at fork — an envelope failure: the group lands interrupted
    # (non-terminal) and the run exits 2, stopped-but-resumable (R1–R3)
    script_session(
        fake_home, name_of(run_id, "g2", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )
    exit_code = main(["run", "--repo", str(repo), "--run-id", run_id], llm_runner=StubLlm())
    assert exit_code == 2
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g2"]["state"] == "interrupted"

    # no state surgery needed: plain `resume` relaunches interrupted groups —
    # just give g2's coder a fresh script
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"g2.out": "two\n"}, commit="g2: work"),
    )

    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g2"]["state"] == "completed"
    # plan U8: g2's earlier interrupted-attempt failure text must not survive
    # into its later successful completion — `status` would otherwise print a
    # stale failure line for a group recorded as completed.
    assert state["groups"]["g2"].get("failure") is None

    # the resumed run reused the original base session instead of starting one
    base_calls = named_calls(fake_home, f"{run_id}-base")
    assert len(base_calls) == 1
    manifest = manifest_of(repo, run_id)
    assert manifest["base_session_id"]
    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g1" in log and f"merge({run_id}): g2" in log


# ------------------------------------------------------------- named groupings


def test_run_snapshots_the_named_grouping_and_records_it_in_the_manifest(repo, fake_home):
    """Plan U10: `run --grouping alpha` copies alpha's files into the run
    directory and records which grouping it used in manifest.json."""
    run_id = "r-snap"
    write_run_artifacts(
        repo, [make_group("g1", intensity=ReviewIntensity.SELF_VERIFY)], name="alpha"
    )
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "x\n"}, commit="g1: work"),
    )
    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--grouping", "alpha"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 0
    run_dir = repo / ".orchestrator" / "runs" / run_id
    assert (run_dir / "groups.json").is_file()
    assert (run_dir / "base-context.md").is_file()
    assert manifest_of(repo, run_id)["grouping"] == "alpha"


def test_resume_after_regroup_uses_the_run_snapshot_not_the_live_grouping(repo, fake_home):
    """Plan U10 (ADR 0002): re-running `group --name alpha` against a different
    plan must not be able to rewrite a run that already started from it —
    `resume` schedules the groups the run began with, from its own snapshot."""
    from test_grouper_pipeline import MIXED_PLAN

    run_id = "r7"
    groups = [
        make_group("g1", intensity=ReviewIntensity.SELF_VERIFY),
        make_group("g2", intensity=ReviewIntensity.SELF_VERIFY),
    ]
    write_run_artifacts(repo, groups, name="alpha")
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "one\n"}, commit="g1: work"),
    )
    script_session(
        fake_home, name_of(run_id, "g2", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )
    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--grouping", "alpha"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 2
    assert set(state_of(repo, run_id)["groups"]) == {"g1", "g2"}

    # `group --name alpha` re-run against an unrelated plan overwrites the live
    # grouping directory
    other_plan = repo / "other-plan.md"
    other_plan.write_text(MIXED_PLAN)
    regroup_exit = main(
        ["group", str(other_plan), "--repo", str(repo), "--name", "alpha"],
        llm_runner=StubLlm(),
        client=CodegraphClient(repo_root=repo, runner=codegraph_response),
    )
    assert regroup_exit == 0
    live_grouping = json.loads(
        (repo / ".orchestrator" / "groupings" / "alpha" / "groups.json").read_text()
    )
    live_tasks = {task for group in live_grouping["groups"] for task in group["tasks"]}
    assert live_tasks == {"t1-api", "t2-ui"}  # the live directory really did change

    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"g2.out": "two\n"}, commit="g2: work"),
    )
    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert set(state["groups"]) == {"g1", "g2"}  # the resumed run kept its own snapshot


# ------------------------------------------------------------------ HITL (Phase D)


def _drive_escalations(
    paths: RunPaths, thread: threading.Thread, plan: dict[str, tuple[str, str]]
) -> list[str]:
    """Background 'operator' mirroring the main-session supervision loop: while the
    run thread is alive, answer each new pending escalation per `plan` (keyed by
    escalation kind → (action, text))."""
    handled: list[str] = []
    seen: set[str] = set()
    deadline = time.monotonic() + 20.0
    while (thread.is_alive() or pending_escalations(paths)) and time.monotonic() < deadline:
        for request in pending_escalations(paths):
            if request.id in seen:
                continue
            action, text = plan.get(request.kind.value, ("answer", ""))
            atomic_write_text(
                paths.escalations_dir / f"response-{request.id}.json",
                EscalationResponse(
                    id=request.id, action=HumanAction(action), answer=text
                ).model_dump_json(),
            )
            seen.add(request.id)
            handled.append(request.kind.value)
        time.sleep(0.02)
    return handled


def test_hitl_answer_a_question_then_skip_a_too_hard_group(repo, fake_home):
    """The live supervision loop, driven deterministically: a coder `needs_input`
    question is answered (coder resumes and completes) and a reviewer `too_hard`
    group is skipped (fails, run continues)."""
    run_id = "rh"
    write_run_artifacts(repo, [make_group("g1"), make_group("g2")])
    write_config(repo, fake_home, "[escalation]\npoll_interval_s = 0.02\n")

    # g1: coder asks a question, then after the answer-resume, commits and completes
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(status="needs_input", question="Which serializer should I use?"),
        coder_entry(files={"g1.out": "done\n"}, commit="g1: work"),
    )
    script_session(fake_home, name_of(run_id, "g1", "reviewer"), verdict_entry("approved"))
    # g2: coder completes, reviewer says too_hard → operator skips the group
    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"g2.out": "done\n"}, commit="g2: work"),
    )
    script_session(fake_home, name_of(run_id, "g2", "reviewer"), verdict_entry("too_hard"))

    paths = RunPaths(repo, run_id)
    outcome: dict = {}

    def run() -> None:
        outcome["code"] = main(
            ["run", "--repo", str(repo), "--run-id", run_id, "--hitl", "--sequential"],
            llm_runner=StubLlm(),
        )

    thread = threading.Thread(target=run)
    thread.start()
    handled = _drive_escalations(
        paths,
        thread,
        {
            "coder_question": ("answer", "use the JSON serializer"),
            "reviewer_too_hard": ("skip", ""),
            # plan U2: skipping g2 raises a follow-up group_resolve escalation
            # for its committed-but-unreviewed work; the operator declines that
            # too, so g2 stays failed rather than getting silently resolved.
            "group_resolve": ("skip", ""),
        },
    )
    thread.join(timeout=25)
    assert not thread.is_alive()

    # g1 completed after the answer; g2 failed after the skip; run continued
    assert outcome["code"] == 1
    state = state_of(repo, run_id)
    assert state["groups"]["g1"]["state"] == "completed"
    assert state["groups"]["g2"]["state"] == "failed"
    assert "operator skipped" in state["groups"]["g2"]["failure"]
    assert set(handled) == {"coder_question", "reviewer_too_hard", "group_resolve"}

    # the coder was resumed warm with the operator's answer (resume rounds carry no
    # --name, so search every call's prompt), and no extra session was forked
    assert any("use the JSON serializer" in call["prompt"] for call in calls_of(fake_home))
    g1_sessions = manifest_of(repo, run_id)["groups"]["g1"]["sessions"]
    assert [s["role"] for s in g1_sessions] == ["coder", "reviewer"]

    # the request/response artifacts and the event log are readable
    esc_dir = repo / ".orchestrator" / "runs" / run_id / "escalations"
    assert list(esc_dir.glob("request-*.json")) and list(esc_dir.glob("response-*.json"))
    run_log = (repo / ".orchestrator" / "runs" / run_id / "logs" / "run.log").read_text()
    assert "ESCALATION" in run_log and "answered" in run_log


def test_intensity_autonomous_flag_stays_headless(repo, fake_home):
    """`--intensity autonomous` runs exactly like a no-flag run: no *escalation*
    artifacts — the injected seam is fully absent. The lifecycle log is always
    on (R10), so run.log exists even here, but it carries no escalation lines."""
    run_id = "ra"
    write_run_artifacts(repo, [make_group("g1", intensity=ReviewIntensity.SELF_VERIFY)])
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "x\n"}, commit="g1: work"),
    )
    exit_code = main(
        ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"],
        llm_runner=StubLlm(),
    )
    assert exit_code == 0
    assert state_of(repo, run_id)["groups"]["g1"]["state"] == "completed"
    run_dir = repo / ".orchestrator" / "runs" / run_id
    assert not (run_dir / "escalations").exists()
    run_log = (run_dir / "logs" / "run.log").read_text()
    assert "ESCALATION" not in run_log  # escalation behaviour itself is unchanged
    assert f"run {run_id} started (autonomous)" in run_log
    assert "group g1: completed" in run_log  # lifecycle events in autonomous mode (R10)


def test_overlapping_groups_are_serialized_end_to_end_and_both_land(repo, fake_home):
    """Plan U9, through the whole stack: two groups declaring shared.py at
    --concurrency 4 are held apart while a third, sharing nothing, is not — and
    every group's work still reaches the integration branch.

    The assertion is on the run log rather than on wall-clock spans: the session
    runner serializes *forks* regardless (fake_claude's fork.lock proves it), so
    coder call spans never overlap here whether U9 is in force or not.
    """
    run_id = "r9"
    write_run_artifacts(
        repo,
        [
            make_group("g1", files=["shared.py", "one.py"]),
            make_group("g2", files=["shared.py", "two.py"]),
            make_group("g3", files=["three.py"]),
        ],
    )
    write_config(repo, fake_home)
    for gid in ("g1", "g2", "g3"):
        script_session(
            fake_home,
            name_of(run_id, gid, "coder"),
            coder_entry(delay_s=0.3, files={f"{gid}.out": f"{gid}\n"}, commit=f"{gid}: work"),
        )
        script_session(fake_home, name_of(run_id, gid, "reviewer"), verdict_entry("approved"))

    exit_code = main(
        [
            "run",
            "--repo",
            str(repo),
            "--run-id",
            run_id,
            "--concurrency",
            "4",
            "--intensity",
            "autonomous",
        ],
        llm_runner=StubLlm(),
    )
    assert exit_code == 0
    state = state_of(repo, run_id)
    assert {gid: state["groups"][gid]["state"] for gid in ("g1", "g2", "g3")} == {
        "g1": "completed",
        "g2": "completed",
        "g3": "completed",
    }

    run_log = (repo / ".orchestrator" / "runs" / run_id / "logs" / "run.log").read_text()
    # Whichever of the pair was admitted first holds the other — no ordering
    # between them is required, so either line satisfies the invariant.
    assert (
        "group g2: held (file_overlap) by g1 on shared.py" in run_log
        or "group g1: held (file_overlap) by g2 on shared.py" in run_log
    )
    # g3 shares nothing: the exclusion must not have held it back too.
    assert "group g3: held" not in run_log

    # Whichever order was chosen, every group's work reached integration.
    log = git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert all(f"merge({run_id}): {gid}" in log for gid in ("g1", "g2", "g3"))
