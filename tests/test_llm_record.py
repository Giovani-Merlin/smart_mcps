"""Coverage for the grouping-stage LLM call record.

The grouper's two LLM calls decide the partition a whole run is built on, and
until this landed they left nothing on disk. These tests pin the three properties
that make the record trustworthy: it captures every attempt (not just the last),
it never changes the grouping, and it degrades rather than failing when the CLI
or the filesystem won't cooperate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.grouping.llm import (
    LlmCallMeta,
    LlmCallResult,
    LlmError,
    call_llm_json,
    call_meta,
    claude_json_runner,
)
from orchestrator.grouping.llm_record import JsonlCallRecorder

SCHEMA = {"title": "mapper"}


def _envelope(result: str, **extra) -> str:
    return json.dumps({"result": result, **extra})


def _recorder(tmp_path, **kwargs) -> JsonlCallRecorder:
    return JsonlCallRecorder(tmp_path, grouping_run_id="run123", **kwargs)


def _index(tmp_path) -> dict:
    return json.loads((tmp_path / "llm" / "calls.json").read_text())


# ---------------------------------------------------------------- result type


def test_runner_result_is_a_plain_str_to_existing_callers(monkeypatch):
    """The str subclass is the whole reason no test stub had to change."""
    monkeypatch.setattr(
        "orchestrator.grouping.llm.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=_envelope('{"ok": 1}', session_id="s-1", model="claude-x"),
            stderr="",
        ),
    )
    raw = claude_json_runner("prompt", SCHEMA)
    assert raw == '{"ok": 1}'
    assert isinstance(raw, str)
    assert json.loads(raw) == {"ok": 1}


def test_runner_carries_session_id_and_usage(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.grouping.llm.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=_envelope(
                "{}",
                session_id="s-1",
                model="claude-x",
                usage={
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 40,
                },
            ),
            stderr="",
        ),
    )
    meta = call_meta(claude_json_runner("prompt", SCHEMA))
    assert meta is not None
    assert (meta.session_id, meta.model) == ("s-1", "claude-x")
    assert (meta.input_tokens, meta.output_tokens) == (10, 20)
    assert (meta.cache_read_tokens, meta.cache_creation_tokens) == (30, 40)


def test_runner_passes_session_id_so_a_transcript_exists(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=_envelope("{}"), stderr="")

    monkeypatch.setattr("orchestrator.grouping.llm.subprocess.run", fake_run)
    monkeypatch.setattr("orchestrator.grouping.llm._SESSION_ID_SUPPORTED", True)
    claude_json_runner("prompt", SCHEMA)
    assert "--session-id" in seen[0]


def test_runner_degrades_when_the_cli_rejects_session_id(monkeypatch):
    """Observability must never cost a working `group`."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "--session-id" in argv:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="error: unknown option --session-id"
            )
        return SimpleNamespace(returncode=0, stdout=_envelope('{"ok": 1}'), stderr="")

    monkeypatch.setattr("orchestrator.grouping.llm.subprocess.run", fake_run)
    monkeypatch.setattr("orchestrator.grouping.llm._SESSION_ID_SUPPORTED", True)
    assert claude_json_runner("prompt", SCHEMA) == '{"ok": 1}'
    assert len(calls) == 2
    assert "--session-id" not in calls[1]


def test_model_comes_from_model_usage_when_the_envelope_omits_model(monkeypatch):
    """The live envelope shape, which the mocked stubs above do not have.

    The first real ``group`` run recorded ``"gen_ai.request.model": null`` on
    every call: the installed CLI has no top-level ``model`` key at all — the
    name is a *key of* ``modelUsage``. The stubs handed the field over directly,
    so nothing caught it until a live run.
    """
    monkeypatch.setattr(
        "orchestrator.grouping.llm.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=_envelope(
                "{}",
                session_id="s-1",
                modelUsage={
                    "claude-haiku-4-5-20251001": {"outputTokens": 4},
                    "claude-opus-5": {"outputTokens": 1570},
                },
            ),
            stderr="",
        ),
    )
    meta = call_meta(claude_json_runner("prompt", SCHEMA))
    assert meta is not None
    # Two models in one envelope: attribute the call to the larger contributor.
    assert meta.model == "claude-opus-5"


