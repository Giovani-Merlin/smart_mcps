"""The Observatory app factory (plan U2).

``create_app`` wires three core read endpoints and includes the four slice
routers. Those routers are included *unconditionally and once*, here: the slice
units fill their own module with routes and nothing in this file changes, so no
file in the Observatory is edited by two units.

Route order matters at the bottom of this file — the SPA catch-all is registered
last, after every router and the static mount, because a route matching ``/{path}``
registered earlier would swallow ``/api``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.routing import Match

from orchestrator.observatory import (
    artifacts,
    escalations,
    events,
    grouping,
    launch,
    transcripts,
)

# Aliased deliberately: the bare name ``paths`` means "a RunPaths" everywhere
# else in this package, and ``test_observatory_drift``'s attribute audit reads
# it that way. ``paths_api.router`` keeps both true.
from orchestrator.observatory import paths as paths_api
from orchestrator.observatory.registry import Project
from orchestrator.observatory.runs import (
    ObservatoryContext,
    RunInfo,
    RunSnapshot,
    build_snapshot,
    list_projects,
    list_runs,
    resolve_repo,
    resolve_run,
)

DEV_RECIPE = (
    "Observatory API is running. No built SPA found at ui/dist — either build it "
    "(cd ui && npm install && npm run build) or run the dev server "
    "(cd ui && npm run dev) and open http://127.0.0.1:5173, which proxies /api "
    "and /events here."
)


def default_dist_dir() -> Path:
    """``ui/dist`` in the repo this package was installed from."""
    return Path(__file__).resolve().parents[2] / "ui" / "dist"


def create_app(
    *,
    registry_path: Path | None = None,
    fallback_repo: Path | None = None,
    dist_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Orchestrator Observatory", version="1")
    app.state.observatory = ObservatoryContext(
        registry_path=registry_path, fallback_repo=fallback_repo
    )

    @app.get("/api/projects", response_model=list[Project])
    def get_projects(request: Request) -> list[Project]:
        """Registry entries in file order. A registry that does not exist is an
        empty list, not an error — it is the state before an operator has
        registered anything."""
        return list_projects(request)

    @app.get("/api/projects/{project}/runs", response_model=list[RunInfo])
    def get_runs(request: Request, project: str) -> list[RunInfo]:
        return list_runs(resolve_repo(request, project))

    @app.get("/api/projects/{project}/runs/{run_id}/snapshot", response_model=RunSnapshot)
    def get_snapshot(request: Request, project: str, run_id: str) -> RunSnapshot:
        return build_snapshot(resolve_run(request, project, run_id), project)

    app.include_router(events.router)
    app.include_router(escalations.router)
    app.include_router(transcripts.router)
    app.include_router(artifacts.router)
    app.include_router(grouping.router)
    app.include_router(grouping.preview_router)
    app.include_router(paths_api.router)
    # The launch surface: two routers because its SSE job-log stream belongs
    # under /events with the run streams, not under the project prefix.
    app.include_router(launch.router)
    app.include_router(launch.events_router)

    _mount_spa(app, dist_dir if dist_dir is not None else default_dist_dir())
    return app


def _leaf_routes(routes) -> list:
    """Every concrete route, with included-router wrappers flattened away.

    ``include_router`` does not splice its routes into ``app.routes`` on this
    FastAPI version — it appends one wrapper object that carries them under
    ``original_router``. A wrapper has no ``path`` and no ``methods``, so both
    callers below would silently see an app with only three ``/api`` routes on
    it. ``route_paths`` in ``test_observatory_api`` recurses the same two ways.
    """
    leaves = []
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            leaves.extend(_leaf_routes(included.routes))
        else:
            leaves.append(route)
    return leaves


def _server_prefixes(app: FastAPI) -> frozenset[str]:
    """The first path segment of every route the server itself owns.

    Derived rather than hard-coded so a router added later (``/grouping``,
    ``/file``, the next one) is covered without editing this file. Segments that
    are path parameters are skipped — they are not a fixed prefix.
    """
    segments = set()
    for route in _leaf_routes(app.routes):
        first = (getattr(route, "path", "") or "").lstrip("/").split("/", 1)[0]
        if first and "{" not in first:
            segments.add(first)
    return frozenset(segments)


def _allowed_methods(app: FastAPI, request: Request) -> set[str]:
    """Methods accepted by a route whose *path* matches but whose method does not.

    Starlette answers 405 itself from these partial matches, but only when no
    route fully matches. The SPA catch-all fully matches every GET, so a GET
    against a POST-only endpoint would reach us instead — this reconstructs the
    405 the operator should have seen.
    """
    allowed: set[str] = set()
    for route in _leaf_routes(app.routes):
        methods = getattr(route, "methods", None)
        if methods and route.matches(request.scope)[0] is Match.PARTIAL:
            allowed.update(methods)
    return allowed


def _asset_under(root: Path, relative: str) -> Path | None:
    """``root/relative`` when it is a real file that has not escaped ``root``."""
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        return None
    candidate = (root / relative).resolve()
    if not candidate.is_file() or not candidate.is_relative_to(root.resolve()):
        return None
    return candidate


def _mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Serve the built SPA when it exists; otherwise say how to get one.

    The client router owns paths like ``/p/proj/r/run/grouping``, which exist on
    no server route: a refresh of one has to reach ``index.html``. That is a
    catch-all, and a catch-all is exactly what must not swallow the API — an
    ``/api`` typo answered with HTML is a worse bug than the 404 being fixed. So
    anything under a prefix the server owns gets a real JSON 404 (or the 405 it
    earned), and only navigation paths fall through to the bundle.

    ``dist/`` is gitignored and no build step is wired into the Python entry
    point, so a fresh checkout legitimately has no bundle — that must start
    fine and explain itself rather than 404 or raise.
    """
    server_prefixes = _server_prefixes(app)
    index = dist_dir / "index.html"
    have_build = dist_dir.is_dir() and index.is_file()

    if have_build and (dist_dir / "assets").is_dir():
        # Imported lazily: only the static path needs it.
        from fastapi.staticfiles import StaticFiles

        # Vite's hashed bundles. Mounted before the catch-all so a missing asset
        # 404s as an asset instead of quietly returning the HTML shell.
        app.mount(
            "/assets",
            StaticFiles(directory=str(dist_dir / "assets")),
            name="spa-assets",
        )

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa_fallback(request: Request, spa_path: str) -> Response:
        if spa_path.split("/", 1)[0] in server_prefixes:
            allowed = _allowed_methods(app, request)
            if allowed:
                return JSONResponse(
                    {"detail": "Method Not Allowed"},
                    status_code=405,
                    headers={"Allow": ", ".join(sorted(allowed))},
                )
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        if not have_build:
            return JSONResponse({"message": DEV_RECIPE, "dist_dir": str(dist_dir)})

        asset = _asset_under(dist_dir, spa_path)
        if asset is not None:
            return FileResponse(asset)
        return FileResponse(index, media_type="text/html")
