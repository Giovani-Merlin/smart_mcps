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
  ``delay_s``, plus side effects performed in the caller's cwd before replying:
  ``files`` ({relative path: content} writes) and ``commit`` (git add -A +
  commit) — a scripted coder that actually produces commits for merge scenarios.
  An empty/missing queue yields a default OK response.
- ``scripts/<name>.jsonl`` — per-session queues keyed by ``--name``. A session
  started or forked with a name that has a script file is bound to it (recorded
  in its session file); its resumes pop from that queue instead of the global
  one. Lets E2E tests script concurrent sessions deterministically, mirroring
  the in-process StubRunner's fork_scripts.
- ``sessions/<id>.json`` — known sessions ({"parent": ..., "script": ...});
  ``--resume`` of an unknown id fails like the real CLI.
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
import subprocess
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
    "--disallowedTools",
    "--add-dir",
    "--append-system-prompt",
    "--settings",
    "--input-format",
    "--max-thinking-tokens",
    "--thinking",
}

HELP_TEXT = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                           Print response and exit
  --verbose                             Override verbose mode setting
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


def _pop_script(home: Path, script_path: Path | None = None) -> dict:
    path = script_path or home / "script.jsonl"
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


def _emit_assistant_turn(usage: dict, session_id: str, text: str = "") -> None:
    print(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}] if text else [],
                    "usage": {**DEFAULT_USAGE, **usage},
                },
                "session_id": session_id,
            }
        ),
        flush=True,
    )


def _emit_streamed_turns(scripted: dict, session_id: str) -> None:
    """Emit one ``assistant`` stream event per scripted turn (default: a single
    turn using the top-level ``usage``), each carrying its own usage — the
    per-turn signal ``StreamingProcess.on_turn`` fires on."""
    turns = scripted.get("turns") or [scripted.get("usage", {})]
    for turn_usage in turns:
        _emit_assistant_turn(turn_usage, session_id)


def _emit_streamed_turns_awaiting_send(scripted: dict, session_id: str) -> str:
    """Emit the scripted turns, then block reading one stream-json user message
    from stdin, and echo it back in one more assistant turn — proof the channel
    is bidirectional while the round is still running. Returns the result text
    (embeds the echoed content)."""
    _emit_streamed_turns(scripted, session_id)
    line = sys.stdin.readline()
    try:
        sent = json.loads(line) if line.strip() else {}
        content = (sent.get("message") or {}).get("content") or []
        text = content[0].get("text", "") if content else ""
    except json.JSONDecodeError:
        text = ""
    echo = f"echo: {text}"
    _emit_assistant_turn(scripted.get("usage", {}), session_id, text=echo)
    return echo


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

    # Under `--input-format stream-json` the real CLI takes the conversation from
    # stdin and ignores any prompt on argv. Mirror that here: read the opening
    # user message off stdin so the stub sees the same prompt a real worker would,
    # rather than an argv the real binary never reads.
    if opts.get("--input-format") == "stream-json":
        first = sys.stdin.readline()
        if first.strip():
            try:
                envelope = json.loads(first)
                blocks = (envelope.get("message") or {}).get("content") or []
                prompt = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict)).strip()
            except json.JSONDecodeError:
                pass

    # The real CLI *rejects* this combination, and this stub not rejecting it let
    # a missing `--verbose` ship: every unit test passed while no real worker
    # could spawn at all. Enforce the precondition here so the stub cannot hide
    # an argv the real binary would refuse.
    if (
        opts.get("--output-format") == "stream-json"
        and ("--print" in bools or "-p" in bools)
        and "--verbose" not in bools
    ):
        print(
            "Error: When using --print, --output-format=stream-json requires --verbose",
            file=sys.stderr,
        )
        return 1

    started = time.time()
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
                    # The env slice the U6 scrub tests assert on: workers must
                    # never inherit the orchestrator's VIRTUAL_ENV / its PATH.
                    # The cache variables are recorded for the same reason —
                    # asserting on what the *process* received is the only way to
                    # tell an env overlay that was built from one that was passed.
                    "env": {
                        key: os.environ[key]
                        for key in (
                            "VIRTUAL_ENV",
                            "PATH",
                            "UV_CACHE_DIR",
                            "npm_config_cache",
                            "XDG_CACHE_HOME",
                            "MAVEN_OPTS",
                            "HOME",
                        )
                        if key in os.environ
                    },
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

        # Per-name script binding: a named session whose scripts/<name>.jsonl
        # exists pops from that queue for its whole lifetime (resumes included);
        # everything else falls back to the shared front-popped queue.
        name = opts.get("--name") or opts.get("-n")
        if resume_id and not forking:
            binding = json.loads(_session_file(home, resume_id).read_text()).get("script")
        else:
            binding = name if name and (home / "scripts" / f"{name}.jsonl").exists() else None
            _session_file(home, session_id).write_text(
                json.dumps({"parent": parent, "script": binding})
            )
        script_path = (home / "scripts" / f"{binding}.jsonl") if binding else None
        scripted = _pop_script(home, script_path)

        delay = float(
            scripted.get("delay_s", os.environ.get("FAKE_CLAUDE_DEFAULT_DELAY_S", 0)) or 0
        )
        if delay:
            time.sleep(delay)

        streaming = opts.get("--output-format") == "stream-json"

        exit_code = int(scripted.get("exit_code", 0))
        if exit_code:
            # A usage-limit failure exits non-zero with an *empty* stderr but a
            # populated JSON envelope on stdout (plan U4) — "stdout" lets a
            # scripted entry reproduce that shape exactly, distinct from the
            # stderr-only failure every other scripted failure uses.
            if "stdout" in scripted:
                stdout_value = scripted["stdout"]
                if streaming:
                    # Only a well-formed {"result": ...} payload survives the
                    # stream reader's line-by-line "type":"result" filter —
                    # deliberately: an unparseable payload must fall through to
                    # stderr unchanged, exactly like the non-streaming path did.
                    try:
                        parsed = json.loads(stdout_value)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, dict) and "result" in parsed:
                        print(
                            json.dumps(
                                {
                                    "type": "result",
                                    "subtype": "error",
                                    "is_error": True,
                                    "result": parsed["result"],
                                    "session_id": session_id,
                                }
                            )
                        )
                else:
                    print(stdout_value)
            print(scripted.get("stderr", "scripted failure"), file=sys.stderr)
            return finish(exit_code)

        # Side effects a scripted "coder" performs in its cwd (the group worktree):
        # file writes and a real git commit, so merge scenarios exercise real git.
        for rel_path, content in (scripted.get("files") or {}).items():
            target = Path.cwd() / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        if scripted.get("commit"):
            subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
            done = subprocess.run(
                ["git", "commit", "-m", str(scripted["commit"])], capture_output=True, text=True
            )
            if done.returncode != 0:
                print(f"scripted commit failed: {done.stderr}", file=sys.stderr)
                return finish(1)

        result_text = scripted.get("result", "OK")

        if streaming:
            if scripted.get("await_send"):
                result_text = _emit_streamed_turns_awaiting_send(scripted, session_id)
            else:
                _emit_streamed_turns(scripted, session_id)
            if scripted.get("no_result"):
                _write_transcript(home, os.getcwd(), session_id, result_text)
                return finish(0)

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
