"""RoundSignals over synthetic stream events (plan U4)."""

from __future__ import annotations

from orchestrator.execution.round_signals import RoundSignals

_DENIED = "bash: /x: Permission denied"


def _call(sig: RoundSignals, n: int, tool: str, tool_input: dict, result=None, error=False):
    tid = f"t{n}"
    sig.observe(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": tid, "name": tool, "input": tool_input}]
            },
        }
    )
    if result is not None or error:
        sig.observe(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tid,
                            "content": result or "",
                            "is_error": error,
                        }
                    ]
                },
            }
        )


def _bash(sig, n, command, result="ok", error=False):
    _call(sig, n, "Bash", {"command": command}, result, error)


def test_eight_identical_failures_stall_but_are_not_denials():
    sig = RoundSignals()
    for n in range(8):
        _bash(sig, n, "make build", "error: boom 12", error=True)
    window = sig.stall_window()
    assert window is not None and window.count == 8
    assert sig.identical_denials("make build") == 0


def test_eight_identical_denials_count():
    sig = RoundSignals()
    for n in range(8):
        _bash(sig, n, "cat /x", _DENIED, error=True)
    assert sig.identical_denials("cat  /x") == 8
    assert sig.stall_window() is not None


def test_seven_failures_are_not_a_window():
    sig = RoundSignals()
    for n in range(7):
        _bash(sig, n, "make build", "error: boom", error=True)
    assert sig.stall_window() is None


def test_search_calls_never_form_a_window():
    sig = RoundSignals()
    for n in range(12):
        _bash(sig, n, f"ls /d{n}", "a b")
    assert sig.stall_window() is None
    for n in range(12, 30):
        _bash(sig, n, "ls /same", "a b")
    assert sig.stall_window() is None


def test_pure_reads_need_twice_the_window():
    sig = RoundSignals()
    for n in range(8):
        _call(sig, n, "Read", {"file_path": "a"}, "text")
    assert sig.stall_window() is None
    for n in range(8, 16):
        _call(sig, n, "Read", {"file_path": "a"}, "text")
    assert sig.stall_window() is not None


def test_changing_error_text_is_progress():
    sig = RoundSignals()
    for n in range(8):
        _bash(sig, n, "make build", f"error: thing{'x' * n}", error=True)
    assert sig.stall_window() is None


def test_redundant_read():
    sig = RoundSignals()
    _call(sig, 0, "Read", {"file_path": "a"}, "t")
    _call(sig, 1, "Read", {"file_path": "a"}, "t")
    assert sig.redundant_read("a") == 0
    assert sig.redundant_read("b") is None

    sig = RoundSignals()
    _call(sig, 0, "Read", {"file_path": "a"}, "t")
    _call(sig, 1, "Edit", {"file_path": "a"}, "ok")
    _call(sig, 2, "Read", {"file_path": "a"}, "t")
    assert sig.redundant_read("a") is None


def test_last_edit_unverified():
    sig = RoundSignals()
    assert sig.last_edit_unverified() is False
    _call(sig, 0, "Edit", {"file_path": "a"}, "ok")
    _bash(sig, 1, "uv run pytest -q")
    assert sig.last_edit_unverified() is False

    sig = RoundSignals()
    _call(sig, 0, "Edit", {"file_path": "a"}, "ok")
    _bash(sig, 1, "ruff check .")
    assert sig.last_edit_unverified() is False

    sig = RoundSignals()
    _call(sig, 0, "Edit", {"file_path": "a"}, "ok")
    _call(sig, 1, "Read", {"file_path": "b"}, "t")
    assert sig.last_edit_unverified() is True


def test_action_classes_and_turn_index():
    sig = RoundSignals()
    _bash(sig, 0, "FOO=1 grep -r x .")
    _bash(sig, 1, "sleep 5")
    _bash(sig, 2, "git status")
    _call(sig, 3, "Write", {"file_path": "w"}, "ok")
    assert [c.action for c in sig.calls] == ["search", "wait", "other", "edit"]
    assert sig.turn_index == 4
