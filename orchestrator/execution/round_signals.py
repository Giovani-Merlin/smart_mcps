"""One per-round reading of a worker's tool calls and their results.

``RoundSignals`` is fed the raw ``assistant`` / ``user`` stream events of a
round (``StreamingProcess.on_tool_event``) and pairs every ``tool_use`` block
with its ``tool_result`` by ``tool_use_id``. Everything a mid-round observer
wants to know — identical denials, a stalled loop, a redundant re-read, an
edit nobody verified — is a query over that one record, so no consumer parses
the stream itself.

This module only *reads*. Acting on a signal (nudging, ending the round) is the
turn observer's job.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field

from orchestrator.execution.streaming import _DENY_SIGNAL_RE, _tool_result_text

READ = "read"
EDIT = "edit"
VERIFY = "verify"
SEARCH = "search"
WAIT = "wait"
OTHER = "other"

DEFAULT_VERIFY_PATTERN = (
    r"\b(?:pytest|tox|nox|vitest|jest|mocha|ruff|mypy|tsc|pyright|flake8|eslint|"
    r"pre-commit|cargo test|go test|npm test|unittest)\b"
)
_WAIT_RE = re.compile(r"\bkill -0\b|\bsleep |\btail -f\b|\bjobs\b|\bwait |\bnohup\b|\bps ")
_SEARCH_PROGRAMS = frozenset({"ls", "find", "grep", "rg"})
_READ_TOOLS = frozenset({"Read", "Glob"})
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")


def normalise_command(command: str) -> str:
    return _WS_RE.sub(" ", command).strip()


def _program(command: str) -> str:
    for token in command.split():
        if _ENV_ASSIGN_RE.match(token):
            continue
        return token.rsplit("/", 1)[-1]
    return ""


def error_signature(tool: str, text: str) -> str:
    """``<tool>:<first error line, lowercased, digits stripped>``."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return f"{tool}:{_DIGITS_RE.sub('', first.strip().lower())}"


@dataclass
class ToolCall:
    tool_use_id: str
    tool: str
    command: str  # normalised: the Bash command, or "<tool> <target>"
    path: str | None
    action: str
    turn: int
    new_touch: bool = False
    resolved: bool = False
    is_error: bool = False
    text: str = ""
    signature: str = ""  # error signature; "" for a call that did not error
    denied: bool = False

    @property
    def stall_key(self) -> tuple[str, str]:
        sig = self.signature
        if self.action == VERIFY:
            # A verify result that changed is progress even if it still fails.
            digest = hashlib.sha1(_DIGITS_RE.sub("", self.text).encode()).hexdigest()[:12]
            sig = f"{sig}|{digest}"
        return (self.command, sig)


@dataclass(frozen=True)
class StallWindow:
    key: tuple[str, str]
    count: int
    start_call: int  # index of the window's first call among non-search calls
    end_call: int  # index of its last; a consumer dedups nudges on this


