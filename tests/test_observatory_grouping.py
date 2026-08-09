"""The Grouping tab's read model, and the model drift the Observatory had missed.

Two fixtures back everything here. ``run-postmortem`` is the original finished
run and stands in for "recorded before any of this existed" — it must keep
rendering unchanged. ``run-modern`` is a run from after the orchestrator merge:
a named grouping, a persisted escalation tier, per-session token classes, and
the two states the merge added, one of them carrying the stale ``failure``
string that a last-writer-wins ``GroupRunState`` cannot avoid leaving behind.

``run-modern``'s ``grouping-trace.json`` is a real one, copied verbatim from the
``obsprov1`` run — the run that produced this very change. Its merge stage
genuinely moves four tasks between groups, which is the case the stepper exists
for, and its ``renumber`` stage relabels groups without moving anything, which
is the case an id-based diff would get wrong. A second real trace,
``grouping-trace-with-slices.json``, covers slice atoms: obsprov1's plan
happened to produce none, and a section that is empty in every fixture is a
section nobody has actually tested.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.execution.manifest import RunPaths
from orchestrator.observatory.app import create_app
from orchestrator.observatory.grouping import (
    build_grouping_view,
    resolve_dag_source,
    stage_diffs,
)
from tests.test_observatory_api import FIXTURE, install_run

MODERN = Path(__file__).parent / "fixtures" / "observatory" / "run-modern"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "modern1", source=MODERN)
    install_run(repo, "legacy1", source=FIXTURE)
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    return TestClient(
        create_app(
            registry_path=tmp_path / "no-registry.yaml",
            fallback_repo=repo,
            dist_dir=tmp_path / "no-dist",
        )
    )


def grouping_of(client: TestClient, run_id: str = "modern1") -> dict:
    response = client.get(f"/api/projects/proj/runs/{run_id}/grouping")
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ drift repair


class TestSnapshotDrift:
    def test_named_grouping_and_escalation_tier_reach_the_client(self, client):
        body = client.get("/api/projects/proj/runs/modern1/snapshot").json()
        assert body["grouping"] == (
            "2026-07-29-001-fix-orchestrator-correctness-and-measurement-plan"
        )
        assert body["escalation"]["enabled"] is True
        assert body["escalation"]["intensity"] == "on_stuck"
        assert body["escalation"]["source"] == "workers_via_orchestrator"

    def test_a_run_without_them_still_renders(self, client):
        """The fields are new; every run recorded before them reads as None."""
        body = client.get("/api/projects/proj/runs/legacy1/snapshot").json()
        assert body["grouping"] is None
        assert body["escalation"] is None
        assert body["groups"]

    def test_the_merge_states_survive_the_round_trip(self, client):
        by_id = {g["group_id"]: g for g in grouping_states(client)}
        assert by_id["g1"]["state"] == "resolved"
        assert by_id["g2"]["state"] == "interrupted"

    def test_a_resolved_group_flags_its_failure_text_as_stale(self, client):
        """`GroupRunState` cannot say "failed once, then resolved" — it says
        ``resolved`` with the old failure string still attached. Rendering that
        as a failure is the likeliest wrong thing this surface could do."""
        by_id = {g["group_id"]: g for g in grouping_states(client)}
        assert by_id["g1"]["failure"]  # the text is still there, deliberately
        assert by_id["g1"]["stale_failure"] is True

    def test_an_interrupted_group_keeps_its_failure_text_live(self, client):
        """INTERRUPTED is not a success state, so its failure text is current."""
        by_id = {g["group_id"]: g for g in grouping_states(client)}
        assert by_id["g2"]["stale_failure"] is False

    def test_session_token_classes_reach_the_client(self, client):
        by_id = {g["group_id"]: g for g in grouping_states(client)}
        coder = next(s for s in by_id["g1"]["sessions"] if s["role"] == "coder")
        assert coder["total_cache_read_tokens"] == 1840223
        assert coder["total_cache_creation_tokens"] == 96110
        assert coder["total_input_tokens"] == 9130
        assert coder["total_output_tokens"] == 21440
        assert coder["rounds_completed"] >= 1
        assert coder["model"] == "claude-opus-5"

    def test_the_estimate_side_of_the_comparison_is_served_too(self, client):
        """`estimated_tokens` predicts one coder's context occupancy, which is
        `last_context_tokens` — not cumulative spend. Both have to be present
        for the UI to keep them in the separate panels the plan requires."""
        by_id = {g["group_id"]: g for g in grouping_states(client)}
        assert by_id["g1"]["estimated_tokens"] is not None
        assert by_id["g1"]["intensity"]
        coder = next(s for s in by_id["g1"]["sessions"] if s["role"] == "coder")
        assert coder["last_context_tokens"] > 0

    def test_every_retired_attempt_is_still_listed(self, client):
        """manifest.json's session list is append-only and is the ground truth
        for what attempts existed; state.json is authoritative only for now."""
        by_id = {g["group_id"]: g for g in grouping_states(client)}
        retired = [s for s in by_id["g1"]["sessions"] if s["retirement_reason"]]
        assert retired and "exceeded limit" in retired[0]["retirement_reason"]

    def test_a_group_resolve_escalation_is_listed(self, client):
        body = client.get("/api/projects/proj/runs/modern1/escalations").json()
        assert [e["kind"] for e in body] == ["group_resolve"]

    def test_live_pids_are_reported_but_never_acted_on(self, client):
        body = client.get("/api/projects/proj/runs/modern1/snapshot").json()
        assert body["live_pids"] == {"41234": "g2"}
        # An interrupted group with a recorded pid still renders; nothing here
        # treats the pid as evidence the group is alive.
        assert body["groups"]


def grouping_states(client: TestClient) -> list[dict]:
    return client.get("/api/projects/proj/runs/modern1/snapshot").json()["groups"]


# ------------------------------------------------------------------ dag_source


class TestDagSource:
    def test_a_run_with_its_own_snapshot_is_not_stale(self, client):
        source = grouping_of(client)["dag_source"]
        assert source["kind"] == "run_snapshot"
        assert source["stale_dag"] is False
        assert source["groups_path"].endswith("modern1/groups.json")

    def test_stale_dag_matches_the_board_exactly(self, client):
        """One computation, two surfaces. They must never disagree."""
        for run_id in ("modern1", "legacy1"):
            snapshot = client.get(f"/api/projects/proj/runs/{run_id}/snapshot").json()
            tab = grouping_of(client, run_id)
            assert tab["dag_source"]["stale_dag"] == snapshot["stale_dag"]

    def test_a_named_grouping_is_used_when_the_run_never_froze_one(self, repo, client):
        """A run that crashed before its snapshot was taken still names its
        grouping, and reading that beats reading the shared file."""
        run_dir = repo / ".orchestrator" / "runs" / "modern1"
        (run_dir / "groups.json").unlink()
        named = (
            repo
            / ".orchestrator"
            / "groupings"
            / "2026-07-29-001-fix-orchestrator-correctness-and-measurement-plan"
        )
        named.mkdir(parents=True)
        (named / "groups.json").write_text((MODERN / "groups.json").read_text())
        (named / "grouping-trace.json").write_text((MODERN / "grouping-trace.json").read_text())

        source = grouping_of(client)["dag_source"]
        assert source["kind"] == "named_grouping"
        assert source["grouping_name"].startswith("2026-07-29-001")
        assert "regrouped since" in source["reason"]

    def test_stale_dag_is_unchanged_by_the_named_grouping_step(self, repo, client, tmp_path):
        """The verbatim-semantics guard. ``stale_dag`` means one thing and one
        thing only: this run has no frozen groups.json of its own. Finding a
        better fallback than the shared file does not make it any less true.
        """
        paths = RunPaths(repo, "modern1")

        # Fresh: the run's own snapshot exists.
        assert legacy_stale(paths) is False
        assert resolve_dag_source(paths, "whatever").stale_dag is False

        # Stale: it does not — with and without a resolvable named grouping.
        (paths.run_dir / "groups.json").unlink()
        assert legacy_stale(paths) is True
        assert resolve_dag_source(paths, None).stale_dag is True

        named = repo / ".orchestrator" / "groupings" / "named-one"
        named.mkdir(parents=True)
        (named / "groups.json").write_text((MODERN / "groups.json").read_text())
        resolved = resolve_dag_source(paths, "named-one")
        assert resolved.kind == "named_grouping"
        assert resolved.stale_dag is True, "resolving a better source must not clear stale_dag"
        assert legacy_stale(paths) is True

    def test_an_unsafe_grouping_name_falls_through_instead_of_raising(self, repo):
        paths = RunPaths(repo, "modern1")
        (paths.run_dir / "groups.json").unlink()
        assert resolve_dag_source(paths, "../escape").kind in ("shared_fallback", "missing")

    def test_a_run_with_nothing_at_all_reports_missing(self, repo):
        paths = RunPaths(repo, "modern1")
        (paths.run_dir / "groups.json").unlink()
        source = resolve_dag_source(paths, None)
        assert source.kind == "missing"
        assert source.stale_dag is True


def legacy_stale(paths: RunPaths) -> bool:
    """``load_dag``'s original staleness rule, spelled out here so a change to
    the implementation has to disagree with a literal copy of the old one."""
    return not (paths.run_dir / "groups.json").is_file()


# ------------------------------------------------------------------- the trace


class TestTracePassthrough:
    def test_every_section_the_tab_renders_is_present(self, client):
        body = grouping_of(client)
        assert body["trace_schema_version"] == 1
        assert body["trace_schema_known"] is True
        assert [s["stage"] for s in body["stages"]] == [
            "louvain",
            "lift",
            "split",
            "merge",
            "repair",
            "renumber",
        ]
        assert body["louvain"] and body["louvain"][0]["communities"]
        assert body["splits"] and body["merges"]
        assert body["hub_roles"] and body["hub_roles"][0]["role"]
        assert body["scorecard"]["group_count"] > 0
        assert body["group_difficulty"] and body["group_difficulty"][0]["intensity"]
        assert body["provenance"]["repo_commit_sha"]
        assert body["last_stage"]
        assert body["input_graph"]["affinity"]
        assert body["node_work"] and body["budget"]["budget_cap"]

    def test_merge_rationale_is_carried_verbatim(self, client):
        """ "Why did these two not merge" is a stored answer; do not paraphrase it."""
        rejected = [m for m in grouping_of(client)["merges"] if not m["accepted"]]
        assert rejected and rejected[0]["reason"]

    def test_a_future_schema_version_still_renders_and_says_so(self, repo, client):
        trace_path = repo / ".orchestrator" / "runs" / "modern1" / "grouping-trace.json"
        trace = json.loads(trace_path.read_text())
        trace["schema_version"] = 99
        trace_path.write_text(json.dumps(trace))

        body = grouping_of(client)
        assert body["trace_schema_known"] is False
        assert body["stages"], "a newer schema must still render what it can"
        assert any("schema v1" in m["artifact"] for m in body["missing"])

    def test_slice_atoms_render_when_the_plan_produced_any(self, repo, client):
        """Slice atoms are the grouper's "these tasks are one indivisible unit"
        record. obsprov1's plan produced none, so this uses a second real trace
        rather than leaving the section untested."""
        slices = Path(__file__).parent / "fixtures" / "observatory" / (
            "grouping-trace-with-slices.json"
        )
        target = repo / ".orchestrator" / "runs" / "modern1" / "grouping-trace.json"
        target.write_text(slices.read_text())

        body = grouping_of(client)
        assert body["slice_atoms"][0]["label"] == "merge-integrity"
        assert body["slice_atoms"][0]["members"] == [
            "u1-merge-integrity",
            "u2-failure-gate",
        ]

    def test_base_context_is_served(self, client):
        response = client.get("/api/projects/proj/runs/modern1/grouping/base-context")
        assert response.status_code == 200
        assert "Base context" in response.json()


# ----------------------------------------------------------------- stage diffs


class TestStageDiffs:
    def test_the_merge_stage_recolour_set_is_exact(self, client):
        """The case the stepper exists for: four tasks change group at ``merge``
        in this real trace, and those four are what must recolour."""
        diffs = {d["stage"]: d for d in grouping_of(client)["stage_diffs"]}
        assert diffs["merge"]["previous_stage"] == "split"
        assert diffs["merge"]["moved"] == [
            "merge-f4-groups-path-fix",
            "observatory-drift-repair",
            "transcript-parser-thinking-usage",
            "ui-grouping-tab",
        ]
        assert diffs["merge"]["group_count"] == 9

    def test_renumber_moves_nothing_even_though_every_id_changes(self, client):
        """Group ids are rewritten wholesale at ``renumber``. An id-based diff
        would light up the entire graph; a co-membership diff correctly says
        nothing moved."""
        diffs = {d["stage"]: d for d in grouping_of(client)["stage_diffs"]}
        assert diffs["renumber"]["moved"] == []

    def test_the_first_stage_seeds_rather_than_diffs(self, client):
        diffs = grouping_of(client)["stage_diffs"]
        assert diffs[0]["previous_stage"] is None
        assert diffs[0]["moved"] == []
        assert len(diffs[0]["added"]) == 14

    def test_renaming_groups_is_not_a_move(self):
        stages = [
            {"stage": "a", "partition": {"x": 0, "y": 0, "z": 1}},
            {"stage": "b", "partition": {"x": 7, "y": 7, "z": 3}},
        ]
        assert stage_diffs(stages)[1].moved == []

    def test_moving_one_node_moves_its_old_and_new_mates(self):
        """Co-membership is symmetric: pulling z in changes things for x and y too."""
        stages = [
            {"stage": "a", "partition": {"x": 0, "y": 0, "z": 1}},
            {"stage": "b", "partition": {"x": 0, "y": 0, "z": 0}},
        ]
        assert stage_diffs(stages)[1].moved == ["x", "y", "z"]

    def test_a_malformed_stage_is_skipped_rather_than_fatal(self):
        stages = [{"stage": "a", "partition": {"x": 0}}, {"stage": "b"}, {"nope": True}]
        assert [d.stage for d in stage_diffs(stages)] == ["a"]


# ---------------------------------------------------------------- degradation


class TestDegradation:
    def test_missing_edge_provenance_names_the_artifact_and_its_path(self, client):
        """No orchestrator on disk writes this file yet, so this is the state of
        every run today. It must be an explicit, actionable absence."""
        body = grouping_of(client)
        missing = {m["artifact"]: m for m in body["missing"]}
        assert "edge-provenance.json" in missing
        assert missing["edge-provenance.json"]["expected_path"].endswith(
            "modern1/edge-provenance.json"
        )
        assert missing["edge-provenance.json"]["explanation"]
        assert body["edge_provenance"] is None

    def test_a_missing_trace_degrades_instead_of_404ing(self, client):
        """``legacy1`` predates the trace schema — the common case, not an error."""
        body = grouping_of(client, "legacy1")
        missing = {m["artifact"]: m for m in body["missing"]}
        assert "grouping-trace.json" in missing
        assert missing["grouping-trace.json"]["expected_path"].endswith(
            "legacy1/grouping-trace.json"
        )
        assert body["stages"] == []
        assert body["stage_diffs"] == []
        assert body["dag_source"]["kind"] == "run_snapshot"

    def test_a_corrupt_trace_reads_as_absent(self, repo, client):
        trace = repo / ".orchestrator" / "runs" / "modern1" / "grouping-trace.json"
        trace.write_text("{ this is not json")
        body = grouping_of(client)
        assert any(m["artifact"] == "grouping-trace.json" for m in body["missing"])

    def test_every_path_the_tab_shows_is_absolute(self, client):
        """`PathChip` copies these; a relative path is useless in another shell."""
        for key, value in grouping_of(client)["paths"].items():
            assert Path(value).is_absolute(), f"{key} is not absolute: {value}"

    def test_an_unknown_run_is_still_a_404(self, client):
        assert client.get("/api/projects/proj/runs/ghost/grouping").status_code == 404


def test_the_view_builds_without_an_http_request(tmp_path: Path) -> None:
    """``build_grouping_view`` is plain disk reading; nothing about it needs a
    request, which is what keeps it testable against a directory on disk."""
    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "modern1", source=MODERN)
    view = build_grouping_view(RunPaths(repo, "modern1"), "proj")
    assert view.run_id == "modern1"
    assert view.stage_diffs