def test_model_is_none_when_no_envelope_field_carries_it(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.grouping.llm.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_envelope("{}"), stderr=""),
    )
    meta = call_meta(claude_json_runner("prompt", SCHEMA))
    assert meta is not None and meta.model is None


def test_call_meta_is_none_for_a_stub_runner():
    assert call_meta("plain text") is None


# ------------------------------------------------------------------ recording


def test_records_the_successful_call(tmp_path):
    recorder = _recorder(tmp_path)
    meta = LlmCallMeta(session_id="s-1", model="claude-x", input_tokens=5)
    runner = lambda prompt, schema: LlmCallResult('{"v": 1}', meta)  # noqa: E731

    result = call_llm_json(runner, "the prompt", SCHEMA, validate=lambda p: p, recorder=recorder)

    assert result == {"v": 1}
    index = _index(tmp_path)
    assert index["grouping_run_id"] == "run123"
    (call,) = index["calls"]
    assert call["gen_ai.operation.name"] == "mapper"
    assert call["status"] == {"code": "ok"}
    assert call["claude.session_id"] == "s-1"
    assert call["gen_ai.usage.input_tokens"] == 5
    assert (tmp_path / "llm" / call["request_file"]).read_text() == "the prompt"


def test_records_a_repaired_attempt_that_the_old_code_discarded(tmp_path):
    """A call that fails validation then succeeds on retry previously left no
    trace at all — `_save_failure` only fired when every attempt failed."""
    recorder = _recorder(tmp_path)
    outputs = iter(["not json", '{"v": 1}'])
    runner = lambda prompt, schema: next(outputs)  # noqa: E731

    call_llm_json(runner, "prompt", SCHEMA, validate=lambda p: p, recorder=recorder)

    calls = _index(tmp_path)["calls"]
    assert [c["status"]["code"] for c in calls] == ["error", "ok"]
    assert calls[0]["error"]
    assert (tmp_path / "llm" / calls[0]["raw_file"]).read_text() == "not json"


def test_records_every_attempt_of_a_call_that_never_validates(tmp_path):
    recorder = _recorder(tmp_path)
    runner = lambda prompt, schema: "not json"  # noqa: E731

    with pytest.raises(LlmError):
        call_llm_json(
            runner, "prompt", SCHEMA, validate=lambda p: p, max_retries=2, recorder=recorder
        )

    calls = _index(tmp_path)["calls"]
    assert len(calls) == 3
    assert all(c["status"]["code"] == "error" for c in calls)


def test_retry_prompt_is_recorded_not_just_the_original(tmp_path):
    """The corrective nudge is the interesting half of a repair — an operator
    reading the record needs to see what the model was actually re-asked."""
    recorder = _recorder(tmp_path)
    outputs = iter(["not json", "{}"])
    runner = lambda prompt, schema: next(outputs)  # noqa: E731

    call_llm_json(runner, "base prompt", SCHEMA, validate=lambda p: p, recorder=recorder)

    calls = _index(tmp_path)["calls"]
    retry_prompt = (tmp_path / "llm" / calls[1]["request_file"]).read_text()
    assert "failed validation" in retry_prompt


def test_link_outputs_joins_the_calls_to_what_they_produced(tmp_path):
    recorder = _recorder(tmp_path)
    runner = lambda prompt, schema: "{}"  # noqa: E731
    call_llm_json(runner, "prompt", SCHEMA, validate=lambda p: p, recorder=recorder)

    recorder.link_outputs(task_ids=["t2", "t1"], group_ids=["g1"])

    assert _index(tmp_path)["produced"] == {"task_ids": ["t1", "t2"], "group_ids": ["g1"]}


def test_recording_is_inert_when_the_filesystem_refuses(tmp_path, monkeypatch):
    """Losing the audit trail is bad; losing the run is worse."""
    recorder = _recorder(tmp_path)
    monkeypatch.setattr(
        "orchestrator.grouping.llm_record.Path.write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    runner = lambda prompt, schema: '{"v": 1}'  # noqa: E731

    assert call_llm_json(runner, "prompt", SCHEMA, validate=lambda p: p, recorder=recorder) == {
        "v": 1
    }


def test_no_recorder_writes_nothing(tmp_path):
    runner = lambda prompt, schema: '{"v": 1}'  # noqa: E731
    call_llm_json(runner, "prompt", SCHEMA, validate=lambda p: p)
    assert not (tmp_path / "llm").exists()
