#!/usr/bin/env python3
"""
PostToolUse hook: auto-format Python (ruff) and Markdown (mdformat) after AI edits.

Handles Edit, Write (tool_input.file_path) and MultiEdit (tool_input.edits[*].file_path).
Tools resolve as PATH binary → `uvx` fallback → stderr warning.
Always exits 0 — never blocks tool execution.
"""

import json
import os
import shutil
import subprocess
import sys

# mdformat without these plugins corrupts YAML frontmatter and GFM tables
_MDFORMAT_PLUGINS = ("--with", "mdformat-gfm", "--with", "mdformat-frontmatter")

_warned: set[str] = set()


def _resolve(tool: str, uvx_args: tuple[str, ...] = ()) -> list[str] | None:
    found = shutil.which(tool)
    if found:
        return [found]
    if shutil.which("uvx"):
        return ["uvx", *uvx_args, tool]
    if tool not in _warned:
        _warned.add(tool)
        print(f"lint_after_edit: {tool} not on PATH and no uvx; skipping", file=sys.stderr)
    return None


def _collect_paths(tool_name: str, tool_input: dict) -> list[str]:
    if tool_name == "MultiEdit":
        return [e["file_path"] for e in (tool_input.get("edits") or []) if e.get("file_path")]
    path = tool_input.get("file_path", "")
    return [path] if path else []


def _lint(path: str) -> None:
    if not os.path.isfile(path):
        return

    if path.endswith(".py"):
        ruff = _resolve("ruff")
        if ruff is None:
            return
        # check --fix must run before format to avoid conflicts
        subprocess.run([*ruff, "check", "--fix", "--quiet", path], capture_output=True)
        subprocess.run([*ruff, "format", "--quiet", path], capture_output=True)
    elif path.endswith(".md"):
        mdformat = _resolve("mdformat", uvx_args=_MDFORMAT_PLUGINS)
        if mdformat is None:
            return
        subprocess.run([*mdformat, path], capture_output=True)


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
