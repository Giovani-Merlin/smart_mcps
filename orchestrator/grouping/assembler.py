"""Deterministic spec assembly (plan U2): replaces the grouping-time speccer.

The planner session already wrote the prose that matters — a paraphrase pass
over it added cost and drift surface, not information (ADR 0006). Every
group's name, summary, spec, and verification are built here from graph/DAG
facts and the plan's own unit sections, with zero LLM calls.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from orchestrator.grouping.graphing import TaskGraph
from orchestrator.grouping.partition import Partition
from orchestrator.grouping.plan_sections import PlanSections, UnitSection, section_for_task
from orchestrator.model import SUMMARY_MAX_CHARS, GroupSpec, VerificationItem

#: groups.json flag recorded whenever assembly (not the speccer) produced specs.
ASSEMBLED_FLAG = "specs: assembled from plan — speccer LLM skipped"


class AssemblyError(Exception):
    """Deterministic spec assembly could not produce a valid, fully-covered result."""


def _truncate_summary(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    return text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"


def _topo_order(members: list[str], graph: TaskGraph) -> list[str]:
    """Member tasks in dependency order (Kahn's algorithm over the intra-group
    subset of ``graph.dependencies``); ties broken lexicographically for
    determinism."""
    member_set = set(members)
    indegree = {m: 0 for m in members}
    downstream: dict[str, list[str]] = {m: [] for m in members}
    for up, down in sorted(graph.dependencies):
        if up in member_set and down in member_set:
            indegree[down] += 1
            downstream[up].append(down)
    ready = sorted(m for m in members if indegree[m] == 0)
    ordered: list[str] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node)
        for down in sorted(downstream[node]):
            indegree[down] -= 1
            if indegree[down] == 0:
                ready.append(down)
        ready.sort()
    # A cycle inside one group is unreachable (build_group_dag already asserts
    # acyclicity at the group level, and intra-group cycles can't exist without
    # one) — but never silently drop a node if it somehow happened.
    ordered.extend(m for m in members if m not in ordered)
    return ordered


def _member_label(task_id: str, unit: UnitSection | None, description: str) -> str:
    if unit is not None:
        return unit.title
    return description or task_id


def _group_name(ordered_members: list[str], units: Mapping[str, UnitSection | None]) -> str:
    labels = [
        _member_label(task_id, units.get(task_id), "") or task_id for task_id in ordered_members
    ]
    return " + ".join(labels) if labels else "empty group"


def _group_summary(ordered_members: list[str], units: Mapping[str, UnitSection | None]) -> str:
    if len(ordered_members) == 1:
        task_id = ordered_members[0]
        unit = units.get(task_id)
        text = unit.summary if unit is not None else task_id
        return _truncate_summary(text)
    labels = [
        _member_label(task_id, units.get(task_id), "") or task_id for task_id in ordered_members
    ]
    return _truncate_summary(f"{len(ordered_members)} units: " + ", ".join(labels))


def _relational_header(
    gid_str: str,
    name: str,
    ordered_members: list[str],
    graph: TaskGraph,
    units: Mapping[str, UnitSection | None],
    descriptions: Mapping[str, str],
    upstream_labels: list[str],
    downstream_labels: list[str],
    contract_labels: list[str],
    slice_label: str | None,
) -> str:
    lines = [f"# Group {gid_str}: {name}", ""]
    lines.append("Members:")
    for task_id in ordered_members:
        unit = units.get(task_id)
        label = unit.title if unit is not None else descriptions.get(task_id, task_id)
        lines.append(f"- {task_id}: {label}")
    lines.append("")

    intra_edges = sorted(
        (up, down)
        for (up, down) in graph.dependencies
        if up in ordered_members and down in ordered_members
    )
    lines.append("Intra-group order (depends_on):")
    if intra_edges:
        for up, down in intra_edges:
            lines.append(f"- {up} before {down}")
    else:
        lines.append("- (no intra-group dependencies)")
    lines.append("")

    lines.append("Upstream groups:")
    if upstream_labels:
        lines.extend(f"- {label}" for label in upstream_labels)
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("Downstream groups:")
    if downstream_labels:
        lines.extend(f"- {label}" for label in downstream_labels)
    else:
        lines.append("- (none)")
    lines.append("")

    # plan U3: contracts-only lines for cross-group units this group consumes
    # from or provides to — derived purely from implements/consumes tag matches
    # (not from graph.dependencies, which only covers declared depends_on
    # edges and misses a pure tag relationship like an unrelated-slice
    # consumer). One line per counterpart, never the counterpart's full
    # section.
    lines.append("Contracts (cross-group):")
    if contract_labels:
        lines.extend(f"- {label}" for label in contract_labels)
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append(f"Slice: {slice_label or '(none)'}")
    lines.append("")
    return "\n".join(lines)


def _contract_label(
    gid_str: str, task_id: str, unit: UnitSection | None, tag: str, direction: str
) -> str:
    summary = unit.summary if unit is not None else task_id
    return f"{gid_str} {direction} `{tag}` — {task_id}: {summary}"


@dataclass(frozen=True)
class AssemblyInputs:
    plan_sections: PlanSections
    graph: TaskGraph
    partition: Partition
    dag: dict[int, set[int]]
    members_by_gid: dict[int, list[str]]
    descriptions: Mapping[str, str]
    group_label: Callable[[int], str]


def assemble_group_specs(inputs: AssemblyInputs) -> dict[str, GroupSpec]:
    """Build every group's ``GroupSpec`` with zero LLM calls (plan U2).

    Raises ``AssemblyError`` if a unit that maps to a task actually present in
    this partition has no Verification bullets — a hard signal the plan lost
    its verification section, not something assembly can paper over.
    """
    units = inputs.plan_sections.units
    unit_by_task: dict[str, UnitSection | None] = {
        task_id: section_for_task(units, task_id)
        for members in inputs.members_by_gid.values()
        for task_id in members
    }

    upstream_of: dict[int, list[int]] = {gid: [] for gid in inputs.members_by_gid}
    for up_gid, downs in inputs.dag.items():
        for down_gid in downs:
            upstream_of[down_gid].append(up_gid)

    task_group: dict[str, int] = {
        task_id: gid for gid, members in inputs.members_by_gid.items() for task_id in members
    }
    implementers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    for task_id, unit in sorted(unit_by_task.items()):
        if unit is None:
            continue
        for tag in unit.implements:
            implementers.setdefault(tag, []).append(task_id)
        for tag in unit.consumes:
            consumers.setdefault(tag, []).append(task_id)

    specs: dict[str, GroupSpec] = {}
    covered_unit_ids: set[str] = set()

    for gid, members in sorted(inputs.members_by_gid.items()):
        gid_str = inputs.group_label(gid)
        ordered_members = _topo_order(sorted(members), inputs.graph)
        name = _group_name(ordered_members, unit_by_task)
        summary = _group_summary(ordered_members, unit_by_task)

        member_set = set(members)
        upstream_labels: list[str] = []
        for up_gid in sorted(upstream_of[gid]):
            up_gid_str = inputs.group_label(up_gid)
            for task_id in sorted(inputs.members_by_gid[up_gid]):
                for up, down in sorted(inputs.graph.dependencies):
                    if up == task_id and down in member_set:
                        unit = unit_by_task.get(task_id)
                        implements = unit.implements if unit is not None else ()
                        tag = implements[0] if implements else task_id
                        upstream_labels.append(
                            _contract_label(up_gid_str, task_id, unit, tag, "provides")
                        )
        downstream_labels: list[str] = []
        for down_gid in sorted(inputs.dag.get(gid, ())):
            down_gid_str = inputs.group_label(down_gid)
            for task_id in sorted(inputs.members_by_gid[down_gid]):
                for up, down in sorted(inputs.graph.dependencies):
                    if down == task_id and up in member_set:
                        unit = unit_by_task.get(task_id)
                        consumes = unit.consumes if unit is not None else ()
                        tag = consumes[0] if consumes else task_id
                        downstream_labels.append(
                            _contract_label(down_gid_str, task_id, unit, tag, "consumes")
                        )

        contract_labels: list[str] = []
        for task_id in ordered_members:
            unit = unit_by_task.get(task_id)
            if unit is None:
                continue
            for tag in unit.consumes:
                for other_task in implementers.get(tag, ()):
                    if task_group.get(other_task) != gid:
                        other_unit = unit_by_task.get(other_task)
                        contract_labels.append(
                            _contract_label(gid_str, other_task, other_unit, tag, "consumes")
                        )
            for tag in unit.implements:
                for other_task in consumers.get(tag, ()):
                    if task_group.get(other_task) != gid:
                        other_unit = unit_by_task.get(other_task)
                        contract_labels.append(
                            _contract_label(gid_str, other_task, other_unit, tag, "provides")
                        )

        slice_labels = sorted(
            {str(inputs.graph.metadata.get(task_id, {}).get("slice") or "") for task_id in members}
            - {""}
        )
        slice_label = ", ".join(slice_labels) if slice_labels else None

        header = _relational_header(
            gid_str=gid_str,
            name=name,
            ordered_members=ordered_members,
            graph=inputs.graph,
            units=unit_by_task,
            descriptions=inputs.descriptions,
            upstream_labels=sorted(set(upstream_labels)),
            downstream_labels=sorted(set(downstream_labels)),
            contract_labels=sorted(set(contract_labels)),
            slice_label=slice_label,
        )

        body_parts = [header]
        verification: list[VerificationItem] = []
        counter = 0
        for task_id in ordered_members:
            unit = unit_by_task.get(task_id)
            if unit is not None:
                body_parts.append(unit.text)
                covered_unit_ids.add(unit.unit_id)
                for description in unit.verification:
                    counter += 1
                    verification.append(
                        VerificationItem(
                            id=f"{gid_str}-{counter}",
                            description=description,
                            required=True,
                        )
                    )
            else:
                body_parts.append(f"### {task_id}\n\n{inputs.descriptions.get(task_id, '')}\n")

        specs[gid_str] = GroupSpec(
            group_id=gid_str,
            name=name,
            summary=summary,
            spec="\n\n".join(body_parts),
            verification=verification,
        )

    _lint_verification_coverage(units, unit_by_task, covered_unit_ids)
    return specs


def _lint_verification_coverage(
    units: Mapping[str, UnitSection],
    unit_by_task: Mapping[str, UnitSection | None],
    covered_unit_ids: set[str],
) -> None:
    """Every unit that maps to a task actually present in this grouping must
    have contributed at least one Verification item to some group."""
    present_unit_ids = {unit.unit_id for unit in unit_by_task.values() if unit is not None}
    for unit_id in sorted(present_unit_ids):
        unit = units[unit_id]
        if not unit.verification:
            raise AssemblyError(
                f"unit {unit_id} ({unit.title!r}) has no Verification bullets — "
                "every grouped unit must contribute at least one verification item"
            )
        if unit_id not in covered_unit_ids:
            raise AssemblyError(
                f"unit {unit_id} ({unit.title!r}) contributed no VerificationItem "
                "to any group — this is an assembler bug"
            )
