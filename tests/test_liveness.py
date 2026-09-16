"""U1 tests: the in-process ``ActivityRegistry`` and its wiring into
``SessionRunner`` (plan U1).

Every test runs against ``tests/fake_claude.py`` — zero live CLI calls, zero
tokens (plan R24), matching the convention ``tests/test_streaming.py`` uses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from orchestrator.execution.liveness import ActivityRegistry
from orchestrator.execution.sessions import SessionError, SessionRunner, SuspendCured

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


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


# --------------------------------------------------------------- registry


def test_registry_current_returns_the_newest_live_child_for_a_cwd():
    registry = ActivityRegistry()
    registry.spawned(101, session_id="s1", cwd="/work/g1")
    registry.spawned(102, session_id="s2", cwd="/work/g1")

    current = registry.current("/work/g1")
    assert current is not None
    assert current.pid == 102
    assert current.session_id == "s2"


def test_registry_current_distinguishes_by_cwd():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    registry.spawned(2, session_id="s2", cwd="/work/g2")

    assert registry.current("/work/g1").pid == 1
    assert registry.current("/work/g2").pid == 2
    assert registry.current("/work/g3") is None


def test_registry_current_is_none_after_exit():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    assert registry.current("/work/g1") is not None

    registry.exited(1)
    assert registry.current("/work/g1") is None


def test_registry_note_event_for_an_unknown_pid_is_a_noop():
    registry = ActivityRegistry()
    # Never raises, and there is nothing to read back for a pid never spawned.
    registry.note_event(9999, "assistant")
    registry.note_assistant_text(9999, "hello")
    registry.mark_cured(9999)
    assert registry.was_cured(9999) is False


def test_registry_note_event_stamps_the_live_child():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    registry.note_event(1, "assistant")

    current = registry.current("/work/g1")
    assert current is not None
    assert current.last_event_type == "assistant"
    assert current.last_event_at is not None


def test_registry_was_cured_survives_exit():
    """`_invoke` checks `was_cured` *after* the child has exited — the whole
    point of the flag — so it must still answer for a pid no longer live."""
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    registry.mark_cured(1)
    registry.exited(1)

    assert registry.was_cured(1) is True
    assert registry.current("/work/g1") is None  # exit still ends its "live" status


def test_registry_was_cured_defaults_false():
    registry = ActivityRegistry()
    registry.spawned(1, session_id="s1", cwd="/work/g1")
    assert registry.was_cured(1) is False


# ----------------------------------------------------------------- runner


class _SpyRegistry(ActivityRegistry):
    """Records every `spawned`/`exited` call it sees, on top of the real
    registry behaviour — so a test can assert on the calls a runner made
    without reaching into the registry's private state."""

    def __init__(self) -> None:
        super().__init__()
        self.spawned_calls: list[tuple[int, str, str]] = []
        self.exited_calls: list[int] = []

    def spawned(self, pid: int, *, session_id: str, cwd: str) -> None:
        self.spawned_calls.append((pid, session_id, cwd))
        super().spawned(pid, session_id=session_id, cwd=cwd)

    def exited(self, pid: int) -> None:
        self.exited_calls.append(pid)
        super().exited(pid)


def test_runner_registers_spawn_with_session_id_and_cwd_then_exit(fake_home, tmp_path):
    script(fake_home, {"result": "ok"})
    registry = _SpyRegistry()
    runner = make_runner(fake_home, activity=registry)

    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    assert len(registry.spawned_calls) == 1
    pid, session_id, cwd = registry.spawned_calls[0]
    assert session_id == result.session_id
    assert cwd == str(tmp_path)
    assert registry.exited_calls == [pid]
    # The round has finished, so the child is no longer live for this cwd.
    assert registry.current(str(tmp_path)) is None


def test_runner_note_event_sees_live_activity_during_the_round(fake_home, tmp_path):
    """A registry wired into a runner observes the child while its round is in
    flight, via the same `on_turn` seam the stream already exposes."""
    script(fake_home, {"turns": [{"input_tokens": 1, "output_tokens": 1}], "result": "done"})
    registry = ActivityRegistry()
    runner = make_runner(fake_home, activity=registry)
    seen_types: list[str] = []

    def on_turn(_usage, _send) -> None:
        current = registry.current(str(tmp_path))
        if current is not None and current.last_event_type is not None:
            seen_types.append(current.last_event_type)

    runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path, on_turn=on_turn)
    assert "assistant" in seen_types


def test_runner_raises_suspend_cured_when_the_exit_was_a_cured_kill(fake_home, tmp_path):
    registry = ActivityRegistry()
    runner = make_runner(fake_home, activity=registry)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    # Force the *next* spawned pid to be marked cured before the runner checks
    # it: monkeypatch-free by wrapping `spawned` isn't needed — we mark it via
    # the registry's own `spawned` hook, observed through a one-shot wrapper.
    original_spawned = registry.spawned

    def spawned_and_cure(pid, *, session_id, cwd):
        original_spawned(pid, session_id=session_id, cwd=cwd)
        registry.mark_cured(pid)

    registry.spawned = spawned_and_cure  # type: ignore[method-assign]

    script(fake_home, {"exit_code": 7, "stderr": "killed"})
    with pytest.raises(SuspendCured) as excinfo:
        runner.start_fork(base_id=base.session_id, prompt="go", name="worker-1", cwd=tmp_path)
    assert excinfo.value.session_id  # the fresh --session-id this attempt used


def test_runner_raises_plain_session_error_without_the_cured_mark(fake_home, tmp_path):
    registry = ActivityRegistry()
    runner = make_runner(fake_home, activity=registry)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    script(fake_home, {"exit_code": 3, "stderr": "boom"})
    with pytest.raises(SessionError) as excinfo:
        runner.start_fork(base_id=base.session_id, prompt="go", name="worker-1", cwd=tmp_path)
    assert not isinstance(excinfo.value, SuspendCured)


def test_runner_with_no_registry_wired_behaves_exactly_as_before(fake_home, tmp_path):
    """`activity=None` (the default) must not change today's behaviour at
    all: a nonzero exit is a plain `SessionError`, never `SuspendCured`."""
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)

    script(fake_home, {"exit_code": 3, "stderr": "boom"})
    with pytest.raises(SessionError) as excinfo:
        runner.start_fork(base_id=base.session_id, prompt="go", name="worker-1", cwd=tmp_path)
    assert not isinstance(excinfo.value, SuspendCured)
