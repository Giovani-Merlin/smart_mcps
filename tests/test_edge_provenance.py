"""Plan P2: the edge-provenance ledgers and the ``edge-provenance.json`` sidecar.

The whole point of the ledgers is that they explain a weight without changing it,
so the anchor here is an exhaustive sum identity — for every pair in the graph the
recorded contributions (plus whatever the cap kept out) add back to exactly the
weight the partitioner saw. Everything else in this module exists to make that
identity meaningful on a graph that actually exercises each signal: shared files,
calls, impact, declared depends_on, semantics, the prose fallback, truncation, and
withdrawn cycle edges.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.config import OrchestratorConfig
from orchestrator.grouping.graphing import (
    MAX_CONTRIBUTIONS_PER_EDGE,
    CodegraphClient,
    EdgeProvenance,
    EdgeWeights,
    TaskMapping,
    build_task_graph,
)
from orchestrator.grouping.mapper import MapperOutput
from orchestrator.grouping.pipeline import (
    EdgeProvenanceRecorder,
    _with_prose_fallback,
    compute_partition,
    edge_provenance_document,
)

from tests.test_cli import _stub_codegraph_runner
from tests.test_grouping_fixtures import ALL_FIXTURES, client_for, make_repo


def _runner(responses):
    """Canned codegraph output keyed by (command, symbol); everything else empty."""

    def run(args):
        command = args[0]
        if command == "sync":
            return ""
        if command == "files":
            return "stub repo\n"
        if command == "query":
            return "[]"
        if command == "status":
            return json.dumps({"initialized": True, "nodeCount": 1})
        symbol = args[1]
        canned = responses.get((command, symbol))
        if canned is not None:
            return canned
        key = {"callers": "callers", "callees": "callees", "impact": "affected"}[command]
        return json.dumps({"symbol": symbol, key: []})

    return run


def _callers(symbol, entries):
    return json.dumps(
        {"symbol": symbol, "callers": [{"name": n, "filePath": f} for n, f in entries]}
    )


def _affected(symbol, entries):
    return json.dumps(
        {"symbol": symbol, "affected": [{"name": n, "filePath": f} for n, f in entries]}
    )


def assert_sums_are_exact(graph):
    """Σ recorded scaled_weight (+ the truncated remainder) == the stored weight.

    Asserted over *every* pair in both maps, in both directions: no weight without a
    ledger, and no ledger without a weight.
    """
    provenance = graph.provenance
    assert isinstance(provenance, EdgeProvenance)
    assert set(provenance.affinity) == set(graph.affinity)
    assert set(provenance.dependencies) == set(graph.dependencies)

    for maps in (
        (graph.affinity, provenance.affinity),
        (graph.dependencies, provenance.dependencies),
    ):
        weights, ledgers = maps
        for key, weight in weights.items():
            ledger = ledgers[key]
            recorded = sum(c.scaled_weight for c in ledger.contributions)
            if ledger.truncated_contributions == 0:
                # Same additions in the same order as the weight map: exact, not close.
                assert recorded == weight, key
            assert recorded + ledger.truncated_weight == pytest.approx(weight, rel=1e-12), key
            assert ledger.total_weight == weight, key
            assert ledger.recorded_contributions <= provenance.max_contributions_per_edge
            assert ledger.total_contributions >= ledger.recorded_contributions


def wide_graph(shared_files=30):
    """Every signal at once, with one pair pushed well past the contribution cap.

    ``a``/``b`` share ``shared_files`` files (one contribution each), reference each
    other through calls and impact, and are joined by a route tag; ``c`` depends on
    ``b`` by declaration and reaches ``a`` through impact only.
    """
    files = tuple(f"app/shared{i}.py" for i in range(shared_files))
    responses = {
        ("callers", "a_sym"): _callers("a_sym", [("b_fn", "app/b.py")]),
        ("impact", "b_sym"): _affected("b_sym", [("a_fn", "app/a.py"), ("c_fn", "app/c.py")]),
    }
    client = CodegraphClient(repo_root=Path("."), runner=_runner(responses))
    return build_task_graph(
        [
            TaskMapping(
                "a",
                files=(*files, "app/a.py"),
                symbols=("a_sym",),
                implements=("GET /items",),
            ),
            TaskMapping(
                "b",
                files=(*files, "app/b.py"),
                symbols=("b_sym",),
                consumes=("GET /items",),
            ),
            TaskMapping("c", files=("app/c.py",), depends_on=("b",)),
        ],
        client,
        EdgeWeights(),
    )


class TestSumExactness:
    """The correctness anchor: provenance explains the weight, never alters it."""

    def test_every_edge_sums_back_to_its_weight(self):
        assert_sums_are_exact(wide_graph())

    def test_the_fixture_exercises_the_cap(self):
        """A sum identity that never truncated would not prove the harder half."""
        graph = wide_graph()
        ledger = graph.provenance.affinity[("a", "b")]
        assert ledger.truncated_contributions > 0
        assert ledger.truncated_weight > 0.0

    def test_every_signal_kind_is_represented(self):
        """Below the cap, so nothing is hidden by truncation."""
        graph = wide_graph(shared_files=2)
        kinds = {
            c.kind
            for ledgers in (graph.provenance.affinity, graph.provenance.dependencies)
            for ledger in ledgers.values()
            for c in ledger.contributions
        }
        assert kinds == {"shared_file", "call", "impact", "declared_depends_on", "semantic"}

    def test_declared_flag_distinguishes_statement_from_inference(self):
        """b -> c is claimed twice — once by the plan, once by an impact relation —
        and the sidecar has to keep the two apart."""
        graph = wide_graph(shared_files=2)
        contributions = graph.provenance.dependencies[("b", "c")].contributions
        by_kind = {c.kind: c for c in contributions}
        assert by_kind["declared_depends_on"].declared is True
        assert by_kind["impact"].declared is False
        assert all(c.declared is False for c in graph.provenance.affinity[("b", "c")].contributions)

    def test_contributions_name_the_files_that_justified_them(self):
        graph = wide_graph(shared_files=2)
        shared = [
            c
            for c in graph.provenance.affinity[("a", "b")].contributions
            if c.kind == "shared_file"
        ]
        assert sorted(f for c in shared for f in c.files) == [
            "app/shared0.py",
            "app/shared1.py",
        ]

    def test_pipeline_graph_sums_exactly_including_the_prose_fallback(self, tmp_path):
        """The pipeline rebuilds the graph to attach region-less tasks; the ledger
        has to follow it through that rebuild, not stop at build_task_graph."""
        repo, plan = make_repo(tmp_path, "hub-file-symbols")
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            llm_runner=lambda prompt, schema: pytest.fail("no LLM in a fixture test"),
            client=client_for(repo, "hub-file-symbols"),
        )
        assert_sums_are_exact(outcome.graph)


class TestTheWholeFixtureRegister:
    """The register is the honest place to assert this: one synthetic graph could
    always be the one shape whose arithmetic happens to line up."""

    @pytest.mark.parametrize("fixture_name,real_files,config_overrides", ALL_FIXTURES)
    def test_every_register_fixture_sums_exactly(
        self, tmp_path, fixture_name, real_files, config_overrides
    ):
        config = OrchestratorConfig()
        for key, value in config_overrides.items():
            setattr(config.estimator, key, value)
        if fixture_name == "slice-over-budget":
            config.partition.allow_oversized_slice = True
        repo, plan = make_repo(tmp_path, fixture_name, real_files=real_files)
        recorder = EdgeProvenanceRecorder()
        outcome = compute_partition(
            plan_path=plan,
            repo_root=repo,
            config=config,
            llm_runner=lambda prompt, schema: pytest.fail("no LLM in a fixture test"),
            client=client_for(repo, fixture_name),
            provenance_recorder=recorder,
        )
        assert_sums_are_exact(outcome.graph)
        assert recorder.document is not None
        assert len(recorder.document["affinity"]) == len(outcome.graph.affinity)
        json.loads(json.dumps(recorder.document))
        if fixture_name == "hub-file-symbols":
            # The register's only symbol-bearing fixture is also its only cycle-drop
            # case: real withdrawals, not just the synthetic pair above.
            assert outcome.graph.provenance.withdrawn


class TestProseFallbackContributions:
    """The one affinity signal added outside ``_EdgeAccumulator``, in the pipeline."""

    def test_region_less_task_edge_is_attributed_to_the_prose_fallback(self):
        client = CodegraphClient(repo_root=Path("."), runner=_runner({}))
        mappings = [
            TaskMapping("a", files=("app/a.py",)),
            TaskMapping("prose_only"),
        ]
        graph = build_task_graph(mappings, client, EdgeWeights())
        mapper_out = MapperOutput(mappings=mappings, descriptions={})

        with_fallback = _with_prose_fallback(graph, mapper_out, weight=0.25)

        ledger = with_fallback.provenance.affinity[("a", "prose_only")]
        assert [c.kind for c in ledger.contributions] == ["prose_neighbor"]
        assert ledger.total_weight == with_fallback.affinity[("a", "prose_only")] == 0.25
        assert_sums_are_exact(with_fallback)


class TestContributionCap:
    """Truncation is counted, never hidden (plan P2's size control)."""

    def test_over_cap_edge_records_exactly_the_cap_and_reports_the_true_total(self):
        graph = wide_graph(shared_files=25)
        ledger = graph.provenance.affinity[("a", "b")]
        assert MAX_CONTRIBUTIONS_PER_EDGE == 20
        assert ledger.recorded_contributions == 20
        # 25 shared files + one caller edge + one impact edge + one semantic edge.
        assert ledger.total_contributions == 28
        assert ledger.truncated_contributions == 8
        assert ledger.truncated_weight > 0.0

    def test_under_cap_edge_records_everything_and_truncates_nothing(self):
        graph = wide_graph(shared_files=2)
        ledger = graph.provenance.affinity[("a", "b")]
        assert ledger.recorded_contributions == ledger.total_contributions
        assert ledger.truncated_contributions == 0
        assert ledger.truncated_weight == 0.0

    def test_the_document_carries_the_counters(self):
        graph = wide_graph(shared_files=25)
        document = edge_provenance_document(graph, {"a": 0, "b": 0, "c": 1})
        edge = next(e for e in document["affinity"] if (e["a"], e["b"]) == ("a", "b"))
        assert edge["recorded_contributions"] == 20
        assert edge["total_contributions"] == 28
        assert edge["truncated_contributions"] == 8
        assert len(edge["contributions"]) == 20


class TestWithdrawnEdges:
    """F3's prose-in-a-flag-string, replaced by records with endpoints and a reason."""

    def _mutual(self):
        responses = {
            ("callers", "a_sym"): _callers("a_sym", [("b_sym", "b.py")]),
            ("callers", "b_sym"): _callers("b_sym", [("a_sym", "a.py")]),
        }
        client = CodegraphClient(repo_root=Path("."), runner=_runner(responses))
        return build_task_graph(
            [
                TaskMapping("a", files=("a.py",), symbols=("a_sym",)),
                TaskMapping("b", files=("b.py",), symbols=("b_sym",)),
            ],
            client,
            EdgeWeights(call=2.0),
        )

    def test_mutual_reference_withdrawal_is_structured(self):
        graph = self._mutual()
        assert graph.dependencies == {}
        withdrawn = graph.provenance.withdrawn
        assert {(w.upstream, w.downstream) for w in withdrawn} == {("a", "b"), ("b", "a")}
        assert {w.reason for w in withdrawn} == {"mutual_reference"}
        assert all(w.weight == 2.0 for w in withdrawn)
        # The evidence that proposed the edge survives its withdrawal.
        assert all(w.contributions and w.contributions[0].kind == "call" for w in withdrawn)

    def test_withdrawn_edges_leave_the_dependency_ledger(self):
        """A withdrawn edge must not linger in the ledger mirroring the live map."""
        graph = self._mutual()
        assert graph.provenance.dependencies == {}
        assert graph.affinity[("a", "b")] == 4.0

    def test_longer_cycle_records_its_members(self):
        responses = {
            ("callers", "a_sym"): _callers("a_sym", [("b_sym", "b.py")]),
            ("callers", "b_sym"): _callers("b_sym", [("c_sym", "c.py")]),
            ("callers", "c_sym"): _callers("c_sym", [("a_sym", "a.py")]),
        }
        client = CodegraphClient(repo_root=Path("."), runner=_runner(responses))
        graph = build_task_graph(
            [
                TaskMapping("a", files=("a.py",), symbols=("a_sym",)),
                TaskMapping("b", files=("b.py",), symbols=("b_sym",)),
                TaskMapping("c", files=("c.py",), symbols=("c_sym",)),
            ],
            client,
            EdgeWeights(call=2.0),
        )
        withdrawn = graph.provenance.withdrawn
        assert {w.reason for w in withdrawn} == {"reference_cycle"}
        assert all(w.cycle_members == ("a", "b", "c") for w in withdrawn)

    def test_document_serializes_withdrawn_records(self):
        graph = self._mutual()
        document = edge_provenance_document(graph, {"a": 0, "b": 0})
        assert document["withdrawn"] == sorted(
            document["withdrawn"], key=lambda r: (r["upstream"], r["downstream"])
        )
        first = document["withdrawn"][0]
        assert first["kind"] == "inferred_precedence"
        assert first["reason"] == "mutual_reference"
        assert {"upstream", "downstream", "weight", "cycle_members"} <= set(first)
        # Round-trips as JSON — the sidecar is a file, not an in-memory object.
        json.loads(json.dumps(document))


class TestGroupRollup:
    """The group-level view is computed at write time, in the pipeline."""

    def test_internal_and_external_affinity_split_by_membership(self):
        graph = wide_graph(shared_files=2)
        document = edge_provenance_document(graph, {"a": 0, "b": 0, "c": 1})
        by_id = {g["group_id"]: g for g in document["groups"]}
        assert by_id["g1"]["tasks"] == ["a", "b"]
        assert by_id["g1"]["internal_affinity_weight"] == graph.affinity[("a", "b")]
        assert by_id["g1"]["external_affinity_weight"] == sum(
            w for pair, w in graph.affinity.items() if pair != ("a", "b")
        )
        assert "shared_file" in by_id["g1"]["internal_affinity_by_kind"]

    def test_upstream_dependency_edges_are_listed_per_group(self):
        graph = wide_graph(shared_files=2)
        document = edge_provenance_document(graph, {"a": 0, "b": 0, "c": 1})
        by_id = {g["group_id"]: g for g in document["groups"]}
        assert by_id["g2"]["upstream_dependency_edges"] == [["b", "c"]]
        assert by_id["g1"]["upstream_dependency_edges"] == []

    def test_a_graph_without_ledgers_degrades_instead_of_raising(self):
        """Historical/synthetic graphs carry no provenance; the writer says so."""
        from orchestrator.grouping.partition import TaskGraph

        document = edge_provenance_document(TaskGraph(nodes=frozenset({"a"})), {"a": 0})
        assert document["affinity"] == []
        assert "note" in document


class TestSidecarIsWritten:
    """The artifact itself, on the modes that produce a partition."""

    def _repo(self, tmp_path):
        from tests.test_cli import GRANULARITY_LADDER_PLAN

        repo = tmp_path / "repo"
        repo.mkdir()
        plan = repo / "plan.md"
        plan.write_text(GRANULARITY_LADDER_PLAN)
        return repo, plan

    def test_no_spec_writes_the_sidecar(self, tmp_path):
        """--no-spec is the debugging mode, so it is exactly when this is wanted."""
        repo, plan = self._repo(tmp_path)
        exit_code = main(
            ["group", str(plan), "--repo", str(repo), "--no-spec"],
            client=CodegraphClient(repo_root=repo, runner=_stub_codegraph_runner),
        )
        assert exit_code == 0
        # Plan U8: --no-spec's sidecar lives in the preview subdirectory.
        sidecar = repo / ".orchestrator" / "groupings" / "plan" / "preview" / "edge-provenance.json"
        document = json.loads(sidecar.read_text())
        assert document["version"] == 1
        assert document["max_contributions_per_edge"] == MAX_CONTRIBUTIONS_PER_EDGE
        assert document["dependencies"], "declared depends_on edges should be recorded"
        assert {g["group_id"] for g in document["groups"]}

    def test_recorder_is_inert(self, tmp_path):
        """Attaching a recorder must not change the partition it observes."""
        repo, plan = make_repo(tmp_path, "hub-file-symbols")
        kwargs = dict(
            plan_path=plan,
            repo_root=repo,
            llm_runner=lambda prompt, schema: pytest.fail("no LLM in a fixture test"),
        )
        without = compute_partition(client=client_for(repo, "hub-file-symbols"), **kwargs)
        recorder = EdgeProvenanceRecorder()
        with_recorder = compute_partition(
            client=client_for(repo, "hub-file-symbols"),
            provenance_recorder=recorder,
            **kwargs,
        )
        assert without.partition == with_recorder.partition
        assert without.graph.affinity == with_recorder.graph.affinity
        assert recorder.document is not None
