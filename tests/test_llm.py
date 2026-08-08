"""Unit coverage for orchestrator/grouping/llm.py's subprocess error handling
(plan U4) — distinct from test_grouping_llm.py's `llm`-marked, real-model
scenarios: these mock subprocess.run so they run in the default suite for free.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.grouping.llm import LlmProcessError, claude_json_runner


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
