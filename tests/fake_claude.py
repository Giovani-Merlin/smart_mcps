#!/usr/bin/env python3
"""Scripted stub ``claude`` executable — the token-free test double (plan R24).

Speaks the CLI surface ``orchestrator.execution.sessions`` uses, with the envelope
shapes the U5 spike verified against CLI 2.1.211: ``-p <prompt>``,
``--output-format json``, ``--resume``, ``--fork-session``, ``--session-id``,
``--name``/``-n``, ``--json-schema``, ``--model``, ``--permission-mode``,
``--allowedTools``, ``--help``, ``--version``.

State lives under ``$FAKE_CLAUDE_HOME``:

- ``calls.jsonl``   — one line per invocation: argv, prompt, cwd, start/end times.
- ``script.jsonl``  — response queue, popped front-first under an flock. Each line
  may set ``result``, ``usage``, ``is_error``, ``exit_code``, ``stderr``,
  ``delay_s``. An empty/missing queue yields a default OK response.
- ``sessions/<id>.json`` — known sessions ({"parent": ...}); ``--resume`` of an
  unknown id fails like the real CLI.
- ``projects/<encoded-cwd>/<sid>.jsonl`` — transcript stub mirroring
  ``~/.claude/projects`` layout, for transcript-discovery and analyzer-join tests.
- ``fork.lock`` / ``fork_overlaps.log`` — overlap detector: concurrent in-flight
  ``--fork-session`` calls append to the log, proving (by absence) that the
  session runner serializes forks.

Env knobs: ``FAKE_CLAUDE_DEFAULT_DELAY_S`` (sleep applied when the scripted entry
sets none), ``FAKE_CLAUDE_HIDE_FLAGS`` (comma-separated flags removed from
``--help``, to simulate an older CLI for preflight tests).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

VALUE_FLAGS = {
    "--output-format",
    "--session-id",
    "--resume",
    "--json-schema",
    "-n",
    "--name",
    "--model",
    "--permission-mode",
    "--allowedTools",
    "--add-dir",
    "--append-system-prompt",
    "--settings",
}

HELP_TEXT = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                           Print response and exit
  --output-format <format>              Output format (only works with --print)
  -r, --resume [value]                  Resume a conversation by session ID
  --fork-session                        When resuming, create a new session ID
  --session-id <uuid>                   Use a specific session ID for the session
  -n, --name <name>                     Set a display name for this session
  --json-schema <schema>                JSON Schema for structured output
  --model <model>                       Model for the current session
  --permission-mode <mode>              Permission mode for the session
  --allowedTools <tools...>             Comma or space-separated list of tools
  -v, --version                         Output the version number
  -h, --help                            Display help for command
"""

DEFAULT_USAGE = {
    "input_tokens": 10,
    "output_tokens": 50,
    "cache_read_input_tokens": 100,
    "cache_creation_input_tokens": 200,
}


def _print_help() -> None:
    hidden = [f for f in os.environ.get("FAKE_CLAUDE_HIDE_FLAGS", "").split(",") if f]
    for line in HELP_TEXT.splitlines():
        if any(flag in line for flag in hidden):
            continue
        print(line)


def _parse(args: list[str]) -> tuple[dict[str, str], set[str], str]:
    opts: dict[str, str] = {}
    bools: set[str] = set()
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in VALUE_FLAGS:
            opts[arg] = args[i + 1]
            i += 2
        elif arg.startswith("-"):
            bools.add(arg)
            i += 1
        else:
            positionals.append(arg)
            i += 1
    return opts, bools, " ".join(positionals)


def _pop_script(home: Path) -> dict:
    path = home / "script.jsonl"
    if not path.exists():
        return {}
    with path.open("r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        lines = [line for line in fh.read().splitlines() if line.strip()]
        if not lines:
            return {}
        fh.seek(0)
        fh.truncate()
        fh.write("\n".join(lines[1:]) + ("\n" if len(lines) > 1 else ""))
        return json.loads(lines[0])


def _log_call(home: Path, record: dict) -> None:
    with (home / "calls.jsonl").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(record) + "\n")


def _session_file(home: Path, session_id: str) -> Path:
    return home / "sessions" / f"{session_id}.json"


def _write_transcript(home: Path, cwd: str, session_id: str, text: str) -> None:
    encoded = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    project_dir = home / "projects" / encoded
    project_dir.mkdir(parents=True, exist_ok=True)
    with (project_dir / f"{session_id}.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "assistant", "text": text}) + "\n")


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        _print_help()
        return 0
    if "--version" in args or "-v" in args:
        print("0.0.0-fake (fake claude)")
        return 0

    home = Path(os.environ["FAKE_CLAUDE_HOME"])
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    opts, bools, prompt = _parse(args)
    started = time.time()
    scripted = _pop_script(home)
    delay = float(scripted.get("delay_s", os.environ.get("FAKE_CLAUDE_DEFAULT_DELAY_S", 0)) or 0)
    forking = "--fork-session" in bools

    fork_lock: Path | None = None
    if forking:
        fork_lock = home / "fork.lock"
        try:
            os.close(os.open(fork_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            fork_lock = None
            with (home / "fork_overlaps.log").open("a") as fh:
                fh.write(f"{os.getpid()} overlapped at {started}\n")
    try:
        if delay:
            time.sleep(delay)

        def finish(code: int) -> int:
            _log_call(
                home,
                {
                    "argv": args,
                    "prompt": prompt,
                    "cwd": os.getcwd(),
                    "ts_start": started,
                    "ts_end": time.time(),
                    "exit_code": code,
                },
            )
            return code

        resume_id = opts.get("--resume")
        if resume_id and not _session_file(home, resume_id).exists():
            print(f"No conversation found with session ID: {resume_id}", file=sys.stderr)
            return finish(1)
        if resume_id and forking:
            session_id = opts.get("--session-id") or str(uuid.uuid4())
            parent: str | None = resume_id
        elif resume_id:
            session_id = resume_id
            parent = None
        else:
            session_id = opts.get("--session-id") or str(uuid.uuid4())
            parent = None
        if not resume_id or forking:
            _session_file(home, session_id).write_text(json.dumps({"parent": parent}))

        exit_code = int(scripted.get("exit_code", 0))
        if exit_code:
            print(scripted.get("stderr", "scripted failure"), file=sys.stderr)
            return finish(exit_code)

        result_text = scripted.get("result", "OK")
        _write_transcript(home, os.getcwd(), session_id, result_text)
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": bool(scripted.get("is_error", False)),
            "result": result_text,
            "session_id": session_id,
            "num_turns": 1,
            "usage": {**DEFAULT_USAGE, **scripted.get("usage", {})},
            "total_cost_usd": 0.0,
            "modelUsage": {},
        }
        print(json.dumps(envelope))
        return finish(0)
    finally:
        if fork_lock is not None:
            fork_lock.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
