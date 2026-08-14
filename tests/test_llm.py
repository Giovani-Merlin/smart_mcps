"""Unit coverage for orchestrator/grouping/llm.py's subprocess error handling
(plan U4) — distinct from test_grouping_llm.py's `llm`-marked, real-model
scenarios: these mock subprocess.run so they run in the default suite for free.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import pytest

from orchestrator.grouping.llm import (
    LlmProcessError,
    LlmUsageLimit,
    claude_json_runner,
    with_usage_limit_retry,
)


def _run(monkeypatch, *, returncode: int, stdout: str, stderr: str):
    monkeypatch.setattr(
        "orchestrator.grouping.llm.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr),
    )


def test_usage_limit_style_failure_surfaces_stdout_result_over_empty_stderr(monkeypatch):
    _run(
        monkeypatch,
        returncode=1,
        stdout=json.dumps({"result": "Claude AI usage limit reached|1700000000"}),
        stderr="",
    )
    with pytest.raises(LlmProcessError, match="Claude AI usage limit reached"):
        claude_json_runner("prompt", {})


def test_failure_with_unparseable_stdout_falls_back_to_stderr_unchanged(monkeypatch):
    _run(monkeypatch, returncode=1, stdout="not json at all", stderr="")
    with pytest.raises(LlmProcessError) as excinfo:
        claude_json_runner("prompt", {})
    assert "not json at all" not in str(excinfo.value)
    assert str(excinfo.value).endswith(": ")


def test_common_case_non_empty_stderr_and_no_usable_stdout_json_uses_stderr(monkeypatch):
    _run(monkeypatch, returncode=1, stdout="", stderr="rate limited")
    with pytest.raises(LlmProcessError, match="rate limited"):
        claude_json_runner("prompt", {})


# ------------------------------------------------- usage-limit classification


def test_a_usage_limit_is_classified_on_the_one_shot_path(monkeypatch):
    """The standing gap this closes: `claude_json_runner` raised a bare
    `LlmProcessError` and `is_usage_limit` was never reached on this path at all,
    so `group` and every run-time spec rewrite treated a reset-in-40-minutes
    exactly like a segfault."""
    _run(
        monkeypatch,
        returncode=1,
        stdout=json.dumps({"result": "You've hit your session limit · resets 1pm (Europe/Berlin)"}),
        stderr="",
    )
    with pytest.raises(LlmUsageLimit) as caught:
        claude_json_runner("prompt", {})
    # Still an LlmProcessError, so the scheduler keeps classifying it INTERRUPTED.
    assert isinstance(caught.value, LlmProcessError)
    assert caught.value.detail.startswith("You've hit your session limit")


def test_an_ordinary_process_death_is_not_a_usage_limit(monkeypatch):
    _run(monkeypatch, returncode=1, stdout="", stderr="Segmentation fault")
    with pytest.raises(LlmProcessError) as caught:
        claude_json_runner("prompt", {})
    assert not isinstance(caught.value, LlmUsageLimit)


def test_the_wrapper_pauses_and_replays_the_same_call():
    from tests.test_ratelimit import FakeClock

    from orchestrator.config import UsageLimitConfig
    from orchestrator.execution.ratelimit import UsageLimitGate

    clock = FakeClock(datetime.datetime(2026, 8, 13, 9, tzinfo=datetime.UTC).astimezone())
    gate = UsageLimitGate(UsageLimitConfig(), now=clock.now, sleep=clock.sleep)
    seen: list[str] = []

    def runner(prompt: str, schema: dict) -> str:
        seen.append(prompt)
        if len(seen) == 1:
            raise LlmUsageLimit("limit", "session limit · resets 1pm (Europe/Berlin)")
        return "{}"

    wrapped = with_usage_limit_retry(runner, gate)
    assert wrapped("p", {}) == "{}"
    assert seen == ["p", "p"]  # the identical call, replayed
    assert gate.pauses == 1


def test_the_wrapper_is_a_no_op_when_auto_resume_is_off():
    from orchestrator.config import UsageLimitConfig
    from orchestrator.execution.ratelimit import UsageLimitGate

    def runner(prompt: str, schema: dict) -> str:
        raise LlmUsageLimit("limit", "session limit · resets 1pm")

    gate = UsageLimitGate(UsageLimitConfig(auto_resume=False))
    assert with_usage_limit_retry(runner, gate) is runner
    assert with_usage_limit_retry(runner, None) is runner


def test_the_wrapper_gives_up_after_max_attempts():
    from tests.test_ratelimit import FakeClock

    from orchestrator.config import UsageLimitConfig
    from orchestrator.execution.ratelimit import UsageLimitGate

    clock = FakeClock(datetime.datetime(2026, 8, 13, 9, tzinfo=datetime.UTC).astimezone())
    gate = UsageLimitGate(UsageLimitConfig(max_attempts=2), now=clock.now, sleep=clock.sleep)
    attempts: list[int] = []

    def runner(prompt: str, schema: dict) -> str:
        attempts.append(1)
        raise LlmUsageLimit("limit", "session limit · resets 1pm (Europe/Berlin)")

    with pytest.raises(LlmUsageLimit):
        with_usage_limit_retry(runner, gate)("p", {})
    assert len(attempts) == 2
    assert gate.pauses == 1
