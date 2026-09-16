"""`transcript_events.parse_transcript`: Claude Code jsonl -> neutral events."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.execution.transcript_events import (
    NeutralEvent,
    activity_tail,
    format_activity_tail,
    parse_transcript,
    read_events_gz,
    write_events_gz,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _user(uuid: str, content, timestamp: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {"role": "user", "content": content},
    }


def _assistant(uuid: str, content, timestamp: str = "2026-01-01T00:00:01Z") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": content},
    }


# ------------------------------------------------------------------- mapping


def test_text_block_mapping(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [
            _user("u1", [{"type": "text", "text": "hello"}]),
            _assistant("a1", [{"type": "text", "text": "hi there"}]),
        ],
    )
    events = parse_transcript(path).events
    assert [(e.event_id, e.role, e.text) for e in events] == [
        ("u1", "user", "hello"),
        ("a1", "assistant", "hi there"),
    ]


def test_string_content_mapping(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(path, [_user("u1", "plain string content")])
    [event] = parse_transcript(path).events
    assert event.role == "user"
    assert event.text == "plain string content"


def test_tool_use_block_mapping(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                "a1",
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "/a"},
                    }
                ],
            )
        ],
    )
    [event] = parse_transcript(path).events
    assert event.role == "assistant"
    assert event.tool_name == "Read"
    assert event.tool_use_id == "toolu_1"
    assert event.tool_input == {"file_path": "/a"}


def test_thinking_block_mapping(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(path, [_assistant("a1", [{"type": "thinking", "thinking": "pondering"}])])
    [event] = parse_transcript(path).events
    assert event.role == "assistant"
    assert event.text == "pondering"


def test_tool_result_block_mapping_and_pairing(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [
            _assistant("a1", [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]),
            _user(
                "u1",
                [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file contents"}],
            ),
        ],
    )
    events = parse_transcript(path).events
    tool_use = next(e for e in events if e.tool_name == "Read")
    tool_result = next(e for e in events if e.role == "tool")
    assert tool_result.tool_use_id == tool_use.tool_use_id == "toolu_1"
    assert tool_result.tool_output == "file contents"
    assert tool_result.is_error is False


def test_tool_result_content_as_block_list(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [
            _user(
                "u1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {"type": "text", "text": "part one"},
                            {"type": "text", "text": "part two"},
                        ],
                    }
                ],
            )
        ],
    )
    [event] = parse_transcript(path).events
    assert event.tool_output == "part onepart two"


def test_error_result_sets_is_error(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [
            _user(
                "u1",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "boom",
                        "is_error": True,
                    }
                ],
            )
        ],
    )
    [event] = parse_transcript(path).events
    assert event.is_error is True
    assert event.tool_output == "boom"


# --------------------------------------------------------------------- strip


BASE_CONTEXT = "# Base context\n\nSome shared rules here."


def test_strip_applied_on_exact_prefix(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    full_text = f"{BASE_CONTEXT}\n\nDo the actual task."
    _write_jsonl(path, [_user("u1", [{"type": "text", "text": full_text}])])
    parsed = parse_transcript(path, strip_prefix=BASE_CONTEXT)
    assert parsed.strip.applied is True
    assert parsed.strip.char_len == len(BASE_CONTEXT)
    assert parsed.strip.sha256 is not None
    [event] = parsed.events
    assert event.text == "Do the actual task."


def test_strip_refused_on_near_miss(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    near_miss = BASE_CONTEXT[:-1] + "X"  # one byte differs
    full_text = f"{near_miss}\n\nDo the actual task."
    _write_jsonl(path, [_user("u1", [{"type": "text", "text": full_text}])])
    parsed = parse_transcript(path, strip_prefix=BASE_CONTEXT)
    assert parsed.strip.applied is False
    assert parsed.strip.sha256 is None
    [event] = parsed.events
    assert event.text == full_text  # unaltered


def test_strip_only_applies_to_first_user_message(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [
            _user("u1", [{"type": "text", "text": f"{BASE_CONTEXT}\n\nfirst"}]),
            _assistant("a1", [{"type": "text", "text": "reply"}]),
            _user("u2", [{"type": "text", "text": f"{BASE_CONTEXT}\n\nsecond"}]),
        ],
    )
    parsed = parse_transcript(path, strip_prefix=BASE_CONTEXT)
    texts = [e.text for e in parsed.events]
    assert texts == ["first", "reply", f"{BASE_CONTEXT}\n\nsecond"]


def test_no_strip_prefix_leaves_text_untouched(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    full_text = f"{BASE_CONTEXT}\n\nDo the actual task."
    _write_jsonl(path, [_user("u1", [{"type": "text", "text": full_text}])])
    parsed = parse_transcript(path)
    assert parsed.strip.applied is False
    [event] = parsed.events
    assert event.text == full_text


# ------------------------------------------------------------------- tolerance


def test_empty_and_malformed_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        "",
        "   ",
        "{not valid json",
        json.dumps({"type": "custom-title", "customTitle": "x"}),  # unknown type, skipped
        json.dumps(_user("u1", [{"type": "text", "text": "hello"}])),
        json.dumps({"type": "user"}),  # no message key
        json.dumps({"type": "assistant", "uuid": "a1", "message": {"content": "no role field ok"}}),
    ]
    path.write_text("\n".join(lines) + "\n")
    parsed = parse_transcript(path)
    assert [e.event_id for e in parsed.events] == ["u1", "a1"]


def test_non_dict_json_line_skipped(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("[1, 2, 3]\n" + json.dumps(_user("u1", "hi")) + "\n")
    parsed = parse_transcript(path)
    assert [e.event_id for e in parsed.events] == ["u1"]


# ------------------------------------------------------------------ activity


def _tool_call(uuid: str, tool_id: str, name: str, input_: dict, timestamp: str) -> dict:
    return _assistant(
        uuid,
        [{"type": "tool_use", "id": tool_id, "name": name, "input": input_}],
        timestamp=timestamp,
    )


def _tool_result(uuid: str, tool_id: str, content: str, timestamp: str) -> dict:
    return _user(
        uuid,
        [{"type": "tool_result", "tool_use_id": tool_id, "content": content}],
        timestamp=timestamp,
    )


def test_activity_tail(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    records: list[dict] = []
    for i in range(1, 8):
        ts = f"2026-01-01T00:{i:02d}:00Z"
        tool_id = f"toolu_{i}"
        if i == 3:
            records.append(
                _tool_call(f"a{i}", tool_id, "Bash", {"command": "ls -la\necho done"}, ts)
            )
        else:
            records.append(_tool_call(f"a{i}", tool_id, "Read", {"file_path": f"/file{i}"}, ts))
        if i != 7:  # the last tool call has no result
            records.append(_tool_result(f"u{i}", tool_id, f"result {i}", ts))
    _write_jsonl(path, records)

    entries = activity_tail(path, n=5)
    assert len(entries) == 5
    assert [e.tool for e in entries] == ["Bash", "Read", "Read", "Read", "Read"]
    assert entries[-1].returned is False
    assert all(e.returned for e in entries[:-1])
    # ordered oldest -> newest
    assert [e.at for e in entries] == [
        "2026-01-01T00:03:00Z",
        "2026-01-01T00:04:00Z",
        "2026-01-01T00:05:00Z",
        "2026-01-01T00:06:00Z",
        "2026-01-01T00:07:00Z",
    ]
    assert entries[0].input_head == "ls -la"


def test_activity_tail_input_head_truncated(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    long_value = "x" * 200
    _write_jsonl(
        path,
        [_tool_call("a1", "toolu_1", "Read", {"file_path": long_value}, "2026-01-01T00:00:00Z")],
    )
    [entry] = activity_tail(path)
    assert len(entry.input_head) == 120
    assert entry.input_head.endswith("…")


def test_activity_tail_missing_file(tmp_path: Path) -> None:
    assert activity_tail(tmp_path / "nope.jsonl") == []


def test_format_activity_tail_empty() -> None:
    assert format_activity_tail([]) == "  (no tool calls yet)"


def test_format_activity_tail_renders_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    _write_jsonl(
        path,
        [_tool_call("a1", "toolu_1", "Read", {"file_path": "/a"}, "2026-01-01T12:34:56Z")],
    )
    entries = activity_tail(path)
    rendered = format_activity_tail(entries)
    assert rendered == '  12:34:56 Read {"file_path": "/a"} [running]'


def test_real_transcript_tail() -> None:
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        import pytest

        pytest.skip("no ~/.claude/projects directory")

    candidates: list[Path] = []
    for transcript_path in projects_dir.glob("**/*.jsonl"):
        try:
            parsed = parse_transcript(transcript_path)
        except (OSError, UnicodeDecodeError):
            continue
        tool_use_count = sum(
            1 for e in parsed.events if e.role == "assistant" and e.tool_name is not None
        )
        if tool_use_count >= 20:
            candidates.append(transcript_path)

    if not candidates:
        import pytest

        pytest.skip("no real transcript with >= 20 tool_use blocks found")

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    entries = activity_tail(newest, n=5)
    assert len(entries) == 5
    assert all(isinstance(e.tool, str) and e.tool for e in entries)
    assert sum(1 for e in entries if e.returned is False) <= 1


# --------------------------------------------------------------------- gzip


def test_gzip_round_trip(tmp_path: Path) -> None:
    events = [
        NeutralEvent(event_id="u1", role="user", text="hi", timestamp="t1"),
        NeutralEvent(
            event_id="a1",
            role="assistant",
            tool_name="Read",
            tool_use_id="toolu_1",
            tool_input={"a": 1},
        ),
        NeutralEvent(
            event_id="a1#1",
            role="tool",
            tool_use_id="toolu_1",
            tool_output="x" * 5000,
            is_error=True,
        ),
    ]
    path = tmp_path / "events" / "sid.jsonl.gz"
    write_events_gz(path, events)
    assert path.is_file()
    round_tripped = read_events_gz(path)
    assert round_tripped == events
