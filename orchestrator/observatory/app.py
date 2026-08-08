"""The Observatory app factory (plan U2).

``create_app`` wires three core read endpoints and includes the four slice
routers. Those routers are included *unconditionally and once*, here: the slice
units fill their own module with routes and nothing in this file changes, so no
file in the Observatory is edited by two units.

Route order matters at the bottom of this file — the SPA is mounted at ``/``
last, because a mount at ``/`` registered earlier would swallow ``/api``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from orchestrator.observatory import artifacts, escalations, events, grouping, transcripts
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

    _mount_spa(app, dist_dir if dist_dir is not None else default_dist_dir())
    return app


def _mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Serve the built SPA when it exists; otherwise say how to get one.

    ``dist/`` is gitignored and no build step is wired into the Python entry
    point, so a fresh checkout legitimately has no bundle — that must start
    fine and explain itself rather than 404.
    """
    if dist_dir.is_dir() and (dist_dir / "index.html").is_file():
        # Imported lazily: only the static path needs it.
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="spa")
        return

    @app.get("/", include_in_schema=False)
    def dev_recipe() -> JSONResponse:
        return JSONResponse({"message": DEV_RECIPE, "dist_dir": str(dist_dir)})
