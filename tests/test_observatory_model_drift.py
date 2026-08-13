"""Mirror audits: every model member the orchestrator grew must be styled in the UI.

A ``GroupState`` or ``EscalationKind`` the Observatory never learned renders as an
unstyled badge — worse than an error, because it looks like it worked. These tests
read ``ui/src/types.ts`` as text rather than running the TypeScript compiler: the
union literal is the contract, and a missing member is a missing string.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.execution.denial import DenialKind
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import CoderReport, EscalationKind, HumanAction, SessionEntry
from orchestrator.observatory import artifacts, escalations, events, grouping, transcripts
from orchestrator.observatory import runs

UI_TYPES = Path(__file__).resolve().parents[1] / "ui" / "src" / "types.ts"
OBSERVATORY_DIR = Path(runs.__file__).parent


def _observatory_sources() -> list[Path]:
    return sorted(p for p in OBSERVATORY_DIR.glob("*.py") if p.name != "__init__.py")


def _ui_types_source() -> str:
    return UI_TYPES.read_text()


@pytest.mark.parametrize("member", [state.value for state in GroupState])
def test_every_group_state_is_known_to_the_ui(member: str) -> None:
    """A state missing from the union renders with no label and no colour."""
    source = _ui_types_source()
    assert f'"{member}"' in source, f"GroupState.{member} is missing from ui/src/types.ts"


@pytest.mark.parametrize("member", [kind.value for kind in EscalationKind])
def test_every_escalation_kind_is_known_to_the_ui(member: str) -> None:
    source = _ui_types_source()
    assert f'"{member}"' in source, f"EscalationKind.{member} is missing from ui/src/types.ts"


@pytest.mark.parametrize("member", [action.value for action in HumanAction])
def test_every_human_action_is_known_to_the_ui(member: str) -> None:
    source = _ui_types_source()
    assert f'"{member}"' in source, f"HumanAction.{member} is missing from ui/src/types.ts"


def test_coder_report_statuses_are_known_to_the_ui() -> None:
    """``permission_denied`` is the newest one and the easiest to miss."""
    statuses = CoderReport.model_fields["status"].annotation.__args__
    source = _ui_types_source()
    for status in statuses:
        assert f'"{status}"' in source, f"CoderReport status {status!r} is missing from types.ts"


def test_every_denial_kind_is_known_to_the_ui() -> None:
    """A kind the UI never learned renders as an unstyled chip — worse than an
    error, because it looks like it worked. Same rule as GroupState, and the
    reason for the whole file."""
    source = _ui_types_source()
    for kind in DenialKind:
        assert f'"{kind.value}"' in source, f"DenialKind.{kind.value} is missing from types.ts"


def test_denial_report_fields_reach_the_ui() -> None:
    """The kind is derived from these two, so a client that cannot see them cannot
    show an operator *why* it was classified that way."""
    source = _ui_types_source()
    for field in ("denial_error", "denial_source"):
        assert field in CoderReport.model_fields
        assert field in source, f"CoderReport.{field} is missing from ui/src/types.ts"
    for value in CoderReport.model_fields["denial_source"].annotation.__args__:
        assert f'"{value}"' in source, f"denial_source {value!r} is missing from types.ts"
    # And the server-derived field on the artifact envelope itself.
    assert "denial_kind" in artifacts.Artifact.model_fields
    assert "denial_kind" in source


def test_session_entry_cost_fields_reach_the_ui() -> None:
    """The token-class split is only useful if the client can see all four."""
    source = _ui_types_source()
    for field in (
        "last_context_tokens",
        "total_input_tokens",
        "total_output_tokens",
        "total_cache_read_tokens",
        "total_cache_creation_tokens",
        "rounds_completed",
    ):
        assert field in SessionEntry.model_fields
        assert field in source, f"SessionEntry.{field} is missing from ui/src/types.ts"


def test_grouping_router_is_mounted() -> None:
    """The Grouping tab's router has to be included or the tab 404s."""
    from orchestrator.observatory.app import create_app

    from tests.test_observatory_api import route_paths

    paths = route_paths(create_app().routes)
    for router in (events, escalations, transcripts, artifacts, grouping):
        for route in router.router.routes:
            assert route.path in paths, f"{route.path} is not mounted on the app"


def test_no_liveness_logic_consults_live_pids() -> None:
    """``live_pids`` is display-only (R9). A crashed run must still render, so
    nothing may treat a recorded pid as evidence that anything is alive."""
    banned = ("os.kill", "psutil", "pid_alive", "is_alive", "process_exists")
    for source in _observatory_sources():
        text = source.read_text()
        if "live_pids" not in text:
            continue
        for token in banned:
            assert token not in text, f"{source.name} mixes live_pids with liveness probing"


def test_ui_never_branches_on_live_pids() -> None:
    """Same rule on the client: the pids may be shown, never consulted."""
    ui_src = UI_TYPES.parent
    for source in sorted(ui_src.rglob("*.ts*")):
        text = source.read_text()
        for line in text.splitlines():
            if "live_pids" not in line:
                continue
            stripped = line.strip()
            assert not stripped.startswith(("if ", "} else if ")), (
                f"{source.name} branches on live_pids: {stripped!r} — it is display-only"
            )
            assert "stall" not in line.lower(), f"{source.name} feeds live_pids into stall logic"


def test_fixture_manifest_carries_the_drifted_fields() -> None:
    """The modern fixture must actually exercise the new fields, or the API
    assertions below it pass vacuously."""
    fixture = Path(__file__).parent / "fixtures" / "observatory" / "run-modern" / "manifest.json"
    manifest = json.loads(fixture.read_text())
    assert manifest["grouping"]
    assert manifest["escalation"]
    session = manifest["groups"]["g1"]["sessions"][0]
    assert session["total_cache_read_tokens"] > 0
