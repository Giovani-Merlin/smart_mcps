"""U1 tests: the bidirectional stream-json worker channel (plan Phase B).

Every test runs against tests/fake_claude.py's stream-json scripting — zero
live CLI calls, zero tokens (plan R24).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from orchestrator.execution.sessions import SessionError, SessionRunner
from orchestrator.execution.streaming import StreamingProcess, TurnUsage

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"

STREAM_ARGV = [
    sys.executable,
    str(FAKE_CLAUDE),
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--input-format",
    "stream-json",
]


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def make_runner(fake_home: Path, **kwargs) -> SessionRunner:
    env = {"FAKE_CLAUDE_HOME": str(fake_home), **kwargs.pop("env", {})}
    kwargs.setdefault("transcript_root", fake_home / "projects")
    return SessionRunner(claude_bin=[sys.executable, str(FAKE_CLAUDE)], env=env, **kwargs)


def script(fake_home: Path, entry: dict) -> None:
    with (fake_home / "script.jsonl").open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


class _Tracker:
    def __init__(self) -> None:
        self.spawned_calls: list[tuple[int, str]] = []
        self.exited_calls: list[int] = []

    def spawned(self, pid: int, context: str) -> None:
        self.spawned_calls.append((pid, context))

    def exited(self, pid: int) -> None:
        self.exited_calls.append(pid)


def test_final_turn_usage_matches_the_final_envelope(fake_home, tmp_path):
    """RoundResult.usage must equal the round's final turn, exactly like
    non-streamed rounds — from_envelope's iterations[-1] rule is unchanged."""
    script(
        fake_home,
        {
            "turns": [
                {"input_tokens": 1, "output_tokens": 1},
                {"input_tokens": 999, "output_tokens": 999},
            ],
            "usage": {
                "input_tokens": 42,
                "output_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 5,
            },
            "result": "final report",
        },
    )
    runner = make_runner(fake_home)
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.text == "final report"
    assert result.usage.input_tokens == 42
    assert result.usage.output_tokens == 7
    assert result.usage.cache_read_input_tokens == 3
    assert result.usage.cache_creation_input_tokens == 5


def test_per_turn_observer_gets_one_callback_per_assistant_turn(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(
        fake_home,
        {
            "turns": [
                {"input_tokens": 10, "output_tokens": 1},
                {"input_tokens": 20, "output_tokens": 2},
                {"input_tokens": 30, "output_tokens": 3},
            ],
            "result": "done",
        },
    )
    seen: list[TurnUsage] = []

    def on_turn(usage: TurnUsage, send) -> None:
        seen.append(usage)

    result = runner.start_fork(
        base_id=base.session_id,
        prompt="go",
        name="worker-1",
        cwd=tmp_path,
        on_turn=on_turn,
    )
    assert result.text == "done"
    assert len(seen) == 3
    assert [u.input_tokens for u in seen] == [10, 20, 30]
    assert [u.output_tokens for u in seen] == [1, 2, 3]
    for usage in seen:
        assert usage.cache_read_input_tokens == 100  # DEFAULT_USAGE fallback
        assert usage.cache_creation_input_tokens == 200


def test_send_reaches_the_child_mid_round_and_is_echoed_back(fake_home, tmp_path):
    """send() writes onto the still-running child's stdin; the scripted CLI
    echoes it in a further turn before the round completes — proof the channel
    is bidirectional while the round is in flight, not only between rounds."""
    script(
        fake_home,
        {
            "await_send": True,
            "turns": [{"input_tokens": 5, "output_tokens": 1}],
        },
    )
    runner = make_runner(fake_home)
    sent = {"done": False}

    def on_turn(usage: TurnUsage, send) -> None:
        if not sent["done"]:
            sent["done"] = True
            send("hello from the orchestrator")

    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path, on_turn=on_turn)
    assert result.text == "echo: hello from the orchestrator"


def test_nonzero_exit_raises_session_error_with_argv_context(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"exit_code": 3, "stderr": "boom"})
    with pytest.raises(SessionError, match="boom") as excinfo:
        runner.start_fork(
            base_id=base.session_id,
            prompt="go",
            name="worker-1",
            cwd=tmp_path,
        )
    assert "--session-id" in str(excinfo.value)  # argv context, not just stderr


def test_stream_without_terminal_result_raises_session_error_not_hang(fake_home, tmp_path):
    script(fake_home, {"no_result": True, "turns": [{"input_tokens": 1, "output_tokens": 1}]})
    runner = make_runner(fake_home)
    with pytest.raises(SessionError, match="terminal result"):
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)


def test_tracker_sees_spawned_and_exited_exactly_once_per_round(fake_home, tmp_path):
    script(fake_home, {"result": "ok"})
    tracker = _Tracker()
    runner = make_runner(fake_home, tracker=tracker)
    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert len(tracker.spawned_calls) == 1
    assert len(tracker.exited_calls) == 1
    spawned_pid, _ = tracker.spawned_calls[0]
    assert tracker.exited_calls[0] == spawned_pid


# ----------------------------------------------------- passive denial evidence


