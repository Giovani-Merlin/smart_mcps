"""Deterministic plan-document parser: preamble, per-unit sections, and the
shared digest that replaces embedding the full plan in worker context (R1, R21,
R22, R23).

A plan written for ``orchestrator-plan`` has a ``## Units`` section whose
sub-headings are ``### U<N>. <name> — <goal>``. Task ids of the form
``u<N>-<slug>`` are this convention's task-map counterpart to heading ``U<N>``.
Older or foreign plans (no ``## Units`` heading, or task ids that don't follow
the ``u<N>-`` convention) keep working: they simply carry no unit sections and
contribute nothing to the digest beyond their preamble.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from orchestrator.grouping.plan_reader import strip_task_map

_UNITS_HEADING = re.compile(r"^## Units[ \t]*$", re.MULTILINE)
_H2_HEADING = re.compile(r"^## [ \t]*\S", re.MULTILINE)
_UNIT_HEADING = re.compile(r"^### U(?P<num>\d+)\.[ \t]*(?P<rest>.*)$", re.MULTILINE)
_BULLET = re.compile(r"^-\s+\*\*(?P<label>[^*]+)\*\*:\s?(?P<rest>.*)$")
_TASK_MAP_BLOCK = re.compile(
    r"```ya?ml[ \t]*\n(?P<body>[ \t]*# orchestrator-task-map v1[ \t]*\n.*?)```",
    re.DOTALL,
)
_TASK_ID_UNIT_PREFIX = re.compile(r"^u(\d+)-")


class PlanSectionsError(Exception):
    """The plan's ``## Units`` structure doesn't cover every declared unit-id task."""


@dataclass(frozen=True)
class UnitSection:
    unit_id: str  # "u1", "u2", ... (matches the numeric part of the U<N> heading)
    title: str  # heading text after "### U<N>. "
    text: str  # verbatim section text (heading line through the next heading), found in source
    summary: str
    summary_is_fallback: bool
    verification: tuple[str, ...] = field(default_factory=tuple)
    implements: tuple[str, ...] = field(default_factory=tuple)
    consumes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PlanSections:
    preamble: str
    units: dict[str, UnitSection]
    digest: str
    flags: tuple[str, ...] = field(default_factory=tuple)


def unit_key_for_task(task_id: str) -> str | None:
    """``u3-layered-context`` -> ``u3``; ``t1-scaffold`` -> ``None`` (not this
    convention, no unit-section is expected for it)."""
    match = _TASK_ID_UNIT_PREFIX.match(task_id)
    return f"u{match.group(1)}" if match else None


def section_for_task(units: dict[str, UnitSection], task_id: str) -> UnitSection | None:
    key = unit_key_for_task(task_id)
    return units.get(key) if key is not None else None


def _declared_unit_task_ids(plan_text: str) -> list[str]:
    """Best-effort extraction of ``task_id`` values from the plan's own embedded
    task map, used only to cross-check unit-heading coverage. A malformed or
    absent map raises nothing here — ``plan_reader.parse_task_map`` is the
    authority on map validity; this is purely an auxiliary read."""
    match = _TASK_MAP_BLOCK.search(plan_text)
    if not match:
        return []
    try:
        payload = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return []
    if not isinstance(payload, dict):
        return []
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [
        task["task_id"]
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("task_id"), str)
    ]


def _split_bullets(body: str) -> dict[str, str]:
    """Top-level ``- **Label**: value`` bullets, continuation lines folded in."""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _BULLET.match(line)
        if match:
            current = match.group("label").strip()
            fields[current] = [match.group("rest")]
        elif current is not None:
            fields[current].append(line)
    return {label: "\n".join(lines).strip() for label, lines in fields.items()}


def _parse_verification_items(raw: str) -> tuple[str, ...]:
    """``raw`` is the folded value of the ``Verification`` bullet: either a single
    sentence, or a sub-bullet list (one item per ``- ``, continuation lines
    joined into the same item)."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines or lines == ["—"]:
        return ()
    if not lines[0].startswith("- "):
        return (" ".join(lines),)
    bullets: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("- "):
            if current:
                bullets.append(" ".join(current))
            current = [line[2:].strip()]
        else:
            current.append(line)
    if current:
        bullets.append(" ".join(current))
    return tuple(bullets)


