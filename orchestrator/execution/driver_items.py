"""Report-level hold for required driver items that name a test nobody wrote.

``unmet_required_verification`` ignores ``driver_run`` items — the coder cannot
run them. But a ``Run (driver): uv run pytest tests/x.py`` whose file does not
exist can only fail after the merge, when the coder is gone. At the gate the
coder can still write the test, so a missing named test is a gap here.
"""

from __future__ import annotations

import datetime
import json
import re
import shlex
from pathlib import Path

from orchestrator.execution.manifest import (
    RunPaths,
    atomic_write_text,
    effective_group,
    latest_report,
    log_event,
)
from orchestrator.model import Group, VerificationItem, VerificationResult
from orchestrator.recipes.run import _RUN_ITEM_RE

DRIVER_ITEM_STATUSES = ("pass", "fail", "skipped")


class DriverItemError(ValueError):
    """The driver recorded an item the group's spec does not have, or a status
    outside ``pass|fail|skipped``."""


def read_driver_items(paths: RunPaths, group_id: str) -> dict[str, dict]:
    """The driver's recorded outcomes for a group, ``{}`` when absent or unreadable."""
    try:
        payload = json.loads(paths.driver_items_path(group_id).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def record_driver_item(
    paths: RunPaths, group: Group, item_id: str, status: str, notes: str = ""
) -> None:
    """Write (or overwrite) the driver's outcome for one ``Run (driver):`` item.

    The id is validated against the spec in force, so a typo cannot silently
    "settle" nothing. A recorded ``fail`` stays pending in
    ``pending_driver_items``; ``pass`` and ``skipped`` settle the item."""
    known = [item.id for item in effective_group(paths, group).verification]
    if item_id not in known:
        raise DriverItemError(
            f"group {group.id} has no verification item {item_id!r}; known ids: {', '.join(known)}"
        )
    if status not in DRIVER_ITEM_STATUSES:
        raise DriverItemError(f"status {status!r} is not one of {', '.join(DRIVER_ITEM_STATUSES)}")
    records = read_driver_items(paths, group.id)
    records[item_id] = {
        "status": status,
        "notes": notes,
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "by": "driver",
    }
    atomic_write_text(paths.driver_items_path(group.id), json.dumps(records, indent=2) + "\n")
    log_event(paths, f"group {group.id}: driver item {item_id} recorded {status} by the driver")


def pending_driver_items(paths: RunPaths, group: Group) -> list[str]:
    """Required ``driver_run`` items of the group's spec in force that neither
    the coder's latest report passed nor the driver recorded ``pass``/``skipped``."""
    report = latest_report(paths, group.id)
    settled = {
        r.get("item_id")
        for r in (report.get("verification_results") if report else None) or []
        if isinstance(r, dict) and r.get("status") == "pass"
    }
    settled |= {
        item_id
        for item_id, record in read_driver_items(paths, group.id).items()
        if isinstance(record, dict) and record.get("status") in ("pass", "skipped")
    }
    return [
        item.id
        for item in effective_group(paths, group).verification
        if item.driver_run and item.required and item.id not in settled
    ]


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