def test_tool_result_events_yield_deny_signals(fake_home, tmp_path):
    """Plan P2: the orchestrator's own account of a denial, independent of the
    model's.

    `_read_stdout` branched only on `assistant` and `result`, so every `user`
    event — where `tool_result` blocks arrive, i.e. what every tool call actually
    returned — was dropped. That left the report as the sole source for
    attributing a `permission_denied`, and the report is the model's own account of
    what happened to it.
    """
    script(
        fake_home,
        {
            "result": "OK",
            "tool_results": [
                "Failed to initialize cache at /home/op/.cache/uv: Permission denied (os error 13)",
                "ok, nothing interesting here",
            ],
        },
    )
    runner = make_runner(fake_home)
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    assert len(result.deny_signals) == 1  # the benign result is filtered out
    assert "os error 13" in result.deny_signals[0]


def test_a_round_with_nothing_denied_carries_no_signals(fake_home, tmp_path):
    script(fake_home, {"result": "OK", "tool_results": ["3 files changed", "tests passed"]})
    runner = make_runner(fake_home)
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.deny_signals == []


def test_deny_signals_are_capped_so_a_chatty_round_cannot_bloat_the_outcome(fake_home, tmp_path):
    """This rides on an outcome object and only ever corroborates a
    classification, so it must not grow with the log it is reading."""
    script(
        fake_home,
        {
            "result": "OK",
            "tool_results": ["EACCES: permission denied, open '/x'" + "y" * 5000] * 40,
        },
    )
    runner = make_runner(fake_home)
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert len(result.deny_signals) <= 10
    assert all(len(signal) <= 500 for signal in result.deny_signals)


def test_a_malformed_user_event_cannot_fail_the_round(fake_home, tmp_path, monkeypatch):
    """Advisory evidence never costs a round. A round that failed because its own
    diagnostics raised would be strictly worse than the opaque denial this exists
    to explain."""
    import orchestrator.execution.streaming as streaming_mod

    monkeypatch.setattr(
        streaming_mod,
        "_tool_result_text",
        lambda _content: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    script(fake_home, {"result": "OK", "tool_results": ["permission denied"]})
    runner = make_runner(fake_home)
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.text == "OK"
    assert result.deny_signals == []


# -------------------------------------------------------- activity evidence


def test_on_event_sees_every_stream_line_in_order_including_result(fake_home, tmp_path):
    """Plan U1: the raw activity signal a liveness probe stands on. `assistant`,
    `user` (tool_result) and `result` cover the event shapes `fake_claude.py`
    can script, and `on_event` must see all three, in order, ending on the
    terminal `result` line."""
    script(fake_home, {"result": "OK", "tool_results": ["3 files changed"]})
    argv = [*STREAM_ARGV, "--session-id", "11111111-1111-1111-1111-111111111111"]
    env = {**os.environ, "FAKE_CLAUDE_HOME": str(fake_home)}
    stream = StreamingProcess(argv, cwd=tmp_path, env=env)
    seen: list[str] = []
    stream.on_event = seen.append
    stream.start(prompt="go")
    outcome = stream.wait()

    assert outcome.envelope is not None
    assert len(set(seen)) >= 3, seen
    assert seen[-1] == "result"
    assert seen == sorted(seen, key=seen.index)  # arrival order preserved


def test_a_raising_on_event_hook_does_not_prevent_wait_from_returning(fake_home, tmp_path):
    script(fake_home, {"result": "OK"})
    argv = [*STREAM_ARGV, "--session-id", "22222222-2222-2222-2222-222222222222"]
    env = {**os.environ, "FAKE_CLAUDE_HOME": str(fake_home)}
    stream = StreamingProcess(argv, cwd=tmp_path, env=env)

    def boom(_event_type: str) -> None:
        raise RuntimeError("hook exploded")

    stream.on_event = boom
    stream.start(prompt="go")
    outcome = stream.wait()

    assert outcome.envelope is not None
    assert outcome.envelope.get("result") == "OK"


def test_last_assistant_text_is_captured_from_the_final_assistant_event(fake_home, tmp_path):
    script(
        fake_home,
        {
            "result": "OK",
            "turns": [
                {"input_tokens": 1, "output_tokens": 1, "text": "first turn"},
                {"input_tokens": 1, "output_tokens": 1, "text": "last turn"},
            ],
        },
    )
    argv = [*STREAM_ARGV, "--session-id", "33333333-3333-3333-3333-333333333333"]
    env = {**os.environ, "FAKE_CLAUDE_HOME": str(fake_home)}
    stream = StreamingProcess(argv, cwd=tmp_path, env=env)
    stream.start(prompt="go")
    outcome = stream.wait()

    assert outcome.last_assistant_text == "last turn"


def test_last_assistant_text_is_capped_at_2000_chars(fake_home, tmp_path):
    script(fake_home, {"result": "OK", "turns": [{"text": "x" * 3000}]})
    argv = [*STREAM_ARGV, "--session-id", "44444444-4444-4444-4444-444444444444"]
    env = {**os.environ, "FAKE_CLAUDE_HOME": str(fake_home)}
    stream = StreamingProcess(argv, cwd=tmp_path, env=env)
    stream.start(prompt="go")
    outcome = stream.wait()

    assert len(outcome.last_assistant_text) == 2000


def test_tool_result_text_accepts_both_shapes_the_field_takes():
    from orchestrator.execution.streaming import _tool_result_text

    assert _tool_result_text("plain string") == "plain string"
    assert (
        _tool_result_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    )
    assert _tool_result_text(None) == ""
    assert _tool_result_text([{"type": "image"}]) == ""
