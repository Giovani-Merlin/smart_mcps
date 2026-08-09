"""The SPA fallback: a deep client route survives a refresh, and the API does not
turn into HTML while that happens.

The router's whole premise is that ``/p/proj/r/run/grouping`` is a real URL an
operator can bookmark, share and reload. Nothing on the server answers that path,
so the server has to hand it to the bundle. The risk the tests below pin down is
the other half: a catch-all that also answers ``/api/typo`` with ``index.html``
makes every API mistake look like a working page.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.observatory.app import _asset_under, create_app

from test_observatory_api import install_run, write_registry

INDEX_HTML = (
    '<!doctype html><html><body><div id="root"></div>'
    '<script src="/assets/app.js"></script></body></html>'
)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A minimal stand-in for a Vite build: an entry point and a hashed asset."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML)
    (dist / "assets" / "app.js").write_text("export const observatory = 1;\n")
    (dist / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    return dist


@pytest.fixture
def client(tmp_path: Path, dist: Path) -> TestClient:
    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "smoke1")
    registry = write_registry(tmp_path, [("proj", repo)])
    return TestClient(create_app(registry_path=registry, dist_dir=dist))


class TestDeepLinkRefresh:
    @pytest.mark.parametrize(
        "path",
        [
            "/p/proj",
            "/p/proj/r/smoke1",
            "/p/proj/r/smoke1/grouping",
            "/p/proj/r/smoke1/history?group=g2&seq=3",
            "/p/proj/r/smoke1/sessions/abc-123",
        ],
    )
    def test_a_deep_client_route_serves_the_spa_entry_point(self, client, path):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert 'id="root"' in response.text

    def test_the_root_still_serves_the_spa(self, client):
        assert 'id="root"' in client.get("/").text


class TestTheApiIsNotSwallowed:
    def test_an_unknown_api_path_is_a_json_404(self, client):
        response = client.get("/api/projects/proj/runs/smoke1/nope")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert "<html" not in response.text

    def test_an_unknown_api_root_path_is_a_json_404(self, client):
        response = client.get("/api/typo")
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}

    def test_an_unknown_events_path_is_a_json_404(self, client):
        """``/events`` is a server prefix too, and it is not under ``/api``."""
        assert client.get("/events/nope").status_code == 404

    def test_a_wrong_method_on_a_known_route_is_405(self, client):
        response = client.post("/api/projects")
        assert response.status_code == 405

    def test_a_get_against_a_post_only_route_is_405_not_html(self, client):
        """The case the catch-all would otherwise steal: it fully matches the GET,
        so Starlette never gets to answer 405 on its own."""
        response = client.get("/api/projects/proj/runs/smoke1/escalations/e1/answer")
        assert response.status_code == 405
        assert "POST" in response.headers["allow"]
        assert "<html" not in response.text

    def test_a_real_api_route_still_answers(self, client):
        assert [p["name"] for p in client.get("/api/projects").json()] == ["proj"]


class TestStaticAssets:
    def test_a_hashed_asset_keeps_its_content_type(self, client):
        response = client.get("/assets/app.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert "observatory" in response.text

    def test_a_root_level_asset_keeps_its_content_type(self, client):
        response = client.get("/favicon.svg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")

    def test_a_missing_asset_404s_instead_of_returning_the_shell(self, client):
        """An asset request answered with HTML is how a stale bundle turns into a
        syntax error in the console instead of a 404 in the network tab."""
        assert client.get("/assets/gone.js").status_code == 404

    def test_the_catch_all_is_registered_after_every_router_and_the_mount(self, tmp_path, dist):
        app = create_app(registry_path=tmp_path / "nope.yaml", dist_dir=dist)
        paths = [getattr(route, "path", "") for route in app.routes]
        assert paths[-1] == "/{spa_path:path}"
        assert paths.index("/assets") < len(paths) - 1
        assert all(not p.startswith("/api") or i < len(paths) - 1 for i, p in enumerate(paths))

    @pytest.mark.parametrize("relative", ["../secret.txt", "a/../../secret.txt", "/etc/hosts", ""])
    def test_no_path_escapes_the_build_directory(self, dist, relative):
        (dist.parent / "secret.txt").write_text("nope")
        assert _asset_under(dist, relative) is None

    def test_a_symlink_out_of_the_build_directory_is_refused(self, dist):
        outside = dist.parent / "secret.txt"
        outside.write_text("nope")
        (dist / "link.txt").symlink_to(outside)
        assert _asset_under(dist, "link.txt") is None


class TestWithoutABuild:
    """A fresh checkout has no ``ui/dist`` — every route must explain that rather
    than raise or 404."""

    @pytest.fixture
    def bare(self, tmp_path: Path) -> TestClient:
        return TestClient(
            create_app(registry_path=tmp_path / "nope.yaml", dist_dir=tmp_path / "no-dist")
        )

    def test_a_deep_link_names_the_dev_recipe(self, bare):
        response = bare.get("/p/proj/r/smoke1/grouping")
        assert response.status_code == 200
        body = response.json()
        assert "npm run build" in body["message"]
        assert body["dist_dir"].endswith("no-dist")

    def test_the_api_still_404s_properly_without_a_build(self, bare):
        assert bare.get("/api/typo").status_code == 404
