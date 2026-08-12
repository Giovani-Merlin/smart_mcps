"""The stream-json channel against the **real** `claude` CLI (plan R26 opt-in).

`tests/test_streaming.py` covers this channel against `fake_claude.py` and passed
in full while no real worker could complete a single round: the stub accepted an
argv the real binary refuses, and it exited on its own where the real binary waits
for stdin. Two P0s hid in that gap (run r20260812-161423, wedged 4h05m).

A stub can only ever assert the contract its author already believed. These tests
assert the contract the CLI actually has, so they are the ones that would have
caught it. Opt-in via `-m llm` — they spend real tokens.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from orchestrator.execution.streaming import StreamingProcess

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

ARGV = [
    "claude",
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--input-format",
    "stream-json",
]

# Generous enough for a cold CLI start, far below the "wedged forever" failure
# these tests exist to catch.
ROUND_TIMEOUT_S = 120.0


def test_a_round_completes_against_the_real_cli(tmp_path: Path) -> None:
    """The prompt reaches the model and the child *exits*.

    Both halves are load-bearing. Passing the prompt on argv as `-p <text>` is
    silently ignored under `--input-format stream-json` (no assistant turn, no
    result, exit 0), and holding stdin open past the `result` event leaves the
    child alive forever.
    """
    stream = StreamingProcess(ARGV, cwd=tmp_path, env=dict(os.environ))
    started = time.time()
    stream.start(prompt="Reply with exactly: PONG")
    outcome = stream.wait()
    elapsed = time.time() - started

    assert elapsed < ROUND_TIMEOUT_S, f"round did not terminate ({elapsed:.0f}s)"
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.envelope is not None, "no terminal result event"
    assert "PONG" in (outcome.envelope.get("result") or "")


def test_a_mid_round_followup_is_answered_and_still_terminates(tmp_path: Path) -> None:
    """`send()` from inside `on_turn` reaches the same live process, and the
    extra `result` it owes does not leave stdin open forever."""
    stream = StreamingProcess(ARGV, cwd=tmp_path, env=dict(os.environ))
    sent: list[int] = []

    def on_turn(_usage) -> None:
        if not sent:
            sent.append(1)
            stream.send("Now reply with exactly: SECOND")

    stream.on_turn = on_turn
    started = time.time()
    stream.start(prompt="Reply with exactly: FIRST")
    outcome = stream.wait()
    elapsed = time.time() - started

    assert elapsed < ROUND_TIMEOUT_S, f"round did not terminate ({elapsed:.0f}s)"
    assert sent, "on_turn never fired, so the follow-up path was not exercised"
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.envelope is not None
    assert "SECOND" in (outcome.envelope.get("result") or "")
