"""``build_facts`` on synthetic run directories — the cases the two real
fixture runs (``tests/fixtures/runs/``) don't exercise: a failed verification
item plus a required change, an unavailable git range, and a group whose
preflight never ran. See ``docs/plans/2026-09-02-001-feat-run-report-plan.md`` U1.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.execution.scheduler import GroupRunState, RunState
from orchestrator.model import (
    Group,
    GroupingResult,
    GroupManifestEntry,
    RunManifest,
    VerificationItem,
)

RUN_ID = "r20260101-000000"

_PLAN_TEXT = """---
title: Synthetic report-facts fixture
type: feat
date: 2026-01-01
origin: docs/brainstorms/does-not-exist.md
---

# Synthetic report-facts fixture

## Objective

A minimal plan so `build_facts` has a real `## Units` section to parse.

## Units

### U1. Widget — the one unit this fixture needs

- **Summary**: A single unit with two verification bullets.
- **Goal**: `widget.py` does the thing.
- **Files**: `widget.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - The widget returns 1.
  - The widget never raises.
"""


def _write_plan(repo_root: Path, plan_path: str) -> None:
    full = repo_root / plan_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(_PLAN_TEXT)


def _write_groups_json(paths: RunPaths, *, group: Group) -> None:
    result = GroupingResult(plan_path="docs/plans/fixture.md", groups=[group])
    atomic_write_text(paths.groups_path, result.model_dump_json(indent=2) + "\n")


def _write_manifest(paths: RunPaths, *, group_id: str, group_name: str, summary: str) -> None:
    manifest = RunManifest(
        run_id=paths.run_id,
        plan_path="docs/plans/fixture.md",
        groups={
            group_id: GroupManifestEntry(group_id=group_id, group_name=group_name, summary=summary)
        },
    )
    atomic_write_text(paths.manifest_path, manifest.model_dump_json(indent=2) + "\n")


def _write_state(paths: RunPaths, *, group_id: str, state: str) -> None:
    run_state = RunState(run_id=paths.run_id, groups={group_id: GroupRunState(state=state)})
    atomic_write_text(paths.state_path, run_state.model_dump_json(indent=2) + "\n")


def _write_artifact(paths: RunPaths, group_id: str, filename: str, payload: dict) -> None:
    path = paths.group_dir(group_id) / filename
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _base_group(group_id: str = "g1") -> Group:
    return Group(
        id=group_id,
        name="Widget",
        summary="A single unit with two verification bullets.",
        spec="build the widget",
        difficulty=0.2,
        intensity="self_verify",
        dependencies=[],
        verification=[
            VerificationItem(id=f"{group_id}-1", description="The widget returns 1."),
            VerificationItem(id=f"{group_id}-2", description="The widget never raises."),
        ],
        tasks=["u1-widget"],
        files=["widget.py"],
        estimated_tokens=1000,
    )


def _build_run(tmp_path: Path, *, with_baseline: bool) -> RunPaths:
    repo_root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    repo_root.mkdir()
    _write_plan(repo_root, "docs/plans/fixture.md")

    paths = RunPaths(repo_root, RUN_ID, run_dir=run_dir)
    group = _base_group()
    _write_groups_json(paths, group=group)
    _write_manifest(paths, group_id="g1", group_name="Widget", summary="A single unit.")
    _write_state(paths, group_id="g1", state="completed")

    if with_baseline:
        atomic_write_text(
            paths.preflight_baseline_path,
            json.dumps({"command": [], "commit_sha": "deadbeef", "exit_code": 0, "captured": True})
            + "\n",
        )

    return paths


def test_fail_item_and_required_change_yield_trouble_and_unit_not_landed(tmp_path: Path) -> None:
    paths = _build_run(tmp_path, with_baseline=False)
    _write_artifact(
        paths,
        "g1",
        "report-g1-r1.json",
        {
            "status": "completed",
            "summary": "done",
            "verification_results": [
                {"item_id": "g1-1", "status": "pass", "notes": "ran widget()"},
                {"item_id": "g1-2", "status": "fail", "notes": "raised ValueError"},
            ],
            "surprises": [],
        },
    )
    _write_artifact(
        paths,
        "g1",
        "verdict-g1-r1.json",
        {
            "status": "changes_required",
            "required_changes": ["fix the raise in widget.py"],
            "surprises": [],
            "notes": "one item failed",
        },
    )

    from orchestrator.report.facts import build_facts

    facts = build_facts(paths.repo_root, RUN_ID, run_dir=paths.run_dir)

    assert facts.trouble is True
    assert len(facts.units) == 1
    unit = facts.units[0]
    assert unit.landed is False
    statuses = {v.item_id: v.status for v in unit.verification}
    assert statuses == {"g1-1": "pass", "g1-2": "fail"}
    assert facts.groups[0].required_changes == ["fix the raise in widget.py"]


def test_no_baseline_and_no_branch_yields_unavailable_range_and_planned_files_fallback(
    tmp_path: Path,
) -> None:
    paths = _build_run(tmp_path, with_baseline=False)
    _write_artifact(
        paths,
        "g1",
        "report-g1-r1.json",
        {
            "status": "completed",
            "summary": "done",
            "verification_results": [
                {"item_id": "g1-1", "status": "pass", "notes": "ran widget()"},
                {"item_id": "g1-2", "status": "pass", "notes": "no raise"},
            ],
            "surprises": [],
        },
    )

    from orchestrator.report.facts import build_facts

    facts = build_facts(paths.repo_root, RUN_ID, run_dir=paths.run_dir)

    assert facts.git_range.available is False
    assert facts.git_range.base_sha is None
    assert [cf.path for cf in facts.changed_files] == ["widget.py"]
    assert facts.changed_files[0].group_id == "g1"
    assert facts.units[0].landed is True


def test_group_with_no_junit_reports_tests_not_ran(tmp_path: Path) -> None:
    paths = _build_run(tmp_path, with_baseline=True)
    _write_artifact(
        paths,
        "g1",
        "report-g1-r1.json",
        {
            "status": "completed",
            "summary": "done",
            "verification_results": [
                {"item_id": "g1-1", "status": "pass", "notes": "ran widget()"},
                {"item_id": "g1-2", "status": "pass", "notes": "no raise"},
            ],
            "surprises": [],
        },
    )

    from orchestrator.report.facts import build_facts

    facts = build_facts(paths.repo_root, RUN_ID, run_dir=paths.run_dir)

    assert facts.groups[0].tests.ran is False
    assert facts.groups[0].tests.total == 0
    assert facts.groups[0].tests.junit_path is None


# ------------------------------------------------- real fixture (report v2 U3)

_REAL_FIXTURE_ID = "r20260828-220035"
_REAL_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "runs" / _REAL_FIXTURE_ID
_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_real_fixture_tokens_exclude_cache_reads_and_elapsed_falls_back_to_heartbeat() -> None:
    from orchestrator.report.facts import build_facts

    facts = build_facts(_REPO_ROOT, _REAL_FIXTURE_ID, run_dir=_REAL_FIXTURE_DIR)
    g1 = next(g for g in facts.groups if g.id == "g1")
    session = next(s for s in g1.sessions if s.role == "coder")
    # The manifest never closed the session: the heartbeat's updated_at is
    # the fallback, so elapsed is a real duration, never 0m.
    assert session.ended_at_source == "heartbeat"
    assert session.ended_at is not None and session.ended_at > session.started_at
    from datetime import datetime

    elapsed = datetime.fromisoformat(session.ended_at) - datetime.fromisoformat(session.started_at)
    assert elapsed.total_seconds() > 60
    # Cache reads (25.7M for this session) are reported apart from tokens.
    assert "cache_read" not in session.tokens
    assert sum(session.tokens.values()) < 1_000_000
    assert session.cache_read_tokens > 1_000_000
