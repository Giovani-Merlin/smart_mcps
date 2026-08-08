"""Guards against the Observatory drifting away from the orchestrator's models.

The Observatory is a *reader* of another package's data model, and the two
evolve on different branches. That has already bitten: ``RunPaths.groups_path``
was proposed for removal while ``runs.py`` and ``events.py`` both dereferenced
it directly, which would have turned ``/snapshot`` and ``/events/run`` into
``AttributeError`` 500s for every run — a failure that only shows up when a
request arrives, since nothing imports the attribute at module load.

So the drift is checked structurally instead. ``test_run_paths_attribute_audit``
parses the Observatory's own source for every attribute it reads off a
``RunPaths`` and asserts the orchestrator still exposes it. When the next
attribute goes, this test fails in CI rather than the endpoint failing in the
operator's browser.

The sibling ``test_observatory_model_drift.py`` applies the same idea one layer
up, to the enum members the UI has to style.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

from orchestrator.execution.manifest import RunPaths
from orchestrator.observatory import events, runs

OBSERVATORY_DIR = Path(runs.__file__).parent

# Local names that hold a ``RunPaths`` in the Observatory's source. Anything
# read off one of these must exist on the real class.
RUN_PATHS_LOCALS = frozenset({"paths", "run_paths"})


def _observatory_sources() -> list[Path]:
    return sorted(p for p in OBSERVATORY_DIR.glob("*.py") if p.name != "__init__.py")


def _run_paths_attributes(source: Path) -> set[str]:
    """Every ``paths.<attr>`` the file reads, by AST rather than regex."""
    tree = ast.parse(source.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in RUN_PATHS_LOCALS
        ):
            found.add(node.attr)
    return found


def test_run_paths_attribute_audit() -> None:
    # A probe instance, not the class: ``run_id`` and ``repo_root`` are set in
    # ``__init__`` and would look "removed" to a class-only audit.
    available = set(dir(RunPaths)) | set(vars(RunPaths(Path("/nonexistent"), "probe")))
    missing: dict[str, set[str]] = {}
    for source in _observatory_sources():
        gone = _run_paths_attributes(source) - available
        if gone:
            missing[source.name] = gone
    assert not missing, (
        "the Observatory reads RunPaths attributes the orchestrator no longer exposes: "
        f"{ {name: sorted(attrs) for name, attrs in missing.items()} } — "
        "add a resolution helper (see runs.run_groups_path) and update this audit"
    )


def test_audit_notices_a_removed_attribute() -> None:
    """The audit is only worth having if it can actually fail — prove it does."""
    source = ast.parse("x = paths.groups_path\ny = paths.state_path\n")
    read = {
        node.attr
        for node in ast.walk(source)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in RUN_PATHS_LOCALS
    }
    assert read == {"groups_path", "state_path"}
    assert read - {"state_path"} == {"groups_path"}


def test_run_groups_path_survives_the_attribute_going_away(tmp_path: Path) -> None:
    """The helper degrades to the literal layout instead of raising."""

    class Stripped:
        """A RunPaths whose ``groups_path`` was removed upstream."""

        def __init__(self, run_dir: Path) -> None:
            self.run_dir = run_dir

    stripped_dir = tmp_path / "runs" / "r1"
    stripped_dir.mkdir(parents=True)
    assert runs.run_groups_path(Stripped(stripped_dir)) == stripped_dir / "groups.json"

    real = RunPaths(tmp_path, "r1")
    assert runs.run_groups_path(real) == real.groups_path
    assert runs.run_groups_path(real) == real.run_dir / "groups.json"


def test_both_endpoints_use_the_shared_helper() -> None:
    """``groups.json`` is named once. A second literal is how the two call sites
    drifted apart in the first place."""
    events_source = Path(events.__file__).read_text()
    assert '"groups.json"' not in events_source, (
        "events.py names groups.json itself; route it through runs.run_groups_path"
    )
    assert "run_groups_path(paths)" in events_source

    # runs.py may name it twice and only twice: once in the helper's fallback,
    # once for the shared `.orchestrator/groups.json` the stale path reads.
    assert Path(runs.__file__).read_text().count('"groups.json"') == 2


# --------------------------------------------------------------- endpoints alive


def test_snapshot_and_run_stream_are_alive_on_a_real_run(tmp_path: Path) -> None:
    """The two endpoints F4 predicted would 500. Both are exercised end to end
    against a run directory copied from disk, so a regression in the path
    resolution shows up as a failing status code rather than a quiet 500.
    """
    from fastapi.testclient import TestClient

    from orchestrator.observatory.app import create_app
    from tests.test_observatory_api import install_run

    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "r1")
    client = TestClient(
        create_app(
            registry_path=tmp_path / "no-registry.yaml",
            fallback_repo=repo,
            dist_dir=tmp_path / "no-dist",
        )
    )

    snapshot = client.get("/api/projects/proj/runs/r1/snapshot")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["groups"], "the snapshot resolved no groups at all"
    assert snapshot.json()["stale_dag"] is False

    # ``/events/run`` never completes, so it is driven the way the SSE suite
    # drives it — the handler is entered and its response built, which is where
    # the DAG snapshot path was dereferenced.
    request = SimpleNamespace(app=SimpleNamespace(state=client.app.state))
    response = asyncio.run(events.stream_run(request, project="proj", run="r1"))
    assert response.status_code == 200


def test_run_stream_signature_covers_the_dag_snapshot(tmp_path: Path) -> None:
    """A regressed ``run_groups_path`` would silently stop noticing DAG rewrites
    rather than crash, so assert the signature actually moves."""
    from orchestrator.execution.manifest import RunPaths as Paths
    from orchestrator.observatory.events import _signature

    run_dir = tmp_path / ".orchestrator" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    paths = Paths(tmp_path, "r1")
    before = _signature(paths)
    runs.run_groups_path(paths).write_text('{"plan_path": "p", "groups": []}')
    assert _signature(paths) != before
