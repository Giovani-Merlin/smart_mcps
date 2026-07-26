"""Tests for orchestrator/grouping/graphing.py — the codegraph affinity adapter.

Synthetic runners cover the edge-computation scenarios from plan U2; the captured
fixtures under tests/fixtures/codegraph/ (real CLI output from this repo) prove the
adapter parses the true shapes and that its output feeds U1 without adaptation.
"""

import json
from pathlib import Path

import pytest

from orchestrator.grouping.graphing import (
    CodegraphClient,
    EdgeWeights,
    GraphBuildError,
    TaskMapping,
    build_task_graph,
)
from orchestrator.grouping.partition import DefaultPartitionStrategy

FIXTURES = Path(__file__).parent / "fixtures" / "codegraph"
REPO_ROOT = Path(__file__).parent.parent


def empty_response(command, symbol):
    if command == "sync":
        return ""
    if command == "query":
        return "[]"
    if command == "files":
        return "files overview"
    key = {"callers": "callers", "callees": "callees", "impact": "affected"}[command]
    return json.dumps({"symbol": symbol, key: []})


class FakeRunner:
    """Replays canned CLI output keyed by (command, symbol); records every argv."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        command = args[0]
        symbol = args[1] if len(args) > 1 else None
        response = self.responses.get((command, symbol))
        if response is None:
            return empty_response(command, symbol)
        return response


def client_with(responses, repo_root=REPO_ROOT):
    runner = FakeRunner(responses)
    return CodegraphClient(repo_root=repo_root, runner=runner), runner


class TestSharedFileEdges:
    def test_shared_file_produces_edge_scaling_with_overlap(self):
        """Plan U2 scenario: shared files → edge; weight scales with overlap count."""
        client, _ = client_with({})
        one_shared = build_task_graph(
            [
                TaskMapping("t1", files=("x.py", "a.py")),
                TaskMapping("t2", files=("x.py", "b.py")),
            ],
            client,
            EdgeWeights(shared_file=1.0),
        )
        two_shared = build_task_graph(
            [
                TaskMapping("t1", files=("x.py", "y.py")),
                TaskMapping("t2", files=("x.py", "y.py")),
            ],
            client,
            EdgeWeights(shared_file=1.0),
        )
        assert one_shared.affinity[("t1", "t2")] == 1.0
        assert two_shared.affinity[("t1", "t2")] == 2.0

    def test_shared_files_carry_no_dependency_direction(self):
        client, _ = client_with({})
        graph = build_task_graph(
            [TaskMapping("t1", files=("x.py",)), TaskMapping("t2", files=("x.py",))],
            client,
        )
        assert graph.dependencies == {}


class TestCallProximityEdges:
    def test_caller_task_depends_on_callee_task(self):
        """Plan U2 scenario: regions calling each other → proximity edge, directed."""
        responses = {
            ("callers", "callee_sym"): json.dumps(
                {
                    "symbol": "callee_sym",
                    "callers": [{"name": "caller_fn", "kind": "function", "filePath": "caller.py"}],
                }
            )
        }
        client, _ = client_with(responses)
        graph = build_task_graph(
            [
                TaskMapping("lib", files=("lib.py",), symbols=("callee_sym",)),
                TaskMapping("app", files=("caller.py",), symbols=("caller_fn",)),
            ],
            client,
            EdgeWeights(call=2.0),
        )
        # app calls lib's symbol → app depends on lib.
        assert graph.dependencies == {("lib", "app"): 2.0}
        assert graph.affinity[("app", "lib")] == 2.0

    def test_same_relation_seen_from_both_sides_counts_once(self):
        """callers(x) on one task and callees(y) on the other describe one edge."""
        responses = {
            ("callers", "callee_sym"): json.dumps(
                {
                    "symbol": "callee_sym",
                    "callers": [{"name": "caller_fn", "kind": "function", "filePath": "caller.py"}],
                }
            ),
            ("callees", "caller_fn"): json.dumps(
                {
                    "symbol": "caller_fn",
                    "callees": [{"name": "callee_sym", "kind": "function", "filePath": "lib.py"}],
                }
            ),
        }
        client, _ = client_with(responses)
        graph = build_task_graph(
            [
                TaskMapping("lib", files=("lib.py",), symbols=("callee_sym",)),
                TaskMapping("app", files=("caller.py",), symbols=("caller_fn",)),
            ],
            client,
            EdgeWeights(call=2.0),
        )
        assert graph.dependencies == {("lib", "app"): 2.0}

    def test_unrelated_tasks_get_no_edge(self):
        client, _ = client_with({})
        graph = build_task_graph(
            [
                TaskMapping("t1", files=("a.py",), symbols=("fn_a",)),
                TaskMapping("t2", files=("b.py",), symbols=("fn_b",)),
            ],
            client,
        )
        assert graph.affinity == {}
        assert graph.dependencies == {}


class TestImpactEdges:
    def test_impact_overlap_produces_edge(self):
        """Plan U2 scenario: one task's write surface impacts another's read surface."""
        responses = {
            ("impact", "core_fn"): json.dumps(
                {
                    "symbol": "core_fn",
                    "affected": [
                        {"name": "reader_fn", "kind": "function", "filePath": "reader.py"}
                    ],
                }
            )
        }
        client, _ = client_with(responses)
        graph = build_task_graph(
            [
                TaskMapping("core", files=("core.py",), symbols=("core_fn",)),
                TaskMapping("reader", files=("reader.py",), symbols=("reader_fn",)),
            ],
            client,
            EdgeWeights(impact=1.5),
        )
        # reader consumes what core changes → reader depends on core.
        assert graph.dependencies == {("core", "reader"): 1.5}
        assert graph.affinity[("core", "reader")] == 1.5


class TestFailureModes:
    def test_empty_output_fails_loudly(self):
        """Plan U2 scenario: empty codegraph output is an error, not an empty graph."""
        client, _ = client_with({("callers", "fn"): ""})
        with pytest.raises(GraphBuildError, match="empty output"):
            build_task_graph([TaskMapping("t", symbols=("fn",))], client)

    def test_invalid_json_fails_loudly(self):
        client, _ = client_with({("callers", "fn"): "not json {"})
        with pytest.raises(GraphBuildError, match="invalid JSON"):
            build_task_graph([TaskMapping("t", symbols=("fn",))], client)

    def test_missing_expected_key_fails_loudly(self):
        client, _ = client_with({("callers", "fn"): json.dumps({"symbol": "fn"})})
        with pytest.raises(GraphBuildError, match="missing the 'callers' key"):
            build_task_graph([TaskMapping("t", symbols=("fn",))], client)

    def test_non_list_payload_fails_loudly(self):
        client, _ = client_with(
            {("callers", "fn"): json.dumps({"symbol": "fn", "callers": "nope"})}
        )
        with pytest.raises(GraphBuildError, match="is not a list"):
            build_task_graph([TaskMapping("t", symbols=("fn",))], client)

    def test_duplicate_task_ids_rejected(self):
        client, _ = client_with({})
        with pytest.raises(GraphBuildError, match="duplicate task ids"):
            build_task_graph([TaskMapping("t"), TaskMapping("t")], client)

    def test_symbol_not_found_plain_text_is_empty_result_not_error(self):
        """The live CLI prints `ℹ Symbol "X" not found` with exit 0 and no JSON;
        that is a legitimate empty result, not malformed output."""
        not_found = '\x1b[34mℹ\x1b[0m Symbol "ghost_fn" not found\n'
        client, _ = client_with(
            {
                ("callers", "ghost_fn"): not_found,
                ("callees", "ghost_fn"): not_found,
                ("impact", "ghost_fn"): not_found,
                ("query", "ghost_fn"): not_found,
            }
        )
        graph = build_task_graph([TaskMapping("t", symbols=("ghost_fn",))], client)
        assert graph.affinity == {}
        assert client.query("ghost_fn") == []
        assert client.symbol_exists("ghost_fn") is False


class TestQueriesAndMetadata:
    def test_call_queries_pass_explicit_high_limit(self):
        """The CLI default limit of 20 would silently truncate hub fan-in (plan U2)."""
        client, runner = client_with({})
        build_task_graph([TaskMapping("t", symbols=("fn",))], client)
        call_argvs = [args for args in runner.calls if args[0] in ("callers", "callees")]
        assert call_argvs and all("-l" in args and "1000" in args for args in call_argvs)

    def test_metadata_carries_estimator_inputs(self, tmp_path):
        (tmp_path / "big.py").write_bytes(b"x" * 300)
        responses = {
            ("callers", "fn"): json.dumps(
                {
                    "symbol": "fn",
                    "callers": [
                        {"name": f"c{i}", "kind": "function", "filePath": "other.py"}
                        for i in range(3)
                    ],
                }
            )
        }
        client, _ = client_with(responses, repo_root=tmp_path)
        graph = build_task_graph(
            [TaskMapping("t", files=("big.py", "missing.py"), symbols=("fn",))], client
        )
        meta = graph.metadata["t"]
        assert meta["source_bytes"] == 300
        assert meta["symbol_count"] == 1
        assert meta["fan_in"] == 3
        assert meta["max_symbol_fan_in"] == 3
        assert meta["fan_out"] == 0

    def test_files_overview_strips_ansi_escapes(self):
        """`codegraph files` colorizes even when piped; the summary feeds LLM
        prompts and the human-readable base-context document."""
        client, runner = client_with({})
        runner.responses[("files", None)] = "\x1b[1mProject Structure\x1b[0m\n├── a.py\n"
        assert client.files_overview() == "Project Structure\n├── a.py\n"

    def test_sync_calls_the_runner_with_the_sync_argv(self):
        """R13: sync() goes through the same injectable runner seam as every
        other command, so offline tests can keep faking the CLI."""
        client, runner = client_with({})
        client.sync()
        assert runner.calls == [["sync"]]

    def test_sync_takes_the_project_path_positionally(self):
        """`codegraph sync` rejects `-p` (`Usage: codegraph sync [options] [path]`),
        so the assembled argv must place the repo path positionally. Every
        grouping test injects a runner, which is precisely why passing `-p` here
        broke every real `group` invocation without failing a single test."""
        client, _ = client_with({})
        assert client._argv(["sync"]) == ["codegraph", "sync", str(client.repo_root)]

    def test_query_commands_keep_the_path_flag(self):
        """The query side of the CLI does accept `-p, --path`."""
        client, _ = client_with({})
        assert client._argv(["query", "X", "-j", "-l", "1000"]) == [
            "codegraph",
            "query",
            "X",
            "-j",
            "-l",
            "1000",
            "-p",
            str(client.repo_root),
        ]
        assert client._argv(["files"])[-2:] == ["-p", str(client.repo_root)]

    def test_identical_queries_are_memoized(self):
        """A hub symbol mapped by many tasks must not respawn the CLI per task."""
        client, runner = client_with({})
        build_task_graph(
            [
                TaskMapping("t1", files=("a.py",), symbols=("shared_fn",)),
                TaskMapping("t2", files=("b.py",), symbols=("shared_fn",)),
            ],
            client,
        )
        caller_calls = [args for args in runner.calls if args[0] == "callers"]
        assert len(caller_calls) == 1

    def test_regionless_task_is_isolated_node(self):
        """Unmappable plan tasks ride along as region-less nodes (plan U4 contract)."""
        client, _ = client_with({})
        graph = build_task_graph(
            [TaskMapping("mapped", files=("a.py",)), TaskMapping("prose_only")], client
        )
        assert "prose_only" in graph.nodes
        assert graph.metadata["prose_only"]["source_bytes"] == 0


class TestPlanTimeSignals:
    """Task-map signals (plan U3/U4): prospective files, depends_on, route tags."""

    def test_shared_prospective_file_produces_affinity(self):
        """The greenfield fix: a pair sharing a planned-but-nonexistent file
        clusters like an editing pair sharing a real one."""
        client, _ = client_with({})
        graph = build_task_graph(
            [
                TaskMapping("t1", prospective_files=("app/new.py",)),
                TaskMapping("t2", prospective_files=("app/new.py", "app/other.py")),
            ],
            client,
            EdgeWeights(shared_file=1.0),
        )
        assert graph.affinity[("t1", "t2")] == 1.0
        assert graph.metadata["t2"]["prospective_files"] == ["app/new.py", "app/other.py"]
        assert graph.metadata["t2"]["source_bytes"] == 0

    def test_depends_on_yields_directed_dependency_without_affinity(self):
        client, _ = client_with({})
        graph = build_task_graph(
            [
                TaskMapping("scaffold", prospective_files=("app/main.py",)),
                TaskMapping("consumer", prospective_files=("app/c.py",), depends_on=("scaffold",)),
            ],
            client,
        )
        assert graph.dependencies == {("scaffold", "consumer"): 1.0}
        assert graph.affinity == {}

    def test_scaffold_everyone_depends_on_becomes_utility_hub(self):
        """The smoke1 shape: a scaffold task everything depends on is isolated as
        its own group, scheduled first."""
        from orchestrator.grouping.partition import detect_hub_roles

        client, _ = client_with({})
        mappings = [TaskMapping("scaffold", prospective_files=("app/main.py",))] + [
            TaskMapping(f"t{i}", prospective_files=(f"app/f{i}.py",), depends_on=("scaffold",))
            for i in range(1, 4)
        ]
        graph = build_task_graph(mappings, client)
        assert detect_hub_roles(graph)["scaffold"] == "utility_hub"

    def test_matched_route_tags_produce_symmetric_semantic_edge(self):
        """The cross-stack fix: a TS task consuming what a Python task implements
        gets an affinity edge codegraph could never see. Affinity only — route
        tags carry no dependency direction."""
        client, _ = client_with({})
        graph = build_task_graph(
            [
                TaskMapping(
                    "py-route", prospective_files=("app/items.py",), implements=("/api/items",)
                ),
                TaskMapping(
                    "ts-page", prospective_files=("web/items.tsx",), consumes=("/api/items",)
                ),
            ],
            client,
            EdgeWeights(semantic=1.5, semantic_floor=0.5, semantic_ceil=3.0),
        )
        # Pure greenfield: Σstruct = 0 → the clamp floors the scale at 0.5.
        assert graph.affinity == {("py-route", "ts-page"): 1.5 * 0.5}
        assert graph.dependencies == {}

    def test_unmatched_tags_produce_no_edge(self):
        client, _ = client_with({})
        graph = build_task_graph(
            [
                TaskMapping("a", implements=("/api/x",)),
                TaskMapping("b", consumes=("/api/y",)),
                TaskMapping(
                    "c", implements=("/api/y",)
                ),  # implements twice, never consumed+implemented pair with a
            ],
            client,
        )
        assert ("a", "b") not in graph.affinity
        assert graph.affinity == {("b", "c"): pytest.approx(1.5 * 0.5)}

    def test_normalization_caps_semantics_on_edit_heavy_plans(self):
        """Σstruct ≫ Σsem → the clamp ceiling stops the semantic boost, so
        semantics refine but never override real reference edges."""
        client, _ = client_with({})
        shared = tuple(f"f{i}.py" for i in range(10))
        graph = build_task_graph(
            [
                TaskMapping("e1", files=shared),
                TaskMapping("e2", files=shared),
                TaskMapping("s1", implements=("/api/z",)),
                TaskMapping("s2", consumes=("/api/z",)),
            ],
            client,
            EdgeWeights(shared_file=1.0, semantic=1.5, semantic_ceil=3.0),
        )
        # Σstruct = 10 shared files × 1.0; Σsem = 1.5 → ratio 6.67 clamps to 3.0.
        assert graph.affinity[("s1", "s2")] == pytest.approx(1.5 * 3.0)

    def test_normalization_lands_between_bounds_on_mixed_plans(self):
        client, _ = client_with({})
        graph = build_task_graph(
            [
                TaskMapping("e1", files=("a.py", "b.py", "c.py")),
                TaskMapping("e2", files=("a.py", "b.py", "c.py")),
                TaskMapping("s1", prospective_files=("new1.py",), implements=("/api/m",)),
                TaskMapping("s2", prospective_files=("new2.py",), consumes=("/api/m",)),
            ],
            client,
            EdgeWeights(shared_file=1.0, semantic=1.5, semantic_floor=0.5, semantic_ceil=3.0),
        )
        # Σstruct = 3.0, Σsem = 1.5 → scale exactly 2.0, inside the clamp.
        assert graph.affinity[("s1", "s2")] == pytest.approx(1.5 * 2.0)

    def test_metadata_carries_slice_and_route_tags(self):
        client, _ = client_with({})
        graph = build_task_graph(
            [TaskMapping("t", slice="items", implements=("/api/items",), consumes=("Evt",))],
            client,
        )
        meta = graph.metadata["t"]
        assert meta["slice"] == "items"
        assert meta["implements"] == ["/api/items"]
        assert meta["consumes"] == ["Evt"]


class TestCapturedFixtures:
    """Real codegraph CLI output from this repo, captured 2026-07-15."""

    def fixture_client(self):
        responses = {}
        for path in FIXTURES.glob("*.json"):
            command, symbol = path.stem.split("_", 1)
            responses[(command, symbol)] = path.read_text()
        assert responses, "captured fixtures missing"
        return client_with(responses)

    def mappings(self):
        return [
            TaskMapping("server", files=("codegraph_mcp/server.py",), symbols=("_forward",)),
            TaskMapping("tests", files=("tests/test_codegraph_server.py",), symbols=("call_tool",)),
        ]

    def test_fixture_graph_builds_with_real_shapes(self):
        client, _ = self.fixture_client()
        graph = build_task_graph(self.mappings(), client)
        assert graph.nodes == {"server", "tests"}
        # _forward calls call_tool (tests' symbol) → server depends on tests.
        assert ("tests", "server") in graph.dependencies
        assert graph.affinity[("server", "tests")] > 0
        assert graph.metadata["server"]["source_bytes"] > 0
        assert graph.metadata["server"]["fan_in"] > 0

    def test_fixture_graph_feeds_partition_without_adaptation(self):
        """Verification: adapter output drives U1 directly."""
        client, _ = self.fixture_client()
        graph = build_task_graph(self.mappings(), client)
        partition = DefaultPartitionStrategy(budget_cap=100.0).partition(graph)
        assert set(partition) == {"server", "tests"}