def _parse_tags(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    implements: list[str] = []
    consumes: list[str] = []
    for keyword, bucket in (("implements", implements), ("consumes", consumes)):
        for match in re.finditer(rf"{keyword}\b([^;]*)", text, re.IGNORECASE):
            bucket.extend(re.findall(r"`([^`]+)`", match.group(1)))
    return tuple(dict.fromkeys(implements)), tuple(dict.fromkeys(consumes))


def _build_unit_section(num: str, heading_rest: str, text: str, flags: list[str]) -> UnitSection:
    unit_id = f"u{num}"
    title = heading_rest.strip()
    body_after_heading = "\n".join(text.splitlines()[1:])
    bullets = _split_bullets(body_after_heading)

    summary_raw = bullets.get("Summary", "").strip()
    if summary_raw and summary_raw != "—":
        summary, is_fallback = summary_raw, False
    else:
        summary, is_fallback = title, True
        flags.append(
            f"plan-sections: unit {unit_id} has no 'Summary:' line — "
            f"falling back to its heading title ({title!r})"
        )

    verification = _parse_verification_items(bullets.get("Verification", ""))
    implements, consumes = _parse_tags(bullets.get("Implements / Consumes", ""))

    return UnitSection(
        unit_id=unit_id,
        title=title,
        text=text,
        summary=summary,
        summary_is_fallback=is_fallback,
        verification=verification,
        implements=implements,
        consumes=consumes,
    )


def _build_digest(preamble: str, units: dict[str, UnitSection]) -> str:
    ordered = sorted(units.values(), key=lambda u: int(u.unit_id[1:]))
    lines = [preamble.rstrip("\n"), "", "## Unit summaries"]
    for unit in ordered:
        lines.append(f"- {unit.unit_id.upper()} ({unit.title}) Summary: {unit.summary}")
    lines.append("")
    lines.append("## Implements / Consumes registry")
    any_tags = False
    for unit in ordered:
        if unit.implements:
            lines.append(f"- {unit.unit_id.upper()} implements: {', '.join(unit.implements)}")
            any_tags = True
        if unit.consumes:
            lines.append(f"- {unit.unit_id.upper()} consumes: {', '.join(unit.consumes)}")
            any_tags = True
    if not any_tags:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def parse_plan_sections(plan_text: str) -> PlanSections:
    """Split ``plan_text`` into its preamble, per-unit sections, and digest.

    Deterministic and pure: the same text always yields byte-identical output.
    """
    units_match = _UNITS_HEADING.search(plan_text)
    if units_match is None:
        preamble = strip_task_map(plan_text)
        return PlanSections(preamble=preamble, units={}, digest=_build_digest(preamble, {}))

    preamble = strip_task_map(plan_text[: units_match.start()])

    next_h2 = _H2_HEADING.search(plan_text, units_match.end())
    units_span_end = next_h2.start() if next_h2 else len(plan_text)
    units_span = plan_text[units_match.start() : units_span_end]

    headings = list(_UNIT_HEADING.finditer(units_span))
    flags: list[str] = []
    units: dict[str, UnitSection] = {}
    for index, match in enumerate(headings):
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(units_span)
        section_text = units_span[match.start() : section_end]
        unit = _build_unit_section(match.group("num"), match.group("rest"), section_text, flags)
        units[unit.unit_id] = unit

    missing = [
        task_id
        for task_id in _declared_unit_task_ids(plan_text)
        if (key := unit_key_for_task(task_id)) is not None and key not in units
    ]
    if missing:
        raise PlanSectionsError(
            "plan sections: missing '### U<N>.' heading for task id(s): " + ", ".join(missing)
        )

    return PlanSections(
        preamble=preamble,
        units=units,
        digest=_build_digest(preamble, units),
        flags=tuple(flags),
    )
