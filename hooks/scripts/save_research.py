#!/usr/bin/env python3
"""
PostToolUse hook: saves NotebookLM query results as markdown.

Detects:
  - Bash calls to `nlm notebook query [--json] NOTEBOOK_ID "question"`
    → docs/research/notebooklm/{notebook_name_or_id}/YYYY-MM-DD_HHMMSS_{slug}.md

Note: Perplexity CLI saves its own results directly to
docs/research/perplexity/{subcommand}/ via CLAUDE_PROJECT_DIR — no hook needed.

Always exits 0 — does not affect what Claude sees.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime


def _slug(text: str, max_len: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:max_len].strip("-")


def _save(out_dir: str, question: str, content: str, meta_lines: list[str]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now()
    fname = ts.strftime("%Y-%m-%d_%H%M%S") + "_" + _slug(question) + ".md"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(f"# {question}\n\n")
        f.write(f"**Date:** {ts.strftime('%Y-%m-%d %H:%M:%S')}  \n")
        for line in meta_lines:
            f.write(line + "  \n")
        f.write("\n## Response\n\n")
        f.write(content.strip() + "\n")


def _resolve_nlm_notebook_name(notebook_id: str) -> str:
    """Try to resolve a notebook ID to its title via `nlm notebook list`."""
    try:
        result = subprocess.run(
            ["nlm", "notebook", "list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        notebooks = json.loads(result.stdout)
        for nb in notebooks:
            if nb.get("id") == notebook_id:
                title = nb.get("title", "").strip()
                if title:
                    return re.sub(r"[^a-z0-9_-]+", "-", title.lower()).strip("-")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    return notebook_id


def _handle_nlm(command: str, output: str, cwd: str) -> None:
    """Parse an `nlm notebook query` call and save the result."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return

    try:
        nlm_idx = tokens.index("nlm")
    except ValueError:
        return

    tokens = tokens[nlm_idx:]
    # Expected shape: nlm notebook query [--json] NOTEBOOK_ID "question" [flags]
    if len(tokens) < 5 or tokens[1] != "notebook" or tokens[2] != "query":
        return

    rest = tokens[3:]
    if rest and rest[0] == "--json":
        rest = rest[1:]

    if len(rest) < 2:
        return

    notebook_id = rest[0]
    question = rest[1]

    if not question or not output.strip():
        return

    notebook_name = _resolve_nlm_notebook_name(notebook_id)
    meta = [
        f"**Notebook ID:** {notebook_id}",
        f"**Notebook:** {notebook_name}",
    ]
    out_dir = os.path.join(cwd, "docs", "research", "notebooklm", notebook_name)
    _save(out_dir, question, output, meta)


def _extract_output(raw_response) -> str:
    if not raw_response:
        return ""
    if isinstance(raw_response, dict):
        return raw_response.get("stdout", "") or raw_response.get("output", "") or ""
    if isinstance(raw_response, str):
        try:
            parsed = json.loads(raw_response)
            if isinstance(parsed, dict):
                return parsed.get("stdout", "") or parsed.get("output", "") or ""
        except (json.JSONDecodeError, ValueError):
            pass
        return raw_response
    return str(raw_response)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        sys.exit(0)

    if data.get("tool_name", "") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    output = _extract_output(data.get("tool_response"))
    cwd = data.get("cwd") or os.getcwd()

    if not command or not output:
        sys.exit(0)

    if "nlm" in command and "notebook" in command and "query" in command:
        _handle_nlm(command, output, cwd)


if __name__ == "__main__":
    main()
    sys.exit(0)
