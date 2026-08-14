"""The launch surface: option→flag translation, the double-launch guard, jobs.

``build_argv`` is where nearly all the risk lives and it is pure, so most of
this file is a table test. ``spawn_job`` is stubbed everywhere except the one
test that proves a real detached child writes its log — starting real
schedulers from a unit test would be neither fast nor honest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.observatory import launch
from orchestrator.observatory.app import create_app
from orchestrator.observatory.launch import (
    ConflictError,
    ExecutionOptions,
    GroupJobBody,
    ResumeJobBody,
    RunJobBody,
    build_argv,
    check_not_live,
    list_plans,
    read_job,
    spawn_job,
)
from tests.test_observatory_api import write_registry


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "docs" / "plans").mkdir(parents=True)
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    registry = write_registry(tmp_path, [("proj", repo)])
    return TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))


def write_state(repo: Path, run_id: str, **fields) -> None:
    run_dir = repo / ".orchestrator" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps({"run_id": run_id, "groups": {}, **fields}))


# ------------------------------------------------------------------- build_argv


class TestBuildArgv:
    def test_it_runs_this_interpreter_unbuffered(self, repo):
        argv = build_argv("group", GroupJobBody(plan="docs/plans/p.md"), repo=repo)
        assert argv[:5] == [sys.executable, "-u", "-m", "orchestrator.cli", "group"]
        # `-u` is not cosmetic: the console-script entry point cannot pass it, and
        # a block-buffered job log reads exactly like a hung job (finding P5).
        assert "-u" in argv
        assert argv[-2:] == ["--repo", str(repo)]

    def test_group_options_map_to_their_flags(self, repo):
        argv = build_argv(
            "group",
            GroupJobBody(
                plan="docs/plans/p.md",
                name="mine",
                granularity="balanced",
                token_budget=50_000,
                dry_run=True,
                auto_resume=False,
            ),
            repo=repo,
        )
        assert "docs/plans/p.md" in argv
        assert argv[argv.index("--name") + 1] == "mine"
        assert argv[argv.index("--granularity") + 1] == "balanced"
        assert argv[argv.index("--token-budget") + 1] == "50000"
        assert "--dry-run" in argv
        assert "--no-auto-resume" in argv

    def test_every_execution_option_has_a_flag(self, repo):
        """One-for-one with ``cli._add_execution_args``. The moment this surface
        and the CLI disagree, the UI and the terminal produce different runs from
        the same intent."""
        argv = build_argv(
            "run",
            RunJobBody(
                grouping="g",
                run_id="r1",
                options=ExecutionOptions(
                    sequential=True,
                    concurrency=3,
                    permission_mode="acceptEdits",
                    review_intensity="paired",
                    hitl=True,
                    intensity="on_stuck",
                    escalation_source="orchestrator_only",
                    escalation_timeout=90.0,
                    auto_resume=True,
                ),
            ),
            repo=repo,
        )
        assert argv[argv.index("--grouping") + 1] == "g"
        assert argv[argv.index("--run-id") + 1] == "r1"
        assert "--sequential" in argv
        assert argv[argv.index("--concurrency") + 1] == "3"
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert argv[argv.index("--review-intensity") + 1] == "paired"
        assert "--hitl" in argv
        assert argv[argv.index("--intensity") + 1] == "on_stuck"
        assert argv[argv.index("--escalation-source") + 1] == "orchestrator_only"
        assert argv[argv.index("--escalation-timeout") + 1] == "90.0"
        assert "--auto-resume" in argv

    def test_unspecified_options_emit_no_flags_at_all(self, repo):
        """``None`` means "not specified", never "off" — the CLI must be left to
        resolve it from the config file exactly as it does for an omitted flag."""
        argv = build_argv("run", RunJobBody(), repo=repo)
        assert argv == [sys.executable, "-u", "-m", "orchestrator.cli", "run", "--repo", str(repo)]

    def test_resume_takes_the_run_id_positionally(self, repo):
        argv = build_argv(
            "resume",
            ResumeJobBody(run_id="r1", options=ExecutionOptions(intensity="interactive")),
            repo=repo,
        )
        assert argv[5] == "r1"
        assert argv[argv.index("--intensity") + 1] == "interactive"

    def test_the_argv_is_a_list_never_a_shell_string(self, repo):
        """A plan path with a space in it must not become two arguments, and
        nothing here may ever reach a shell."""
        argv = build_argv("group", GroupJobBody(plan="docs/my plan.md"), repo=repo)
        assert "docs/my plan.md" in argv

    def test_every_execution_options_field_is_translated(self):
        """A field added to the model without a flag would be silently dropped —
        the request would look accepted and the run would ignore it."""
        every = ExecutionOptions(
            sequential=True,
            concurrency=1,
            permission_mode="plan",
            review_intensity="paired",
            hitl=True,
            intensity="on_failure",
            escalation_source="workers_via_orchestrator",
            escalation_timeout=1.0,
            auto_resume=False,
        )
        emitted = " ".join(every.to_argv())
        for field in ExecutionOptions.model_fields:
            flag = "--" + field.replace("_", "-")
            assert flag in emitted or f"--no-{field.replace('_', '-')}" in emitted, (
                f"ExecutionOptions.{field} has no flag in to_argv()"
            )


# ------------------------------------------------------------- double-launch


class TestDoubleLaunchGuard:
    def test_a_live_run_refuses_a_second_scheduler(self, repo):
        write_state(repo, "r1", live_pids={"4242": "coder g1"}, interrupted_at=None)
        with pytest.raises(ConflictError):
            check_not_live(repo, "r1")

    def test_an_interrupted_run_may_be_resumed_even_with_stale_pids(self, repo):
        """Nothing clears ``live_pids`` on a crash, so pids alone would make every
        crashed run permanently un-resumable — the interruption mark is what says
        no scheduler is driving it."""
        write_state(
            repo, "r1", live_pids={"4242": "coder g1"}, interrupted_at="2026-08-13T10:00:00"
        )
        check_not_live(repo, "r1")

    def test_a_run_with_no_recorded_pids_is_launchable(self, repo):
        write_state(repo, "r1", live_pids={})
        check_not_live(repo, "r1")

    def test_an_unknown_run_is_not_a_conflict(self, repo):
        check_not_live(repo, "never-existed")
        check_not_live(repo, None)

    def test_the_endpoint_answers_409(self, client, repo, monkeypatch):
        write_state(repo, "r1", live_pids={"4242": "coder g1"})
        monkeypatch.setattr(launch, "spawn_job", _never_spawn)
        response = client.post("/api/projects/proj/jobs/resume", json={"run_id": "r1"})
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]


def _never_spawn(*args, **kwargs):  # pragma: no cover — asserts by being called
    raise AssertionError("a refused launch must not spawn anything")


# ------------------------------------------------------------------ discovery


class TestPlans:
    def test_plans_are_listed_with_title_and_relative_path(self, client, repo):
        (repo / "docs" / "plans" / "one.md").write_text("# The First Plan\n\nbody\n")
        body = client.get("/api/projects/proj/plans").json()
        assert body[0]["path"] == "docs/plans/one.md"
        assert body[0]["title"] == "The First Plan"
        assert body[0]["modified_at"]

    def test_a_plan_without_a_heading_still_lists(self, repo):
        (repo / "docs" / "plans" / "bare.md").write_text("no heading here\n")
        assert list_plans(repo)[0].title == "bare"

    def test_a_repo_with_no_plans_is_an_empty_list(self, client):
        assert client.get("/api/projects/proj/plans").json() == []

    def test_an_unknown_project_is_a_404(self, client):
        assert client.get("/api/projects/nope/plans").status_code == 404


class TestGroupings:
    def test_groupings_come_from_the_shared_describe_helper(self, client, repo):
        directory = repo / ".orchestrator" / "groupings" / "mine"
        directory.mkdir(parents=True)
        (directory / "groups.json").write_text(
            json.dumps({"plan_path": "docs/plans/one.md", "groups": []})
        )
        body = client.get("/api/projects/proj/groupings").json()
        assert body == [{"name": "mine", "plan_path": "docs/plans/one.md", "group_count": 0}]

    def test_no_groupings_is_an_empty_list(self, client):
        assert client.get("/api/projects/proj/groupings").json() == []


# ----------------------------------------------------------------------- jobs


class TestJobs:
    def test_a_spawned_job_records_its_command_and_writes_a_log(self, repo):
        """The one test that spawns for real — with a trivial argv, so it proves
        the plumbing (detached child, merged streams, recorded pid) rather than
        the orchestrator."""
        repo.mkdir(parents=True, exist_ok=True)
        info = spawn_job(repo, [sys.executable, "-c", "print('hello job')"], "group")
        assert info.pid
        Path(info.log_path).parent  # exists by construction
        _wait_for(lambda: "hello job" in Path(info.log_path).read_text())

        stored = read_job(repo, info.job_id)
        assert stored is not None
        assert stored.argv == info.argv
        assert stored.kind == "group"
        # `running` is refreshed from the pid at read time, and this child is done.
        _wait_for(lambda: read_job(repo, info.job_id).running is False)

    def test_stderr_is_merged_into_the_same_log_as_stdout(self, repo):
        info = spawn_job(
            repo,
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            "group",
        )
        _wait_for(lambda: {"out", "err"} <= set(Path(info.log_path).read_text().split()))

    def test_an_unknown_job_is_a_404(self, client):
        assert client.get("/api/projects/proj/jobs/nope").status_code == 404

    def test_listing_a_repo_that_never_launched_anything_is_empty(self, client):
        assert client.get("/api/projects/proj/jobs").json() == []

    def test_posting_a_run_records_the_options_it_was_given(self, client, repo, monkeypatch):
        captured: dict = {}

        def fake_spawn(repo_arg, argv, kind, options=None):
            captured.update({"argv": argv, "kind": kind, "options": options})
            return launch.JobInfo(job_id="j1", kind=kind, argv=argv, pid=1, log_path="/dev/null")

        monkeypatch.setattr(launch, "spawn_job", fake_spawn)
        response = client.post(
            "/api/projects/proj/jobs/run",
            json={"grouping": "mine", "options": {"hitl": True, "intensity": "on_stuck"}},
        )
        assert response.status_code == 201
        assert "--intensity" in captured["argv"]
        # Echoed back so the job list can say what was launched without argv
        # archaeology, and so a form can be pre-filled from a past job.
        assert captured["options"]["options"]["intensity"] == "on_stuck"

    def test_job_log_stream_404s_for_an_unknown_job(self, client):
        response = client.get("/events/job", params={"project": "proj", "job": "nope"})
        assert response.status_code == 404


def _wait_for(predicate, timeout: float = 5.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, AttributeError):
            pass
        time.sleep(0.02)
    raise AssertionError("condition was never met")
