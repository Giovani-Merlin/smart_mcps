"""Report-level hold for required driver items that name a test nobody wrote.

``unmet_required_verification`` ignores ``driver_run`` items — the coder cannot
run them. But a ``Run (driver): uv run pytest tests/x.py`` whose file does not
exist can only fail after the merge, when the coder is gone. At the gate the
coder can still write the test, so a missing named test is a gap here.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from orchestrator.model import VerificationItem, VerificationResult
from orchestrator.recipes.run import _RUN_ITEM_RE

_SHELL_OPERATORS_RE = re.compile(r"&&|\|\||;|\|")
_TEST_ARG_RE = re.compile(r"^([\w./-]+\.py)((?:::[\w\[\]-]+)*)$")


def _is_pytest(argv: list[str]) -> bool:
    """``pytest``, ``uv run pytest``, ``python -m pytest`` (any runner prefix)."""
    for idx, token in enumerate(argv):
        if Path(token).name == "pytest":
            return True
        if token == "-m" and idx + 1 < len(argv) and argv[idx + 1] == "pytest":
            return True
    return False


def _names_in_file(path: Path, name: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return (
        re.search(rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", text, re.M) is not None
    )


def driver_items_naming_missing_tests(
    items: list[VerificationItem], results: list[VerificationResult], worktree: Path
) -> list[str]:
    reported = {result.item_id: result for result in results}
    gaps: list[str] = []
    for item in items:
        if not (item.required and item.driver_run):
            continue
        result = reported.get(item.id)
        if result is not None and result.status == "skipped" and result.notes.strip():
            continue
        match = _RUN_ITEM_RE.search(item.description)
        if match is None:
            continue
        command = match.group(1) or match.group(2) or ""
        for segment in _SHELL_OPERATORS_RE.split(command):
            try:
                argv = shlex.split(segment)
            except ValueError:
                argv = segment.split()
            if not _is_pytest(argv):
                continue
            for arg in argv:
                found = _TEST_ARG_RE.match(arg)
                if found is None:
                    continue
                rel, names = found.group(1), found.group(2)
                target = worktree / rel
                missing = None
                if not target.is_file():
                    missing = rel
                elif names:
                    first = names.split("::")[1]
                    if not _names_in_file(target, first):
                        missing = f"{rel}{names}"
                if missing is not None:
                    gaps.append(
                        f"driver item {item.id} names {missing} which does not exist in the "
                        "worktree — write it (the driver runs it after the merge) or report "
                        "the item skipped with the reason"
                    )
    return gaps