@dataclass
class RoundSignals:
    stall_k: int = 8
    verify_pattern: str = DEFAULT_VERIFY_PATTERN
    calls: list[ToolCall] = field(default_factory=list)
    turn_index: int = 0

    def __post_init__(self) -> None:
        self._verify_re = re.compile(self.verify_pattern)
        self._by_id: dict[str, ToolCall] = {}
        self._touched: set[str] = set()
        self._last_read_turn: dict[str, int] = {}
        self._redundant: dict[str, int] = {}
        self._last_edit_idx = -1
        self._last_verify_idx = -1
        self._lock = threading.Lock()

    # ------------------------------------------------------------- feeding

    def observe(self, event: dict) -> None:
        """Fold one raw ``assistant`` or ``user`` stream event into the round."""
        with self._lock:
            event_type = event.get("type")
            content = (event.get("message") or {}).get("content")
            if not isinstance(content, list):
                if event_type == "assistant":
                    self.turn_index += 1
                return
            if event_type == "assistant":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        self._add_call(block)
                self.turn_index += 1
            elif event_type == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        self._resolve(block)

    def _classify(self, tool: str, command: str) -> str:
        if tool in _READ_TOOLS:
            return READ
        if tool in _EDIT_TOOLS:
            return EDIT
        if tool == "Grep":
            return SEARCH
        if tool == "Bash":
            if _program(command) in _SEARCH_PROGRAMS:
                return SEARCH
            if self._verify_re.search(command):
                return VERIFY
            if _WAIT_RE.search(command):
                return WAIT
        return OTHER

    def _add_call(self, block: dict) -> None:
        tool = str(block.get("name") or "")
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        path = next(
            (
                tool_input[key]
                for key in ("file_path", "notebook_path", "path")
                if isinstance(tool_input.get(key), str)
            ),
            None,
        )
        if tool == "Bash":
            command = normalise_command(str(tool_input.get("command") or ""))
        else:
            target = path or tool_input.get("pattern") or ""
            command = normalise_command(f"{tool} {target}")
        action = self._classify(tool, command)
        call = ToolCall(
            tool_use_id=str(block.get("id") or f"anon-{len(self.calls)}"),
            tool=tool,
            command=command,
            path=path,
            action=action,
            turn=self.turn_index,
        )
        idx = len(self.calls)
        if path is not None and action in (READ, EDIT):
            call.new_touch = path not in self._touched
            self._touched.add(path)
        if action == READ and tool == "Read" and path is not None:
            earlier = self._last_read_turn.get(path)
            if earlier is not None and path not in self._redundant:
                self._redundant[path] = earlier
            self._last_read_turn[path] = self.turn_index
        elif action == EDIT:
            if path is not None:
                self._last_read_turn.pop(path, None)
            self._last_edit_idx = idx
        elif action == VERIFY:
            self._last_verify_idx = idx
        self.calls.append(call)
        self._by_id[call.tool_use_id] = call

    def _resolve(self, block: dict) -> None:
        call = self._by_id.get(str(block.get("tool_use_id") or ""))
        if call is None or call.resolved:
            return
        call.resolved = True
        call.is_error = bool(block.get("is_error"))
        call.text = _tool_result_text(block.get("content"))
        if call.is_error:
            call.signature = error_signature(call.tool, call.text)
            call.denied = bool(_DENY_SIGNAL_RE.search(call.text))

    # ------------------------------------------------------------- queries

    def identical_denials(self, command: str) -> int:
        """How many times this (normalised) command came back denied."""
        wanted = normalise_command(command)
        with self._lock:
            return sum(1 for c in self.calls if c.denied and c.command == wanted)

    def stall_window(self) -> StallWindow | None:
        """The trailing run of identical, non-progressing calls, if long enough."""
        with self._lock:
            seq = [c for c in self.calls if c.action != SEARCH]
            if not seq or not seq[-1].resolved:
                return None
            key = seq[-1].stall_key
            start = len(seq)
            while start > 0 and seq[start - 1].resolved and seq[start - 1].stall_key == key:
                start -= 1
            window = seq[start:]
            # The first call may legitimately touch a file; none after it may.
            if any(c.new_touch for c in window[1:]):
                return None
            need = self.stall_k
            if all(c.action == READ for c in window):
                need *= 2
            if len(window) < need:
                return None
            return StallWindow(key=key, count=len(window), start_call=start, end_call=len(seq) - 1)

    def redundant_read(self, path: str) -> int | None:
        """Turn of the earlier ``Read`` of *path* that a later one repeated with
        no edit between, else ``None``. Recorded once per path."""
        with self._lock:
            return self._redundant.get(path)

    def redundant_reads(self) -> dict[str, int]:
        with self._lock:
            return dict(self._redundant)

    def last_edit_unverified(self) -> bool:
        with self._lock:
            return self._last_edit_idx >= 0 and self._last_verify_idx < self._last_edit_idx
