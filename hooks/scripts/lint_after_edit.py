#!/usr/bin/env python3
"""
PostToolUse hook: auto-fix Python (ruff) and Markdown (markdownlint-cli2) after edits.

Handles Edit, Write (tool_input.file_path) and MultiEdit (tool_input.edits[*].file_path).
Always exits 0 — never blocks tool execution.
"""

import json
import subprocess
import sys


def _collect_paths(tool_name: str, tool_input: dict) -> list[str]:
    if tool_name == "MultiEdit":
        return [e["file_path"] for e in (tool_input.get("edits") or []) if e.get("file_path")]
    path = tool_input.get("file_path", "")
    return [path] if path else []


def _lint(path: str) -> None:
    import os

    if not os.path.isfile(path):
        return

    if path.endswith(".py"):
        # check --fix must run before format to avoid conflicts
        subprocess.run(["ruff", "check", "--fix", "--quiet", path], capture_output=True)
        subprocess.run(["ruff", "format", "--quiet", path], capture_output=True)
    elif path.endswith(".md"):
        # --fix still exits non-zero for unfixable violations — return code ignored
        subprocess.run(["markdownlint-cli2", "--fix", path], capture_output=True)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        sys.exit(0)

    for path in _collect_paths(tool_name, tool_input):
        try:
            _lint(path)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
