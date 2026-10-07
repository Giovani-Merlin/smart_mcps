"""A required driver item naming a pytest file that is not in the worktree is a
gap at the report gate, while a coder can still write the test."""

from __future__ import annotations

import json

import pytest

from orchestrator.execution.driver_items import driver_items_naming_missing_tests as gaps_for
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import ReviewIntensity, VerificationItem, VerificationResult
from tests.test_review_loop import Harness, StubRunner, coder_report, make_group


@pytest.fixture
def worktree(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_one():\n    pass\n")
    return tmp_path


def driver(run: str, *, id: str = "d1", required: bool = True) -> VerificationItem:
    return VerificationItem(
        id=id, description=f"Run (driver): {run} Pass: green", required=required
    )


def test_an_existing_named_test_is_no_gap(worktree):
    item = driver("uv run pytest tests/test_a.py::test_one -m llm")
    assert gaps_for([item], [], worktree) == []


def test_a_missing_file_is_one_gap_naming_it(worktree):
    gaps = gaps_for([driver("uv run pytest tests/test_b.py -m llm")], [], worktree)
    assert len(gaps) == 1 and "tests/test_b.py" in gaps[0] and "d1" in gaps[0]


def test_a_missing_test_name_is_one_gap_naming_it(worktree):
    gaps = gaps_for([driver("uv run pytest tests/test_a.py::test_two")], [], worktree)
    assert len(gaps) == 1 and "::test_two" in gaps[0]


@pytest.mark.parametrize(
    "run",
    [
        "python -m pytest tests/test_b.py",
        "pytest tests/test_b.py",
        "uv run pytest tests/test_a.py && pytest tests/test_b.py",
    ],
)
def test_other_pytest_spellings_are_read(worktree, run):
    assert len(gaps_for([driver(run)], [], worktree)) == 1


def test_a_non_pytest_run_is_no_gap(worktree):
    assert gaps_for([driver("uv run python tests/test_b.py")], [], worktree) == []


def test_an_optional_item_is_no_gap(worktree):
    item = driver("uv run pytest tests/test_b.py", required=False)
    assert gaps_for([item], [], worktree) == []


def test_a_required_non_driver_item_is_no_gap(worktree):
    item = VerificationItem(id="v", description="Run: uv run pytest tests/test_b.py Pass: green")
    assert gaps_for([item], [], worktree) == []


def test_a_skipped_result_with_notes_is_no_gap(worktree):
    item = driver("uv run pytest tests/test_b.py")
    skipped = VerificationResult(item_id="d1", status="skipped", notes="no fixture available")
    assert gaps_for([item], [skipped], worktree) == []
    bare = VerificationResult(item_id="d1", status="skipped", notes="")
    assert len(gaps_for([item], [bare], worktree)) == 1


@pytest.mark.asyncio
async def test_the_gap_reaches_the_changes_required_verdict(tmp_path):
    items = [
        VerificationItem(id="v1", description="tests pass"),
        driver("uv run pytest tests/test_nope.py -m llm"),
    ]
    held = coder_report(verification_results=[{"item_id": "v1", "status": "pass", "notes": ""}])
    resolved = coder_report(
        verification_results=[
            {"item_id": "v1", "status": "pass", "notes": ""},
            {"item_id": "d1", "status": "skipped", "notes": "no fixture available"},
        ]
    )
    runner = StubRunner({"r1-g1-coder-g1": [held, resolved]})
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group(intensity=ReviewIntensity.SELF_VERIFY, verification=items))
    assert state == GroupState.COMPLETED
    verdicts = sorted(
        (tmp_path / ".orchestrator" / "runs" / "r1" / "groups" / "g1").glob("verdict-*")
    )
    assert verdicts
    body = json.loads(verdicts[0].read_text())
    assert body["status"] == "changes_required"
    assert any("tests/test_nope.py" in change for change in body["required_changes"])
