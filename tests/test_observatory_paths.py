"""The path/file API: display-only strings, and bytes behind a root key.

The interesting half of this file is adversarial. ``/file``'s whole contract is
that a client can never name a directory to read from, so the tests below
actually attempt the attacks — a client-supplied root, ``..`` climbing, an
absolute path, and a symlink planted inside the run directory pointing out of
it — rather than asserting the happy path and calling the defence tested.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.execution.manifest import RunPaths
from orchestrator.observatory import paths as paths_module
from orchestrator.observatory.app import create_app
from orchestrator.observatory.paths import (
    MAX_FILE_BYTES,
    FileAccessError,
    check_relative,
    file_roots,
    read_capped,
    resolve_file,
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


def paths_of(client: TestClient, run_id: str = "modern1") -> dict:
    response = client.get(f"/api/projects/proj/runs/{run_id}/paths")
    assert response.status_code == 200, response.text
    return response.json()


def fetch(client: TestClient, root: str, path: str, run_id: str = "modern1"):
    return client.get(f"/api/projects/proj/runs/{run_id}/file", params={"root": root, "path": path})


def code_of(response) -> str:
    return response.json()["detail"]["code"]


# --------------------------------------------------------------------- /paths


class TestPathsEndpoint:
    def test_names_every_file_backed_panel_source(self, client):
        body = paths_of(client)
        keys = {entry["key"] for entry in body["entries"]}
        assert {
            "run_dir",
            "manifest",
            "state",
            "groups",
            "event_log",
            "grouping_dir",
            "trace",
            "edge_provenance",
            "base_context",
        } <= keys
        assert body["run_id"] == "modern1"

    def test_paths_are_absolute_strings(self, client, repo):
        body = paths_of(client)
        manifest = next(e for e in body["entries"] if e["key"] == "manifest")
        assert manifest["path"] == str(
            repo / ".orchestrator" / "runs" / "modern1" / "manifest.json"
        )
        assert Path(manifest["path"]).is_absolute()

    def test_missing_artifacts_are_listed_with_their_expected_path(self, client):
        """The whole point of a chip on an absent file: the operator's next move
        is to go look there."""
        body = paths_of(client)
        provenance = next(e for e in body["entries"] if e["key"] == "edge_provenance")
        assert provenance["exists"] is False
        assert provenance["path"].endswith("edge-provenance.json")

        present = next(e for e in body["entries"] if e["key"] == "manifest")
        assert present["exists"] is True

    def test_returns_no_file_contents(self, client):
        """Display-only by construction: nothing in the body may be a payload."""
        body = paths_of(client)
        blob = json.dumps(body)
        assert "content" not in body
        # The fixture manifest's own text must not have leaked into the listing.
        manifest_text = (
            Path(next(e for e in body["entries"] if e["key"] == "manifest")["path"])
        ).read_text()
        assert manifest_text[:200] not in blob
        for entry in body["entries"]:
            assert set(entry) == {
                "key",
                "label",
                "panel",
                "path",
                "kind",
                "exists",
                "root",
                "rel",
                "description",
            }

    def test_entries_carry_the_root_key_and_rel_that_file_takes(self, client):
        body = paths_of(client)
        manifest = next(e for e in body["entries"] if e["key"] == "manifest")
        assert manifest["root"] in body["roots"]
        assert manifest["rel"] == "manifest.json"
        response = fetch(client, manifest["root"], manifest["rel"])
        assert response.status_code == 200

    def test_directories_are_not_offered_as_fetchable(self, client):
        body = paths_of(client)
        for entry in body["entries"]:
            if entry["kind"] == "directory":
                assert entry["root"] is None and entry["rel"] is None

    def test_transcripts_are_shown_but_not_servable(self, client):
        body = paths_of(client)
        sessions = [e for e in body["entries"] if e["panel"] == "sessions"]
        assert sessions, "the fixture run has sessions with transcript paths"
        for entry in sessions:
            assert entry["root"] is None

    def test_a_legacy_run_still_lists(self, client):
        body = paths_of(client, "legacy1")
        assert {e["key"] for e in body["entries"]} >= {"run_dir", "manifest", "trace"}

    def test_unknown_run_is_404(self, client):
        assert client.get("/api/projects/proj/runs/nope/paths").status_code == 404


# ------------------------------------------------------- root keys, not paths


class TestRootIsAKey:
    def test_only_allowlisted_keys_resolve(self, client):
        body = paths_of(client)
        assert set(body["roots"]) == {"run", "grouping"}
        assert fetch(client, "run", "manifest.json").status_code == 200

    def test_a_client_supplied_directory_is_not_a_root(self, client, tmp_path):
        """The attack this endpoint is shaped to prevent: naming the base."""
        secret = tmp_path / "secret.txt"
        secret.write_text("classified")
        for attempt in (str(tmp_path), str(secret), "/", "/etc", "../..", "~"):
            response = fetch(client, attempt, "secret.txt")
            assert response.status_code == 404, attempt
            assert code_of(response) == "unknown_root", attempt

    def test_unknown_key_is_rejected_distinctly(self, client):
        response = fetch(client, "nonsense", "manifest.json")
        assert response.status_code == 404
        assert code_of(response) == "unknown_root"
        assert "server-side keys" in response.json()["detail"]["message"]

    def test_the_registry_resolves_to_real_directories(self, repo):
        roots = file_roots(RunPaths(repo, "modern1"))
        assert set(roots) == {"run", "grouping"}
        assert all(path.is_dir() for path in roots.values())


# --------------------------------------------------------- traversal defences


class TestTraversalRejected:
    @pytest.mark.parametrize(
        "attempt",
        [
            "../../../../etc/passwd",
            "..",
            "../state.json",
            "groups/../../../secret.txt",
            "/etc/passwd",
            "/tmp/secret.txt",
            "\\windows\\system32",
            "C:\\windows\\system32",
            "",
        ],
    )
    def test_dotdot_and_absolute_paths_are_rejected(self, client, attempt):
        response = fetch(client, "run", attempt)
        assert response.status_code == 400, response.text
        assert code_of(response) == "rejected_path"

    def test_rejection_precedes_any_file_lookup(self, client, monkeypatch, tmp_path):
        """Rejection must happen *before* the path is resolved or read.

        Proven by making both of those explode: if the request still comes back
        as ``rejected_path``, neither ran. (Root resolution, which stats the run
        directory, legitimately happens first — it decides whether the key is
        even known.)
        """

        def boom(*args, **kwargs):
            raise AssertionError("filesystem touched before the path was rejected")

        monkeypatch.setattr(paths_module.Path, "resolve", boom)
        monkeypatch.setattr(paths_module, "read_capped", boom)
        response = fetch(client, "run", "../../etc/passwd")
        assert response.status_code == 400
        assert code_of(response) == "rejected_path"

    def test_check_relative_is_pure(self):
        assert check_relative("manifest.json") is None
        assert check_relative("groups/g1/report-g1-r1.json") is None
        assert check_relative("../x") == "rejected_path"
        assert check_relative("/x") == "rejected_path"
        assert check_relative("a/../../b") == "rejected_path"
        assert check_relative("") == "rejected_path"
        assert check_relative("a\x00b") == "rejected_path"

    def test_a_legitimate_dot_prefixed_name_still_works(self, repo):
        """``..`` is rejected; a file whose name merely starts with a dot is not."""
        run_dir = RunPaths(repo, "modern1").run_dir
        (run_dir / ".hidden.json").write_text("{}")
        assert resolve_file(RunPaths(repo, "modern1"), "run", ".hidden.json").name == ".hidden.json"


class TestSymlinkEscapeBlocked:
    def test_a_symlink_out_of_the_run_directory_is_refused(self, client, repo, tmp_path):
        """The case the cheap gate cannot catch: no ``..``, not absolute."""
        secret = tmp_path / "outside-secret.txt"
        secret.write_text("classified")
        link = RunPaths(repo, "modern1").run_dir / "escape.json"
        link.symlink_to(secret)
        assert link.read_text() == "classified", "the symlink is real and readable on disk"

        response = fetch(client, "run", "escape.json")
        assert response.status_code == 403, response.text
        assert code_of(response) == "outside_root"
        assert "classified" not in response.text

    def test_a_symlinked_directory_out_of_the_run_directory_is_refused(
        self, client, repo, tmp_path
    ):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("classified")
        (RunPaths(repo, "modern1").run_dir / "away").symlink_to(outside)

        response = fetch(client, "run", "away/secret.txt")
        assert response.status_code == 403
        assert code_of(response) == "outside_root"
        assert "classified" not in response.text

    def test_a_symlink_staying_inside_the_root_is_served(self, client, repo):
        run_dir = RunPaths(repo, "modern1").run_dir
        (run_dir / "alias.json").symlink_to(run_dir / "manifest.json")
        response = fetch(client, "run", "alias.json")
        assert response.status_code == 200
        assert json.loads(response.json()["content"])["run_id"] == "modern1"


# ------------------------------------------------------- distinguishable cases


class TestDistinguishableErrors:
    def test_unknown_root_rejected_path_and_missing_file_all_differ(self, client):
        unknown = fetch(client, "no-such-root", "manifest.json")
        rejected = fetch(client, "run", "../etc/passwd")
        missing = fetch(client, "run", "not-written-yet.json")

        assert code_of(unknown) == "unknown_root"
        assert code_of(rejected) == "rejected_path"
        assert code_of(missing) == "not_found"
        codes = {code_of(r) for r in (unknown, rejected, missing)}
        assert len(codes) == 3
        statuses = {unknown.status_code, rejected.status_code, missing.status_code}
        assert statuses == {404, 400}  # codes disambiguate the two 404s

    def test_missing_but_valid_says_where_it_looked(self, client):
        response = fetch(client, "run", "edge-provenance.json")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["code"] == "not_found"
        assert detail["message"].endswith("edge-provenance.json")

    def test_a_directory_is_not_a_file_rather_than_a_listing(self, client):
        response = fetch(client, "run", "groups")
        assert response.status_code == 400
        assert code_of(response) == "not_a_file"


# --------------------------------------------------------- read-only and cap


class TestReadOnlyAndCapped:
    def test_no_write_verbs_are_registered(self, client):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(client, method)(
                "/api/projects/proj/runs/modern1/file?root=run&path=manifest.json"
            )
            assert response.status_code == 405, method

    def test_reading_leaves_the_run_directory_untouched(self, client, repo):
        run_dir = RunPaths(repo, "modern1").run_dir
        before = {p.name: p.stat().st_mtime_ns for p in sorted(run_dir.rglob("*"))}
        assert fetch(client, "run", "manifest.json").status_code == 200
        after = {p.name: p.stat().st_mtime_ns for p in sorted(run_dir.rglob("*"))}
        assert before == after

    def test_no_directory_listing_is_exposed(self, client):
        """Neither an empty rel nor a directory yields an index."""
        for attempt in ("", ".", "groups", "logs"):
            response = fetch(client, "run", attempt)
            assert response.status_code == 400, attempt
            assert code_of(response) in ("rejected_path", "not_a_file"), attempt

    def test_an_oversized_file_is_capped_and_says_so(self, client, repo):
        big = RunPaths(repo, "modern1").run_dir / "big.json"
        big.write_text("x" * (MAX_FILE_BYTES + 4096))
        body = fetch(client, "run", "big.json").json()
        assert body["truncated"] is True
        assert body["returned_bytes"] == MAX_FILE_BYTES
        assert body["size_bytes"] == MAX_FILE_BYTES + 4096
        assert len(body["content"]) == MAX_FILE_BYTES

    def test_a_small_file_is_not_marked_truncated(self, client):
        body = fetch(client, "run", "manifest.json").json()
        assert body["truncated"] is False
        assert body["returned_bytes"] == body["size_bytes"]

    def test_read_capped_reports_honestly(self, tmp_path):
        path = tmp_path / "f.txt"
        path.write_bytes(b"abcdef")
        assert read_capped(path, limit=3) == ("abc", 6, 3, True)
        assert read_capped(path, limit=100) == ("abcdef", 6, 6, False)

    def test_undecodable_bytes_do_not_raise(self, client, repo):
        (RunPaths(repo, "modern1").run_dir / "raw.bin").write_bytes(b"\xff\xfe ok")
        body = fetch(client, "run", "raw.bin").json()
        assert body["encoding"] == "utf-8-replace"
        assert "ok" in body["content"]


class TestGroupingRoot:
    def test_the_grouping_root_serves_the_trace(self, client):
        body = paths_of(client)
        trace = next(e for e in body["entries"] if e["key"] == "trace")
        response = fetch(client, trace["root"], trace["rel"])
        assert response.status_code == 200
        assert json.loads(response.json()["content"])["schema_version"] == 1

    def test_resolve_file_raises_the_typed_error(self, repo):
        with pytest.raises(FileAccessError) as excinfo:
            resolve_file(RunPaths(repo, "modern1"), "elsewhere", "x.json")
        assert excinfo.value.code == "unknown_root"
