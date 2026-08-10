"""U2 tests: the app factory, the project registry, run discovery and the
composed run snapshot — plus R19 (one app, many projects) and the board/DAG half
of R20 (a finished run renders entirely from disk).

Every test reads from ``tmp_path`` or from the committed post-mortem fixture;
none touches ``$HOME`` and none needs a running orchestrator. That is the point:
the Observatory is a reader, and a run is a directory, not a process.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.scheduler import GroupRunState, GroupState, RunState
from orchestrator.model import GroupingResult
from orchestrator.observatory import artifacts, escalations, events, grouping, transcripts
from orchestrator.observatory.app import create_app
from orchestrator.observatory.runs import RUN_PREFIX

FIXTURE = Path(__file__).parent / "fixtures" / "observatory" / "run-postmortem"
MODERN_FIXTURE = Path(__file__).parent / "fixtures" / "observatory" / "run-modern"


# ------------------------------------------------------------------ fixtures


def install_run(repo: Path, run_id: str, source: Path = FIXTURE) -> Path:
    """Copy a finished run into a repo's ``.orchestrator/runs/<id>/``.

    ``.orchestrator/`` is gitignored, so the fixture cannot itself be a run
    directory — it is copied into place per test instead.
    """
    run_dir = repo / ".orchestrator" / "runs" / run_id
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, run_dir)
    state = json.loads((run_dir / "state.json").read_text())
    state["run_id"] = run_id
    (run_dir / "state.json").write_text(json.dumps(state, indent=2))
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["run_id"] = run_id
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return run_dir


def route_paths(routes) -> set[str]:
    """Flatten a route list to its paths. Included routers appear as wrapper
    objects — depending on the FastAPI version either carrying their own
    ``routes`` or holding the sub-router as ``original_router`` — so this
    recurses through both rather than assuming one flat level."""
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        nested = getattr(route, "routes", None)
        if nested:
            paths |= route_paths(nested)
        included = getattr(route, "original_router", None)
        if included is not None and getattr(included, "routes", None):
            paths |= route_paths(included.routes)
    return paths


def write_registry(tmp_path: Path, projects: list[tuple[str, Path | str]]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(
        yaml.safe_dump({"projects": [{"name": name, "repo": str(repo)} for name, repo in projects]})
    )
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    """One app over a single registered project holding the post-mortem run."""
    install_run(repo, "smoke1")
    registry = write_registry(tmp_path, [("proj", repo)])
    return TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))


# ------------------------------------------------------------------ projects


class TestProjects:
    def test_lists_registry_entries_in_file_order(self, tmp_path, repo):
        other = tmp_path / "second"
        other.mkdir()
        registry = write_registry(tmp_path, [("zeta", other), ("alpha", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        body = client.get("/api/projects").json()
        assert [entry["name"] for entry in body] == ["zeta", "alpha"]
        assert [entry["repo"] for entry in body] == [str(other), str(repo)]
        assert all(entry["error"] is None for entry in body)

    def test_missing_registry_file_is_an_empty_list_not_a_crash(self, tmp_path):
        client = TestClient(
            create_app(registry_path=tmp_path / "nope.yaml", dist_dir=tmp_path / "no-dist")
        )
        response = client.get("/api/projects")
        assert response.status_code == 200
        assert response.json() == []

    def test_a_repo_that_does_not_exist_is_reported_with_an_error(self, tmp_path, repo):
        registry = write_registry(tmp_path, [("good", repo), ("gone", tmp_path / "absent")])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        body = client.get("/api/projects").json()
        assert [entry["name"] for entry in body] == ["good", "gone"]  # kept, not dropped
        assert body[0]["error"] is None
        assert "does not exist" in body[1]["error"]

    def test_a_repo_that_is_a_file_is_reported_with_an_error(self, tmp_path):
        not_a_dir = tmp_path / "file.txt"
        not_a_dir.write_text("hi")
        registry = write_registry(tmp_path, [("bad", not_a_dir)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        body = client.get("/api/projects").json()
        assert "not a directory" in body[0]["error"]

    def test_malformed_yaml_is_surfaced_rather_than_raised(self, tmp_path):
        registry = tmp_path / "registry.yaml"
        registry.write_text("projects: [oops\n")
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        response = client.get("/api/projects")
        assert response.status_code == 200
        assert "YAML" in response.json()[0]["error"]

    def test_fallback_repo_serves_a_project_when_no_registry_exists(self, tmp_path, repo):
        client = TestClient(
            create_app(
                registry_path=tmp_path / "nope.yaml",
                fallback_repo=repo,
                dist_dir=tmp_path / "no-dist",
            )
        )
        body = client.get("/api/projects").json()
        assert [entry["name"] for entry in body] == ["proj"]

    def test_unknown_project_is_404(self, client):
        assert client.get("/api/projects/ghost/runs").status_code == 404


# ---------------------------------------------------------------------- runs


class TestRuns:
    def test_lists_run_ids_newest_first(self, tmp_path, repo):
        import os
        import time

        install_run(repo, "older")
        install_run(repo, "newer")
        now = time.time()
        os.utime(repo / ".orchestrator" / "runs" / "older" / "state.json", (now - 500, now - 500))
        os.utime(repo / ".orchestrator" / "runs" / "newer" / "state.json", (now, now))
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        body = client.get("/api/projects/proj/runs").json()
        assert [entry["run_id"] for entry in body] == ["newer", "older"]
        assert body[0]["updated_at"] is not None

    def test_absent_runs_dir_is_an_empty_list(self, tmp_path, repo):
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        response = client.get("/api/projects/proj/runs")
        assert response.status_code == 200
        assert response.json() == []

    def test_unknown_run_snapshot_is_404(self, client):
        assert client.get("/api/projects/proj/runs/ghost/snapshot").status_code == 404


# ------------------------------------------------------------------ snapshot


class TestSnapshot:
    def test_composes_states_sessions_and_dag_from_the_fixture(self, client):
        """R20 (board/DAG half): every card, the sessions join and the edges come
        off disk for a run with no live process."""
        body = client.get("/api/projects/proj/runs/smoke1/snapshot").json()
        assert body["run_id"] == "smoke1"
        assert body["project"] == "proj"
        assert body["stale_dag"] is False

        groups = {group["group_id"]: group for group in body["groups"]}
        assert set(groups) == {"g1", "g2"}
        assert groups["g1"]["state"] == "completed"
        assert groups["g1"]["generation"] == 1
        assert groups["g1"]["failure"] is None
        assert groups["g1"]["name"] == "types-sample-and-views"
        assert groups["g1"]["depends_on"] == ["g2"]

        # the manifest's groups→sessions join, with the fields the drill-in needs
        roles = [session["role"] for session in groups["g1"]["sessions"]]
        assert roles == ["coder", "reviewer"]
        coder = groups["g1"]["sessions"][0]
        assert coder["session_id"] and coder["name"].startswith("smoke1-g1-")
        assert coder["transcript_path"].endswith(".jsonl")

        assert body["edges"] == [{"from": "g2", "to": "g1"}]
        assert body["base_session_id"]
        assert body["plan_path"] == "frontend-plan.md"

    def test_prefers_the_per_run_snapshot_over_the_shared_file(self, tmp_path, repo, client):
        """The shared file is rewritten by every planning cycle; a run that has
        its own copy must ignore it (ADR 0002)."""
        shared = repo / ".orchestrator" / "groups.json"
        shared.write_text(GroupingResult(plan_path="other.md", groups=[]).model_dump_json(indent=2))
        body = client.get("/api/projects/proj/runs/smoke1/snapshot").json()
        assert body["stale_dag"] is False
        assert {group["group_id"] for group in body["groups"]} == {"g1", "g2"}

    def test_falls_back_to_the_shared_file_with_stale_dag(self, tmp_path, repo):
        run_dir = install_run(repo, "old")
        snapshot = json.loads((run_dir / "groups.json").read_text())
        (run_dir / "groups.json").unlink()  # a run from before the snapshot existed
        (repo / ".orchestrator" / "groups.json").write_text(json.dumps(snapshot))
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        body = client.get("/api/projects/proj/runs/old/snapshot").json()
        assert body["stale_dag"] is True
        assert body["edges"] == [{"from": "g2", "to": "g1"}]

    def test_no_dag_at_all_yields_empty_edges_and_stale_dag(self, tmp_path, repo):
        run_dir = install_run(repo, "bare")
        (run_dir / "groups.json").unlink()
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        response = client.get("/api/projects/proj/runs/bare/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["edges"] == []
        assert body["stale_dag"] is True
        # the board still renders: states and sessions do not come from the DAG
        assert {group["group_id"] for group in body["groups"]} == {"g1", "g2"}

    def test_the_snapshot_serves_the_manifest_grouping_and_escalation(self, tmp_path, repo):
        """The two fields the merge added to ``RunManifest``. Escalation config is
        the operator's worst-rated blind spot: without it there is no way to tell
        a run that never asks a human from one whose questions are going nowhere.
        Asserted against the modern fixture, which is the one that has them."""
        install_run(repo, "modern", source=MODERN_FIXTURE)
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        response = client.get("/api/projects/proj/runs/modern/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert (
            body["grouping"] == "2026-07-29-001-fix-orchestrator-correctness-and-measurement-plan"
        )
        assert body["escalation"] == {
            "enabled": True,
            "intensity": "on_stuck",
            "source": "workers_via_orchestrator",
            "timeout_s": None,
            "on_timeout": "autonomous",
            "poll_interval_s": 1.0,
        }

    def test_a_run_predating_those_fields_serves_them_as_null(self, client):
        """The post-mortem fixture has neither. Absent must read as absent, not as
        an error and not as a fabricated default."""
        body = client.get("/api/projects/proj/runs/smoke1/snapshot").json()
        assert body["grouping"] is None
        assert body["escalation"] is None


class TestStallEvidence:
    """Plan P3: the snapshot serves the *facts* a stall would be inferred from —
    heartbeat age, round start, transcript mtime — and never the inference. No
    run on disk today has a heartbeat, so absence is the common case and must
    read as null rather than as an error."""

    def test_a_run_without_a_heartbeat_serves_null_not_an_error(self, client):
        response = client.get("/api/projects/proj/runs/smoke1/snapshot")
        assert response.status_code == 200
        assert all(group["heartbeat"] is None for group in response.json()["groups"])

    def test_a_heartbeat_on_disk_is_served_as_written(self, tmp_path, repo):
        run_dir = install_run(repo, "beat")
        heartbeat = {
            "schema_version": 1,
            "group_id": "g1",
            "started_at": "2026-08-09T10:00:00.000+00:00",
            "generation": 2,
            "round": 3,
            "round_started_at": "2026-08-09T10:31:00.000+00:00",
            "updated_at": "2026-08-09T10:54:00.000+00:00",
        }
        group_dir = run_dir / "groups" / "g1"
        group_dir.mkdir(parents=True, exist_ok=True)
        (group_dir / "heartbeat.json").write_text(json.dumps(heartbeat))
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        groups = {
            g["group_id"]: g
            for g in client.get("/api/projects/proj/runs/beat/snapshot").json()["groups"]
        }
        assert groups["g1"]["heartbeat"] == {
            "started_at": "2026-08-09T10:00:00.000+00:00",
            "generation": 2,
            "round": 3,
            "round_started_at": "2026-08-09T10:31:00.000+00:00",
            "updated_at": "2026-08-09T10:54:00.000+00:00",
        }
        # 23 minutes of silence is for the client to interpret; the server never
        # says so, and there is no field here in which it could.
        assert "stalled" not in json.dumps(groups["g1"])
        assert groups["g2"]["heartbeat"] is None

    def test_a_torn_heartbeat_reads_as_absent_rather_than_500(self, tmp_path, repo):
        run_dir = install_run(repo, "torn")
        group_dir = run_dir / "groups" / "g1"
        group_dir.mkdir(parents=True, exist_ok=True)
        (group_dir / "heartbeat.json").write_text('{"round": ')
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        response = client.get("/api/projects/proj/runs/torn/snapshot")
        assert response.status_code == 200
        assert response.json()["groups"][0]["heartbeat"] is None

    def test_transcript_mtime_is_served_when_the_file_still_exists(self, tmp_path, repo):
        """The other half of the evidence, recorded by the runner all along and
        read by nobody until now."""
        run_dir = install_run(repo, "mtime")
        manifest = json.loads((run_dir / "manifest.json").read_text())
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("{}\n")
        manifest["groups"]["g1"]["sessions"][0]["transcript_path"] = str(transcript)
        manifest["groups"]["g1"]["sessions"][1]["transcript_path"] = str(tmp_path / "gone.jsonl")
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        groups = {
            g["group_id"]: g
            for g in client.get("/api/projects/proj/runs/mtime/snapshot").json()["groups"]
        }
        sessions = groups["g1"]["sessions"]
        assert sessions[0]["transcript_mtime"] is not None
        # A transcript that has been cleaned up is missing evidence, not an error.
        assert sessions[1]["transcript_mtime"] is None


class TestLivenessIndependence:
    """R9: a run is a directory, not a process. Nothing in the read path checks
    whether a recorded pid is alive."""

    def test_a_finished_run_reads_fully(self, client):
        body = client.get("/api/projects/proj/runs/smoke1/snapshot").json()
        assert [group["state"] for group in body["groups"]] == ["completed", "completed"]
        assert body["live_pids"] == {}

    def test_a_failed_group_reads_fully(self, tmp_path, repo):
        run_dir = install_run(repo, "failed")
        state = RunState(
            run_id="failed",
            groups={
                "g1": GroupRunState(
                    state=GroupState.FAILED, generation=3, failure="reviewer rejected 3x"
                ),
                "g2": GroupRunState(state=GroupState.COMPLETED),
            },
        )
        atomic_write_text(run_dir / "state.json", state.model_dump_json(indent=2))
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        response = client.get("/api/projects/proj/runs/failed/snapshot")
        assert response.status_code == 200
        groups = {group["group_id"]: group for group in response.json()["groups"]}
        assert groups["g1"]["state"] == "failed"
        assert groups["g1"]["generation"] == 3
        assert groups["g1"]["failure"] == "reviewer rejected 3x"

    def test_a_crashed_run_with_dead_pids_reads_fully(self, tmp_path, repo):
        """The orchestrator died mid-flight: a group is stuck in `running` and
        `live_pids` names a pid that is long gone."""
        run_dir = install_run(repo, "crashed")
        state = RunState(
            run_id="crashed",
            groups={
                "g1": GroupRunState(state=GroupState.RUNNING, generation=1),
                "g2": GroupRunState(state=GroupState.COMPLETED),
            },
            live_pids={999_999: "crashed-g1-coder-g1"},
        )
        atomic_write_text(run_dir / "state.json", state.model_dump_json(indent=2))
        registry = write_registry(tmp_path, [("proj", repo)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        response = client.get("/api/projects/proj/runs/crashed/snapshot")
        assert response.status_code == 200
        body = response.json()
        groups = {group["group_id"]: group for group in body["groups"]}
        assert groups["g1"]["state"] == "running"
        assert body["live_pids"] == {"999999": "crashed-g1-coder-g1"}
        assert len(body["groups"]) == 2  # the whole board, not a partial body


class TestMultiProject:
    """R19: one app instance spans projects — no restart, no leakage."""

    def test_two_projects_keep_their_own_runs_and_snapshots(self, tmp_path):
        first = tmp_path / "alpha-repo"
        second = tmp_path / "beta-repo"
        first.mkdir()
        second.mkdir()
        install_run(first, "alpha-run")
        install_run(second, "beta-run")
        registry = write_registry(tmp_path, [("alpha", first), ("beta", second)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))

        assert [entry["name"] for entry in client.get("/api/projects").json()] == ["alpha", "beta"]

        alpha_runs = [entry["run_id"] for entry in client.get("/api/projects/alpha/runs").json()]
        beta_runs = [entry["run_id"] for entry in client.get("/api/projects/beta/runs").json()]
        assert alpha_runs == ["alpha-run"]
        assert beta_runs == ["beta-run"]

        # each project's snapshot resolves under its own repo, on the same app
        alpha = client.get("/api/projects/alpha/runs/alpha-run/snapshot").json()
        beta = client.get("/api/projects/beta/runs/beta-run/snapshot").json()
        assert alpha["run_id"] == "alpha-run" and alpha["project"] == "alpha"
        assert beta["run_id"] == "beta-run" and beta["project"] == "beta"

        # a run id from one project is not reachable through the other
        assert client.get("/api/projects/beta/runs/alpha-run/snapshot").status_code == 404

    def test_a_project_added_to_the_registry_appears_without_a_restart(self, tmp_path):
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        registry = write_registry(tmp_path, [("one", first)])
        client = TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))
        assert len(client.get("/api/projects").json()) == 1

        write_registry(tmp_path, [("one", first), ("two", second)])
        assert [entry["name"] for entry in client.get("/api/projects").json()] == ["one", "two"]


# ------------------------------------------------------------- app assembly


class TestAppAssembly:
    def test_without_a_built_spa_the_root_names_the_dev_recipe(self, tmp_path):
        client = TestClient(
            create_app(registry_path=tmp_path / "nope.yaml", dist_dir=tmp_path / "no-dist")
        )
        response = client.get("/")
        assert response.status_code == 200
        assert "npm run dev" in response.json()["message"]

    def test_a_built_spa_is_mounted_and_the_api_still_resolves(self, tmp_path):
        dist = tmp_path / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!doctype html><title>Observatory</title>")
        client = TestClient(create_app(registry_path=tmp_path / "nope.yaml", dist_dir=dist))

        root = client.get("/")
        assert root.status_code == 200
        assert "Observatory" in root.text
        # the mount at "/" must not shadow the API
        assert client.get("/api/projects").json() == []

    def test_the_four_slice_routers_are_included(self, tmp_path):
        """The seam U4/U6/U8 fill: their routes reach the app with no edit here."""
        app = create_app(registry_path=tmp_path / "nope.yaml", dist_dir=tmp_path / "no-dist")
        app_paths = route_paths(app.routes)
        for module in (events, escalations, transcripts, artifacts):
            assert hasattr(module, "router")
            module_paths = route_paths(module.router.routes)
            assert module_paths <= app_paths, module.__name__


class TestGroupingRouter:
    """The Grouping tab's endpoints, from the app's side.

    ``test_observatory_grouping.py`` covers the read model in depth; what is
    asserted here is that the router is genuinely wired into the app — present
    in the OpenAPI schema, reachable over HTTP — and that the three sections the
    tab is built around arrive from a real ``grouping-trace.json``.
    """

    @pytest.fixture
    def client(self, tmp_path, repo):
        install_run(repo, "modern1", source=MODERN_FIXTURE)
        return TestClient(
            create_app(
                registry_path=tmp_path / "nope.yaml",
                fallback_repo=repo,
                dist_dir=tmp_path / "no-dist",
            )
        )

    def test_the_grouping_routes_are_registered_and_documented(self, tmp_path):
        app = create_app(registry_path=tmp_path / "nope.yaml", dist_dir=tmp_path / "no-dist")
        assert route_paths(grouping.router.routes) <= route_paths(app.routes)
        # The schema is what the client generates against: a route the app
        # serves but does not document is a route the frontend cannot see.
        documented = set(app.openapi()["paths"])
        for suffix in ("grouping", "grouping/llm", "grouping/llm/calls/{seq}"):
            assert f"{RUN_PREFIX}/{suffix}" in documented, suffix

    def test_the_stages_are_served_in_pipeline_order(self, client):
        """The stepper scrubs this list front to back, so its order is the story
        it tells. Serving it sorted is the server's job, not the client's."""
        body = client.get("/api/projects/proj/runs/modern1/grouping").json()
        served = [stage["stage"] for stage in body["stages"]]
        assert served == ["louvain", "lift", "split", "merge", "repair", "renumber"]
        assert body["pipeline_order"][0] == "louvain"
        # A real trace records stages as they ran, so ordering changed nothing.
        assert body["stages_reordered"] is False

    def test_the_scorecard_is_served_from_the_trace(self, client):
        scorecard = client.get("/api/projects/proj/runs/modern1/grouping").json()["scorecard"]
        assert scorecard["group_count"] > 0
        assert "modularity" in scorecard
        assert "cross_group_edges" in scorecard

    def test_both_levels_of_the_graph_are_served(self, client):
        """The task-level graph the partition consumed, and the group-level DAG
        it produced. The tab draws both, so one without the other is a gap."""
        body = client.get("/api/projects/proj/runs/modern1/grouping").json()
        assert body["input_graph"]["nodes"]
        assert body["input_graph"]["affinity"] is not None
        assert body["dag"], "the group-level DAG never reached the client"


class TestFixture:
    def test_the_postmortem_fixture_is_committed_and_complete(self):
        for relative in ("state.json", "manifest.json", "groups.json", "logs/run.log"):
            assert (FIXTURE / relative).is_file(), relative
        state = RunState.model_validate_json((FIXTURE / "state.json").read_text())
        assert all(entry.state == GroupState.COMPLETED for entry in state.groups.values())
        grouping = GroupingResult.model_validate_json((FIXTURE / "groups.json").read_text())
        assert {group.id for group in grouping.groups} == set(state.groups)

    def test_run_paths_groups_path_points_into_the_run_dir(self, tmp_path):
        paths = RunPaths(tmp_path, "r1")
        assert paths.groups_path == paths.run_dir / "groups.json"
