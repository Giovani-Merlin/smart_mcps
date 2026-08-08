"""Task-map parser: the deterministic front-end that replaces the mapper LLM.

A plan embedding an ``orchestrator-task-map v1`` block (docs/orchestrator-task-map.md)
was written by a session that already knew every file — including not-yet-existing
ones — plus symbols, ordering, and slice membership, so this parser turns the block
straight into the mapper's output shape. Absent block → ``None`` (the LLM-mapper
fallback keeps foreign plans working); malformed block → ``TaskMapError``, never a
silent fallback, which would hide drift between the plan prose and the map.
"""

from __future__ import annotations

import re
from collections import defaultdict

import yaml

from orchestrator.grouping.graphing import CodegraphClient, TaskMapping
from orchestrator.grouping.mapper import MapperOutput

VERSION_MARKER = "# orchestrator-task-map v1"
# A slice contracts to a single Louvain node: past this size the partition would
# degenerate to pure budget-splitting (docs/orchestrator-task-map.md).
SLICE_TASK_CAP = 5
# size_hints classes, priced by EstimatorConfig.size_hint_{small,medium,large}
# (plan U7); medium is today's flat per_file_tool_allowance rate.
SIZE_HINT_CLASSES = frozenset({"small", "medium", "large"})

_BLOCK = re.compile(
    r"```ya?ml[ \t]*\n(?P<body>[ \t]*" + re.escape(VERSION_MARKER) + r"[ \t]*\n.*?)```",
    re.DOTALL,
)
_ANY_VERSION = re.compile(r"```ya?ml[ \t]*\n[ \t]*#[ \t]*orchestrator-task-map v(\d+)")
# A "## Task Map" heading directly preceding the block (only blank lines in
# between) is part of the strip span too — matched against the text *before*
# the block, anchored to end exactly where the block begins.
_PRECEDING_HEADING = re.compile(r"(?:(?<=\n)|\A)[ \t]*## Task Map[ \t]*\n(?:[ \t]*\n)*\Z")

_LIST_FIELDS = ("files", "symbols", "depends_on", "implements", "consumes")
_KNOWN_KEYS = {"task_id", "description", "slice", "size_hints", *_LIST_FIELDS}


class TaskMapError(Exception):
    """The plan's task-map block is present but malformed (hard error, no fallback)."""


def parse_task_map(
    plan_text: str,
    client: CodegraphClient,
    allow_unknown_symbols: bool = False,
) -> MapperOutput | None:
    """Parse the plan's task-map block into verified mappings, or ``None`` if absent.

    Files that don't exist yet are retained as prospective files with an info
    flag. Unknown symbols are a hard error by default (R14): the map's ``symbols:``
    field has no prospective notation, so every listed symbol is a claim that it
    exists — unlike the mapper LLM's guesses, a false claim should stop the run,
    not get silently dropped. ``allow_unknown_symbols=True`` restores the old
    mirror-the-mapper behaviour (drop with a flag). Structural problems (bad
    YAML, duplicate ids, bad ``depends_on``, oversized slices, unknown keys)
    always raise ``TaskMapError``.
    """
    blocks = _BLOCK.findall(plan_text)
    if not blocks:
        versions = _ANY_VERSION.findall(plan_text)
        if versions:
            raise TaskMapError(
                f"unsupported task map version v{versions[0]} (this parser reads v1)"
            )
        return None
    if len(blocks) > 1:
        raise TaskMapError("multiple orchestrator-task-map blocks; exactly one is allowed")

    try:
        payload = yaml.safe_load(blocks[0])
    except yaml.YAMLError as exc:
        raise TaskMapError(f"task map is not valid YAML: {exc}") from exc
    tasks = _validate_shape(payload)

    mappings: list[TaskMapping] = []
    descriptions: dict[str, str] = {}
    flags: list[str] = []
    for entry in tasks:
        task_id = entry["task_id"]
        descriptions[task_id] = entry["description"]
        raw_size_hints: dict[str, str] = entry.get("size_hints") or {}
        files: list[str] = []
        prospective: list[str] = []
        for file in _dedupe(entry.get("files") or []):
            if (client.repo_root / file).is_file():
                if file in raw_size_hints:
                    raise TaskMapError(
                        f"task {task_id!r} size_hints names {file!r}, which already exists — "
                        "hints price unwritten (prospective) work only"
                    )
                files.append(file)
            else:
                prospective.append(file)
                flags.append(
                    f"task map: task {task_id} file {file} does not exist yet — "
                    "retained as prospective"
                )
        size_hints = tuple(
            sorted((f, raw_size_hints[f]) for f in prospective if f in raw_size_hints)
        )
        symbols: list[str] = []
        for symbol in _dedupe(entry.get("symbols") or []):
            if client.symbol_exists(symbol):
                symbols.append(symbol)
            elif allow_unknown_symbols:
                flags.append(f"task map: task {task_id} mapped unknown symbol {symbol} — dropped")
            else:
                raise TaskMapError(
                    f"task {task_id} mapped unknown symbol {symbol!r} — not found in the "
                    "codegraph index (pass --allow-unknown-symbols to drop it instead)"
                )
        mappings.append(
            TaskMapping(
                task_id,
                files=tuple(files),
                symbols=tuple(symbols),
                prospective_files=tuple(prospective),
                size_hints=size_hints,
                depends_on=tuple(_dedupe(entry.get("depends_on") or [])),
                slice=entry.get("slice"),
                implements=tuple(_dedupe(entry.get("implements") or [])),
                consumes=tuple(_dedupe(entry.get("consumes") or [])),
            )
        )
    return MapperOutput(mappings=mappings, descriptions=descriptions, flags=flags)


