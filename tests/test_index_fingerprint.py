"""U5: the index fingerprint hashes a canonical logical export (sorted symbol
ids, file paths and edges), not `codegraph status -j`'s operational counters —
the exact bug that made the fingerprint churn three times in fifteen minutes
at one commit while `sync` reported "already up to date".
"""

import json

from orchestrator.grouping.graphing import CodegraphClient, index_fingerprint
from orchestrator.grouping.trace import ProvenanceEntry


def make_export(symbols=None, files=None, edges=None) -> dict:
    return {
        "symbols": symbols or [],
        "files": files or [],
        "edges": edges or [],
    }


class TestFingerprintStability:
    def test_same_export_produces_the_same_string(self):
        export = make_export(
            symbols=[{"id": "function:a", "name": "a"}],
            files=["a.py"],
            edges=[{"filePath": "a.py", "target": "b"}],
        )
        assert index_fingerprint(export) == index_fingerprint(export)

    def test_stable_across_separately_constructed_but_identical_exports(self):
        """No process-local state (no random seed, no insertion-order dependence
        beyond what canonicalization already handles) — a fresh dict built from
        scratch with the same content hashes the same as another."""
        export_a = make_export(
            symbols=[{"id": "function:a", "name": "a"}],
            files=["a.py"],
        )
        export_b = json.loads(json.dumps(export_a))  # round-trip: a fresh object
        assert index_fingerprint(export_a) == index_fingerprint(export_b)


class TestOperationalCountersIgnored:
    def test_changing_status_payload_alone_does_not_affect_fingerprint(self):
        """The fingerprint function no longer takes `status -j` at all — proof
        that queue depth, uptime and cache size (whatever `status -j` reports)
        cannot move it, however they vary."""
        export = make_export(symbols=[{"id": "function:a"}], files=["a.py"])
        fp_before = index_fingerprint(export)
        # Simulate two different `status -j` reads at the same content: nothing
        # about them is even inputs to index_fingerprint.
        status_a = {"nodeCount": 10, "edgeCount": 5, "dbSizeBytes": 4096}
        status_b = {"nodeCount": 11, "edgeCount": 6, "dbSizeBytes": 9999}
        assert status_a != status_b  # sanity: the counters really did change
        fp_after = index_fingerprint(export)
        assert fp_before == fp_after


class TestContentChangesFingerprint:
    def test_adding_one_symbol_changes_the_fingerprint(self):
        base = make_export(symbols=[{"id": "function:a", "name": "a"}], files=["a.py"])
        extra = make_export(
            symbols=[
                {"id": "function:a", "name": "a"},
                {"id": "function:b", "name": "b"},
            ],
            files=["a.py"],
        )
        assert index_fingerprint(base) != index_fingerprint(extra)

    def test_adding_one_file_changes_the_fingerprint(self):
        base = make_export(files=["a.py"])
        extra = make_export(files=["a.py", "b.py"])
        assert index_fingerprint(base) != index_fingerprint(extra)

    def test_adding_one_edge_changes_the_fingerprint(self):
        base = make_export(edges=[{"filePath": "a.py", "target": "b"}])
        extra = make_export(
            edges=[
                {"filePath": "a.py", "target": "b"},
                {"filePath": "a.py", "target": "c"},
            ]
        )
        assert index_fingerprint(base) != index_fingerprint(extra)


class TestOrderInsensitive:
    def test_reordering_symbols_leaves_fingerprint_unchanged(self):
        a = make_export(symbols=[{"id": "function:a"}, {"id": "function:b"}])
        b = make_export(symbols=[{"id": "function:b"}, {"id": "function:a"}])
        assert index_fingerprint(a) == index_fingerprint(b)

    def test_reordering_files_leaves_fingerprint_unchanged(self):
        a = make_export(files=["a.py", "b.py", "c.py"])
        b = make_export(files=["c.py", "a.py", "b.py"])
        assert index_fingerprint(a) == index_fingerprint(b)

    def test_reordering_edges_leaves_fingerprint_unchanged(self):
        a = make_export(
            edges=[
                {"filePath": "a.py", "target": "b"},
                {"filePath": "c.py", "target": "d"},
            ]
        )
        b = make_export(
            edges=[
                {"filePath": "c.py", "target": "d"},
                {"filePath": "a.py", "target": "b"},
            ]
        )
        assert index_fingerprint(a) == index_fingerprint(b)


class TestLogicalExport:
    """CodegraphClient.logical_export builds the shape index_fingerprint hashes
    from one bulk `codegraph query ""` call — no CLI command exports the whole
    edge graph in bulk, so import-kind nodes stand in for the edge layer."""

    def _runner(self, payload):
        def runner(args):
            if args[0] == "sync":
                return ""
            if args[0] == "query":
                return json.dumps(payload)
            raise AssertionError(f"unexpected call: {args}")

        return runner

    def test_import_nodes_become_edges_other_nodes_become_symbols(self, tmp_path):
        payload = [
            {
                "node": {
                    "id": "function:a",
                    "kind": "function",
                    "name": "a",
                    "qualifiedName": "a",
                    "filePath": "a.py",
                    "signature": "def a(): ...",
                }
            },
            {
                "node": {
                    "id": "import:xyz",
                    "kind": "import",
                    "name": "os",
                    "filePath": "a.py",
                }
            },
        ]
        client = CodegraphClient(repo_root=tmp_path, runner=self._runner(payload))
        export = client.logical_export()
        assert len(export["symbols"]) == 1
        assert export["symbols"][0]["id"] == "function:a"
        assert len(export["edges"]) == 1
        assert export["edges"][0] == {"filePath": "a.py", "target": "os"}
        assert export["files"] == ["a.py"]

    def test_bulk_query_bypasses_the_per_argv_cache(self, tmp_path):
        """Each call to logical_export must issue a fresh subprocess-equivalent
        read — the quiescence handshake polls it repeatedly and a cached first
        answer would make drift undetectable."""
        calls = {"n": 0}

        def runner(args):
            if args[0] == "sync":
                return ""
            if args[0] == "query":
                calls["n"] += 1
                return json.dumps([{"node": {"id": f"function:{calls['n']}", "kind": "function"}}])
            raise AssertionError(f"unexpected call: {args}")

        client = CodegraphClient(repo_root=tmp_path, runner=runner)
        first = client.logical_export()
        second = client.logical_export()
        assert calls["n"] == 2
        assert first != second


class TestProvenanceCarriesLouvainParameters:
    def test_provenance_entry_accepts_seed_and_resolution(self):
        entry = ProvenanceEntry(
            timestamp="2026-08-27T00:00:00+00:00",
            plan_path="plan.md",
            plan_content_sha256="0" * 64,
            repo_commit_sha="deadbeef",
            worktree_dirty=False,
            index_fingerprint="1" * 64,
            louvain_seed=42,
            louvain_resolution=1.0,
        )
        assert entry.louvain_seed == 42
        assert entry.louvain_resolution == 1.0
