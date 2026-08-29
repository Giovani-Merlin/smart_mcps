"""Verbatim plan-document surgery: the single write path for programmatic plan
edits (``orchestrate split``, ``/orchestrator-deepen``).

Both consumers move unit sections and task-map entries between documents
without ever regenerating their prose, and both need the same guarantee: a
rewrite either leaves the task map and unit ids untouched, or it is refused
with every difference it finds named, not just the first. This module is
that guarantee's one implementation — extraction, reassembly, and the
``verify_map_unchanged`` comparator all live here so neither consumer has to
duplicate the fence/heading regexes.

No LLM, no codegraph: everything here is a pure string/YAML operation over
already-loaded plan text, sub-second even on the repo's largest plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from orchestrator.grouping.plan_reader import task_map_block_span
from orchestrator.grouping.plan_sections import UNIT_HEADING, locate_units_span

_ENTRY_START = re.compile(r"^  - task_id:[ \t]*(?P<task_id>\S+)[ \t]*$", re.MULTILINE)
_FENCE = "```"


class PlanEditError(Exception):
    """A plan document cannot be surgically split or reassembled as asked."""


@dataclass(frozen=True)
class TaskMapDocument:
    """A plan's task-map block, sliced into verbatim, order-preserving pieces.

    ``header`` runs from the opening fence through the line before the first
    ``  - task_id:`` entry (the version marker and ``tasks:`` line);
    ``footer`` is the closing fence. ``header + "".join(entries[t] for t in
    order) + footer`` reconstructs the original block exactly.
    """

    header: str
    footer: str
    order: tuple[str, ...]
    entries: dict[str, str]


def extract_task_map_entries(plan_text: str) -> TaskMapDocument | None:
    """Split ``plan_text``'s task-map block into verbatim per-task entries.

    ``None`` if the plan carries no task-map block at all, mirroring
    ``parse_task_map``'s absent-block convention. A block that exists but
    parses no ``  - task_id:`` entries, or repeats one, is a hard error —
    unlike absence, that is drift worth stopping on.
    """
    span = task_map_block_span(plan_text)
    if span is None:
        return None
    block = plan_text[span[0] : span[1]]
    starts = list(_ENTRY_START.finditer(block))
    if not starts:
        raise PlanEditError("task-map block has no '  - task_id:' entries")

    footer_start = len(block) - len(_FENCE)
    header = block[: starts[0].start()]
    footer = block[footer_start:]

    order: list[str] = []
    entries: dict[str, str] = {}
    for index, match in enumerate(starts):
        entry_end = starts[index + 1].start() if index + 1 < len(starts) else footer_start
        task_id = match.group("task_id")
        if task_id in entries:
            raise PlanEditError(f"task-map block: duplicate task_id {task_id!r}")
        entries[task_id] = block[match.start() : entry_end]
        order.append(task_id)
    return TaskMapDocument(header=header, footer=footer, order=tuple(order), entries=entries)


def render_task_map_block(doc: TaskMapDocument, task_ids: list[str] | None = None) -> str:
    """Rebuild a fenced task-map block from ``doc``, keeping only ``task_ids``
    (default: all of them, in their original order) — each entry's bytes are
    untouched, only which entries appear changes.
    """
    ids = list(task_ids) if task_ids is not None else list(doc.order)
    unknown = [tid for tid in ids if tid not in doc.entries]
    if unknown:
        raise PlanEditError(f"task-map block has no entry for task_id(s): {unknown}")
    body = "".join(doc.entries[tid] for tid in ids)
    return doc.header + body + doc.footer


def split_units(plan_text: str) -> tuple[str, dict[str, str], tuple[str, ...], str]:
    """Split ``plan_text`` into ``(head, unit_texts, order, tail)``.

    ``head`` runs up to the first ``### U<N>.`` heading (including anything
    before it, the ``## Units`` heading, and the task-map block if it lives
    there); ``tail`` is whatever follows the ``## Units`` section (the next
    top-level ``## `` heading, or nothing). ``head + "".join(unit_texts[u] for
    u in order) + tail`` reconstructs ``plan_text`` exactly.
    """
    span = locate_units_span(plan_text)
    if span is None:
        raise PlanEditError("plan has no '## Units' section")
    units_start, units_end = span
    headings = list(UNIT_HEADING.finditer(plan_text, units_start, units_end))
    if not headings:
        raise PlanEditError("plan has no '### U<N>.' unit sections")

    head = plan_text[: headings[0].start()]
    tail = plan_text[units_end:]

    order: list[str] = []
    unit_texts: dict[str, str] = {}
    for index, match in enumerate(headings):
        section_end = headings[index + 1].start() if index + 1 < len(headings) else units_end
        unit_id = f"u{match.group('num')}"
        if unit_id in unit_texts:
            raise PlanEditError(f"plan has duplicate unit heading: {unit_id!r}")
        unit_texts[unit_id] = plan_text[match.start() : section_end]
        order.append(unit_id)
    return head, unit_texts, tuple(order), tail


def _unit_key_for_task(task_id: str) -> str | None:
    match = re.match(r"^u(\d+)-", task_id)
    return f"u{match.group(1)}" if match else None


def _safe_split_units(plan_text: str) -> dict[str, str]:
    try:
        _, unit_texts, _, _ = split_units(plan_text)
    except PlanEditError:
        return {}
    return unit_texts


def validate_plan(plan_text: str) -> list[str]:
    """Structural-only consistency checks for one plan: does its task map parse,
    does every map task have a matching ``### U<N>.`` section, and does every
    section have a matching map entry. Zero LLM, zero codegraph — a plan
    without either convention (no map, no ``## Units``) validates clean,
    mirroring the compatibility rule the rest of the map tooling follows.
    """
    problems: list[str] = []
    try:
        doc = extract_task_map_entries(plan_text)
    except PlanEditError as exc:
        return [str(exc)]

    unit_texts = _safe_split_units(plan_text)
    if doc is None:
        return problems

    mapped_unit_keys: set[str] = set()
    for task_id in doc.order:
        key = _unit_key_for_task(task_id)
        if key is None:
            continue
        mapped_unit_keys.add(key)
        if key not in unit_texts:
            problems.append(f"task map entry {task_id!r} has no matching '### U{key[1:]}.' section")
    for unit_id in unit_texts:
        if unit_id not in mapped_unit_keys:
            problems.append(f"unit section {unit_id!r} has no matching task-map entry")
    return problems


def _parse_entry_fields(entry_text: str) -> dict:
    payload = yaml.safe_load("tasks:\n" + entry_text)
    return payload["tasks"][0]


def _describe_entry_diff(task_id: str, before_text: str, after_text: str) -> list[str]:
    try:
        before_fields = _parse_entry_fields(before_text)
        after_fields = _parse_entry_fields(after_text)
    except yaml.YAMLError:
        return [f"task {task_id!r}: map entry changed"]
    messages: list[str] = []
    for key in sorted(set(before_fields) | set(after_fields)):
        before_value, after_value = before_fields.get(key), after_fields.get(key)
        if before_value != after_value:
            messages.append(
                f"task {task_id!r}: {key!r} changed from {before_value!r} to {after_value!r}"
            )
    return messages or [f"task {task_id!r}: map entry changed"]


def verify_map_unchanged(before: str, after: str) -> list[str]:
    """Compare two plan documents' task-map entries and unit ids.

    Reports every difference it finds, not just the first: entries or unit
    sections present in one document and not the other, and — for task ids
    surviving in both — every field whose value differs (``depends_on``,
    ``size_hints``, or any other map field). Empty list means the map and
    unit ids are unchanged between the two documents.
    """
    diffs: list[str] = []

    before_doc = extract_task_map_entries(before)
    after_doc = extract_task_map_entries(after)
    before_entries = before_doc.entries if before_doc else {}
    after_entries = after_doc.entries if after_doc else {}

    for task_id in before_entries:
        if task_id not in after_entries:
            diffs.append(f"task map: {task_id!r} was removed")
    for task_id in after_entries:
        if task_id not in before_entries:
            diffs.append(f"task map: {task_id!r} was added")
    for task_id in sorted(set(before_entries) & set(after_entries)):
        if before_entries[task_id] != after_entries[task_id]:
            diffs.extend(
                _describe_entry_diff(task_id, before_entries[task_id], after_entries[task_id])
            )

    before_units = _safe_split_units(before)
    after_units = _safe_split_units(after)
    for unit_id in before_units:
        if unit_id not in after_units:
            diffs.append(f"unit section {unit_id!r} was removed")
    for unit_id in after_units:
        if unit_id not in before_units:
            diffs.append(f"unit section {unit_id!r} was added")

    return diffs