def strip_task_map(plan_text: str) -> str:
    """Strip the marked task-map block (plus a directly preceding ``## Task Map``
    heading) out of plan text bound for an LLM context (R27).

    The map is this parser's input, not worker/speccer/rewrite context — every
    LLM-facing consumer of the plan text calls this first. Text without a marked
    block passes through unchanged; only the first marker-located block (there is
    at most one in a valid plan — ``parse_task_map`` rejects more) is removed,
    using the same detection this module's parser uses.
    """
    match = _BLOCK.search(plan_text)
    if not match:
        return plan_text
    start = match.start()
    heading_match = _PRECEDING_HEADING.search(plan_text[:start])
    if heading_match:
        start = heading_match.start()
    return plan_text[:start] + plan_text[match.end() :]


def _validate_shape(payload: object) -> list[dict]:
    """Structural validation per docs/orchestrator-task-map.md — every miss is hard."""
    if not isinstance(payload, dict):
        raise TaskMapError("task map top level must be a mapping with a 'tasks' list")
    unknown_top = set(payload) - {"tasks"}
    if unknown_top:
        raise TaskMapError(f"task map has unknown top-level keys: {sorted(unknown_top)}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise TaskMapError("task map 'tasks' must be a non-empty list")

    for entry in tasks:
        if not isinstance(entry, dict):
            raise TaskMapError("each task entry must be a mapping")
        unknown = set(entry) - _KNOWN_KEYS
        if unknown:
            raise TaskMapError(f"task entry has unknown keys: {sorted(unknown)}")
        for key in ("task_id", "description"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise TaskMapError(f"task entry needs a non-empty string {key!r}")
        if entry.get("slice") is not None and (
            not isinstance(entry["slice"], str) or not entry["slice"]
        ):
            raise TaskMapError(f"task {entry['task_id']!r} 'slice' must be a string or null")
        for key in _LIST_FIELDS:
            value = entry.get(key) or []
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise TaskMapError(
                    f"task {entry['task_id']!r} {key!r} must be a list of non-empty strings"
                )
        size_hints = entry.get("size_hints")
        if size_hints is not None:
            if not isinstance(size_hints, dict) or not all(
                isinstance(path, str) and path and isinstance(cls, str) and cls
                for path, cls in size_hints.items()
            ):
                raise TaskMapError(
                    f"task {entry['task_id']!r} 'size_hints' must be a mapping of "
                    "path to size class"
                )
            declared_files = set(entry.get("files") or [])
            for path, cls in size_hints.items():
                if path not in declared_files:
                    raise TaskMapError(
                        f"task {entry['task_id']!r} size_hints names {path!r}, which is "
                        "not in this task's files"
                    )
                if cls not in SIZE_HINT_CLASSES:
                    raise TaskMapError(
                        f"task {entry['task_id']!r} size_hints class {cls!r} for {path!r} "
                        f"must be one of {sorted(SIZE_HINT_CLASSES)}"
                    )

    ids = [entry["task_id"] for entry in tasks]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise TaskMapError(f"duplicate task_id values: {duplicates}")

    id_set = set(ids)
    for entry in tasks:
        for dep in entry.get("depends_on") or []:
            if dep == entry["task_id"]:
                raise TaskMapError(f"task {entry['task_id']!r} depends_on itself")
            if dep not in id_set:
                raise TaskMapError(f"task {entry['task_id']!r} depends_on unknown task {dep!r}")
    _check_acyclic({e["task_id"]: list(e.get("depends_on") or []) for e in tasks})

    slice_members: dict[str, list[str]] = defaultdict(list)
    for entry in tasks:
        if entry.get("slice"):
            slice_members[entry["slice"]].append(entry["task_id"])
    for label, members in sorted(slice_members.items()):
        if len(members) > SLICE_TASK_CAP:
            raise TaskMapError(
                f"slice {label!r} has {len(members)} tasks (cap {SLICE_TASK_CAP}) — "
                "a whole-plan slice contracts to one giant node; split it or drop labels"
            )
    return tasks


def _check_acyclic(depends_on: dict[str, list[str]]) -> None:
    """Kahn's algorithm over declared edges; leftovers are part of a cycle."""
    dependents: dict[str, list[str]] = defaultdict(list)
    indegree = {task: len(ups) for task, ups in depends_on.items()}
    for task, ups in depends_on.items():
        for up in ups:
            dependents[up].append(task)
    queue = sorted(task for task, degree in indegree.items() if degree == 0)
    seen = 0
    while queue:
        task = queue.pop(0)
        seen += 1
        for down in sorted(dependents[task]):
            indegree[down] -= 1
            if indegree[down] == 0:
                queue.append(down)
        queue.sort()
    if seen != len(indegree):
        cyclic = sorted(task for task, degree in indegree.items() if degree > 0)
        raise TaskMapError(f"depends_on cycle among tasks {cyclic}")


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving dedupe (a duplicated file must not double-count bytes)."""
    return list(dict.fromkeys(values))
