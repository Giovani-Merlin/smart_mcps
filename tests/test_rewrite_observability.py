"""U14 tests: a spec rewrite logs to run.log, persists the rewritten spec, and
records the rewrite speccer call in the run's own llm/calls.json.

Before this, ``_rewrite`` did all three silently: no log line, no persisted
spec, and the rewrite's speccer call never reached an audit trail.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.cli import _rewrite_provider
from orchestrator.execution.scheduler import GroupState
from orchestrator.grouping.llm import LlmCallMeta, LlmCallResult
from orchestrator.grouping.llm_record import JsonlCallRecorder
from orchestrator.model import ReviewIntensity, Surprise
from test_review_loop import Harness, StubRunner, coder_report, make_group, verdict


def _rewrite_scenario(tmp_path) -> tuple[Harness, "object"]:
    """A group whose first coder reports blocked, forcing a rewrite before its
    second (successful) generation."""
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report("blocked")],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        }
    )
    return Harness(tmp_path, runner), runner


@pytest.mark.asyncio
async def test_rewrite_logs_a_surprise_line_to_run_log(tmp_path):
    harness, _ = _rewrite_scenario(tmp_path)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED
    lines = harness.store.paths.event_log_path.read_text().splitlines()
    # mirrors `grep -c surprise logs/run.log`
    assert sum(1 for line in lines if "surprise" in line) > 0


@pytest.mark.asyncio
async def test_rewritten_spec_persisted_and_differs_from_groups_json(tmp_path):
    harness, _ = _rewrite_scenario(tmp_path)
    original = make_group()
    state = await harness.run(original)
    assert state == GroupState.COMPLETED
    spec_path = harness.store.paths.group_dir("g1") / "spec-gen2.json"
    assert spec_path.is_file()
    persisted = json.loads(spec_path.read_text())
    assert persisted["spec"] != original.spec


@pytest.mark.asyncio
async def test_group_never_rewritten_writes_no_spec_gen_file(tmp_path):
    runner = StubRunner({"r1-g1-coder-g1": [coder_report()]})
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group(intensity=ReviewIntensity.SELF_VERIFY))
    assert state == GroupState.COMPLETED
    group_dir = harness.store.paths.group_dir("g1")
    assert list(group_dir.glob("spec-gen*.json")) == []


def _stub_speccer_runner(prompt: str, schema: dict) -> str:
    payload = json.dumps(
        {
            "groups": [
                {
                    "group_id": "g1",
                    "name": "group g1",
                    "summary": "rewritten summary",
                    "spec": "spec g1 v2",
                    "verification": [{"id": "v1", "description": "tests pass"}],
                }
            ]
        }
    )
    meta = LlmCallMeta(
        session_id="sess-rewrite-1",
        model="claude-opus-5",
        duration_ms=1200,
        input_tokens=500,
        output_tokens=120,
        cache_read_tokens=10,
        cache_creation_tokens=5,
    )
    return LlmCallResult(payload, meta)


def test_rewrite_speccer_call_recorded_in_run_llm_calls_json(tmp_path):
    recorder = JsonlCallRecorder(tmp_path, grouping_run_id="r1")
    rewrite_spec = _rewrite_provider(
        "plan text", _stub_speccer_runner, tmp_path / "failures", recorder=recorder
    )
    group = make_group("g1")
    surprises = [Surprise(kind="other", description="something broke", affected_groups=["g1"])]

    rewritten = rewrite_spec(group, surprises)

    assert rewritten.spec == "spec g1 v2"
    index = json.loads((tmp_path / "llm" / "calls.json").read_text())
    assert len(index["calls"]) == 1
    call = index["calls"][0]
    assert call["gen_ai.request.model"] == "claude-opus-5"
    assert call["gen_ai.usage.input_tokens"] == 500
    assert call["gen_ai.usage.output_tokens"] == 120
