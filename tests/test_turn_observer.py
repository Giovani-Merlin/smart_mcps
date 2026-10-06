"""U5: the per-turn observer — ladder on by default, the 100% stop, the
repeat-denial stop, the stall nudge and the redundant-read reminder."""

from __future__ import annotations

import json
import sys
from itertools import count
from pathlib import Path
from types import SimpleNamespace


from orchestrator.config import BreakerConfig
from orchestrator.model import SessionEntry, SessionRole
from orchestrator.execution.records import SessionRecords
from orchestrator.execution.round_signals import RoundSignals
from orchestrator.execution.sessions import SessionRunner
from orchestrator.execution.streaming import TurnUsage

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
DENIED = "Permission denied: this command is not allowed in the sandbox."


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.ended: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        self.sent.append(text)

    def end_round(self, text: str) -> None:
        self.ended.append(text)
        self.closed = True


class _Records(SessionRecords):
    def __init__(self, breaker: BreakerConfig) -> None:
        self.deps = SimpleNamespace(
            breaker=breaker,
            store=SimpleNamespace(save=lambda _m: None),
            manifest=None,
        )
        self.gid = "g1"
        self.generation = 1
        self.logs: list[str] = []
        self._log = self.logs.append


def _observer(**breaker):
    records = _Records(BreakerConfig(context_token_limit=200_000, **breaker))
    # A real SessionEntry, not a namespace stub: the observer's bookkeeping writes
    # whatever fields the model carries, and a stub lagging one field behind
    # failed a sibling group's merge gate (run r20261006-115802, g7).
    on_turn = records._make_coder_on_turn(
        SessionEntry(session_id="s-observer", role=SessionRole.CODER)
    )
    return records, on_turn, on_turn.signals, _Recorder()


_ids = count()


def _call(signals, tool, tool_input, *, result=None, is_error=False):
    """Feed one assistant turn holding a tool call, then its result."""
    tid = f"t{next(_ids)}"
    block = {"type": "tool_use", "id": tid, "name": tool, "input": tool_input}
    signals.observe({"type": "assistant", "message": {"content": [block]}})
    if result is not None:
        done = {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}
        signals.observe({"type": "user", "message": {"content": [done]}})


def _turn(on_turn, rec, tokens=1_000):
    on_turn(TurnUsage(input_tokens=tokens), rec.send, rec.end_round)


def _denied_bash(signals, command="rm -rf x"):
    _call(signals, "Bash", {"command": command}, result=DENIED, is_error=True)


def test_defaults():
    b = BreakerConfig()
    assert (b.context_ladder_enabled, b.stall_window_k, b.repeat_denial_cap) == (True, 8, 3)


def test_limit_crossing_sends_compact_then_one_stop_and_closes_stdin():
    records, on_turn, _signals, rec = _observer()
    for _ in range(3):
        _turn(on_turn, rec, 1_000)
    assert rec.sent == [] and rec.ended == []
    _turn(on_turn, rec, 210_000)
    _turn(on_turn, rec, 220_000)
    assert len(rec.sent) == 1 and "Context checkpoint" in rec.sent[0]
    assert len(rec.ended) == 1 and rec.closed
    assert sum("context limit stop at turn" in line for line in records.logs) == 1


def test_disabled_ladder_disables_every_prompt():
    _records, on_turn, signals, rec = _observer(context_ladder_enabled=False)
    for _ in range(20):
        _call(signals, "Bash", {"command": "make"}, result="boom", is_error=True)
        _turn(on_turn, rec, 250_000)
    assert rec.sent == [] and rec.ended == []


def test_third_identical_denial_ends_the_round_two_do_not():
    records, on_turn, signals, rec = _observer()
    _denied_bash(signals)
    _turn(on_turn, rec)
    _denied_bash(signals)
    _turn(on_turn, rec)
    assert rec.ended == [] and not rec.closed
    _denied_bash(signals)
    _turn(on_turn, rec)
    _turn(on_turn, rec)
    assert len(rec.ended) == 1 and "rm -rf x" in rec.ended[0] and rec.closed
    assert sum("repeat denial stop at turn" in line for line in records.logs) == 1


def test_stall_nudge_at_eight_and_again_at_sixteen():
    records, on_turn, signals, rec = _observer()
    for i in range(1, 17):
        _call(signals, "Bash", {"command": "make build"}, result="error: boom", is_error=True)
        _turn(on_turn, rec)
        assert len(rec.sent) == (0 if i < 8 else 1 if i < 16 else 2), i
    assert sum("stall nudge at turn" in line for line in records.logs) == 2


def test_redundant_read_reminder_fires_once_per_path():
    records, on_turn, signals, rec = _observer()
    for _ in range(3):
        _call(signals, "Read", {"file_path": "a.py"}, result="x")
        _turn(on_turn, rec)
    assert len(rec.sent) == 1 and "a.py" in rec.sent[0]
    assert sum("redundant read reminder at turn" in line for line in records.logs) == 1


def test_two_argument_callers_get_end_round_as_send():
    _records, on_turn, _signals, rec = _observer()
    on_turn(TurnUsage(input_tokens=250_000), rec.send)
    assert len(rec.sent) == 2


def test_runner_wires_end_round_signals_and_closes_stdin(tmp_path):
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    entry = {"await_send": True, "turns": [{"input_tokens": 5, "output_tokens": 1}]}
    (home / "script.jsonl").write_text(json.dumps(entry) + "\n")
    runner = SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env={"FAKE_CLAUDE_HOME": str(home)},
        transcript_root=home / "projects",
    )

    def on_turn(usage, send, end_round):
        end_round("final words")

    signals = RoundSignals()
    on_turn.signals = signals
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path, on_turn=on_turn)
    assert result.text == "echo: final words"
    assert result.signals is signals
