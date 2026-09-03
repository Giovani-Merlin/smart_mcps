"""Diagram sources rendered from ``RunFacts`` and git (plan U2): two mermaid
flowcharts (plan → outcome, architecture delta) and one HTML timeline.

Every function here is a pure text renderer: no LLM call, no invented data.
When the underlying fact is unavailable (no git range, no Python files
changed), the mermaid renderer returns a short ``%%`` comment explaining why
instead of raising or fabricating a diagram.

The run timeline is plain HTML/CSS generated here (report v2 U1) — a
``<table class="timeline">`` with one row per group and one positioned bar
per session — because mermaid's gantt squeezed an 11-group run into an
unreadable strip and needed a CDN to render at all.
"""

from __future__ import annotations

import ast
import re
import subprocess
from datetime import datetime
from pathlib import Path

from markupsafe import escape
from pydantic import BaseModel

from orchestrator.report.facts import GroupFacts, RunFacts

_INVALID_ID_RE = re.compile(r"[^A-Za-z0-9_]")
_MERMAID_TEXT_BANNED = str.maketrans({":": "-", ",": ";", "\n": " "})


class Diagrams(BaseModel):
    #: HTML (not mermaid) since report v2: see ``timeline_html``.
    timeline: str = ""
    plan_outcome: str = ""
    architecture_delta: str = ""


# ------------------------------------------------------------------- helpers


def _mermaid_id(name: str) -> str:
    sanitized = _INVALID_ID_RE.sub("_", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = f"n_{sanitized}"
    return sanitized


def _mermaid_text(text: str) -> str:
    return text.translate(_MERMAID_TEXT_BANNED).strip()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _run_git(repo_root: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout


# ----------------------------------------------------------------- timeline


def _fmt_clock(value: datetime) -> str:
    return value.strftime("%H:%M")


def _fmt_minutes(minutes: float) -> str:
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def timeline_html(facts: RunFacts) -> str:
    """The run timeline as an HTML table: one row per group; each cell holds a
    positioned ``<span class="bar coder|reviewer">`` per session (left/width
    are percentages of the run's wall clock) and a ``<span class="mark …">``
    glyph per retirement/escalation. Every string from the facts is escaped
    here, so the template renders the result with ``|safe``."""
    spans: list[tuple[datetime, datetime]] = []
    for group in facts.groups:
        for session in group.sessions:
            start = _parse_dt(session.started_at)
            if start is None:
                continue
            end = _parse_dt(session.ended_at) or start
            spans.append((start, max(end, start)))
    for event in facts.timeline:
        at = _parse_dt(event.at)
        if at is not None:
            spans.append((at, at))
    if not spans:
        return '<p class="muted timeline-empty">no timestamped sessions recorded for this run</p>'
    run_start = min(start for start, _ in spans)
    run_end = max(end for _, end in spans)
    total_minutes = max((run_end - run_start).total_seconds() / 60, 1.0)

    def pct(value: datetime) -> float:
        return max(0.0, min(100.0, (value - run_start).total_seconds() / 60 / total_minutes * 100))

    rows: list[str] = []
    for group in facts.groups:
        cells: list[str] = []
        for session in group.sessions:
            start = _parse_dt(session.started_at)
            if start is None:
                continue
            end = _parse_dt(session.ended_at)
            known_end = end is not None and end > start
            end = end if end is not None and end > start else start
            left = pct(start)
            width = max(pct(end) - left, 0.4)
            role = "reviewer" if session.role == "reviewer" else "coder"
            duration = _fmt_minutes((end - start).total_seconds() / 60) if known_end else "n/a"
            title = (
                f"{group.id} {session.role} gen{session.generation}: "
                f"{_fmt_clock(start)}–{_fmt_clock(end) if known_end else '?'} ({duration})"
            )
            if session.ended_at_source not in ("manifest", "unknown"):
                title += f"; end inferred from {session.ended_at_source}"
            classes = f"bar {role}" + ("" if known_end else " open")
            cells.append(
                f'<span class="{classes}" style="left:{left:.2f}%;width:{width:.2f}%" '
                f'title="{escape(title)}"></span>'
            )
            if session.retirement_reason:
                cells.append(
                    f'<span class="mark retired" style="left:{pct(end):.2f}%" '
                    f'title="{escape(f"retired: {session.retirement_reason}")}">&#x2715;</span>'
                )
        for event in facts.timeline:
            if event.group_id != group.id or event.kind != "escalation":
                continue
            at = _parse_dt(event.at)
            if at is None:
                continue
            cells.append(
                f'<span class="mark escalation" style="left:{pct(at):.2f}%" '
                f'title="{escape(f"escalation: {event.label}")}">&#x26A0;</span>'
            )
        label = escape(f"{group.id}: {group.name or group.id}")
        anchor = escape(f"#group-{group.id}")
        state = escape(group.state)
        rows.append(
            f'<tr><th scope="row"><a href="{anchor}">{label}</a></th>'
            f'<td class="lane">{"".join(cells)}</td>'
            f'<td class="state">{state}</td></tr>'
        )

    ticks = 6
    axis = "".join(
        f'<span class="tick" style="left:{i / ticks * 100:.2f}%">'
        f"{_fmt_clock(run_start + (run_end - run_start) * i / ticks)}</span>"
        for i in range(ticks + 1)
    )
    caption = escape(
        f"{_fmt_clock(run_start)} → {_fmt_clock(run_end)} "
        f"({_fmt_minutes(total_minutes)} wall clock, {run_start.strftime('%Y-%m-%d')})"
    )
    return (
        '<table class="timeline">'
        f"<caption>{caption}</caption>"
        '<thead><tr><th scope="col">Group</th>'
        f'<th scope="col" class="lane"><div class="axis">{axis}</div></th>'
        '<th scope="col">State</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


# --------------------------------------------------------------- flowchart


def _state_class(group: GroupFacts) -> str:
    if group.state == "resolved":
        return "resolved"
    if group.state == "completed" and not group.failure:
        return "ok"
    return "fail"


def plan_outcome_flowchart(facts: RunFacts) -> str:
    lines = [
        "flowchart LR",
        "    classDef ok fill:#d1f5d3,stroke:#2f9e44,color:#1a1a1a;",
        "    classDef fail fill:#ffd6d6,stroke:#c92a2a,color:#1a1a1a;",
        "    classDef resolved fill:#fff3bf,stroke:#e8a400,color:#1a1a1a;",
    ]
    group_by_id = {g.id: g for g in facts.groups}
    group_nodes: dict[str, str] = {}

    def group_node(group_id: str) -> str:
        node_id = _mermaid_id(f"grp_{group_id}")
        if group_id not in group_nodes:
            group = group_by_id.get(group_id)
            label = _mermaid_text(f"{group_id}: {group.name}" if group else group_id)
            lines.append(f'    {node_id}["{label}"]')
            group_nodes[group_id] = node_id
        return node_id

    for unit in facts.units:
        unit_node = _mermaid_id(f"u_{unit.unit_id}")
        unit_label = _mermaid_text(f"{unit.unit_id}: {unit.title}")[:80]
        lines.append(f'    {unit_node}["{unit_label}"]')
        if not unit.group_id:
            continue
        node_id = group_node(unit.group_id)
        group = group_by_id.get(unit.group_id)
        edge_label = _mermaid_text(group.verdict_status) if group and group.verdict_status else ""
        if edge_label:
            lines.append(f"    {unit_node} -->|{edge_label}| {node_id}")
        else:
            lines.append(f"    {unit_node} --> {node_id}")

    for group in facts.groups:
        node_id = group_node(group.id)
        lines.append(f"    {node_id}:::{_state_class(group)}")

    return "\n".join(lines)


# ---------------------------------------------------------- architecture delta


def _module_name(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    parts = stem.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _git_show(repo_root: Path, sha: str, path: str) -> str | None:
    return _run_git(repo_root, "show", f"{sha}:{path}")


def _list_py_modules(repo_root: Path, sha: str) -> set[str]:
    out = _run_git(repo_root, "ls-tree", "-r", "--name-only", sha)
    if not out:
        return set()
    return {_module_name(line) for line in out.splitlines() if line.endswith(".py")}


def _parse_imports(source: str, current_module: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    modules: set[str] = set()
    package_parts = current_module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    modules.add(node.module)
                continue
            up = node.level - 1
            base_parts = package_parts[: len(package_parts) - up] if up else package_parts
            if node.module:
                modules.add(".".join([*base_parts, node.module]))
            elif base_parts:
                modules.add(".".join(base_parts))
    return modules


def architecture_delta(facts: RunFacts, repo_root: Path) -> str:
    if not facts.git_range.available:
        return "%% architecture delta unavailable: no git range for this run"
    base_sha = facts.git_range.base_sha
    tip_sha = facts.git_range.tip_sha
    assert base_sha is not None and tip_sha is not None

    py_paths = [cf.path for cf in facts.changed_files if cf.path.endswith(".py")]
    if not py_paths:
        return "%% architecture delta unavailable: no Python files changed in this run"

    known_at_base = _list_py_modules(repo_root, base_sha)
    known_at_tip = _list_py_modules(repo_root, tip_sha)

    node_class: dict[str, str] = {}
    edges: set[tuple[str, str]] = set()

    for path in py_paths:
        module = _module_name(path)
        base_src = _git_show(repo_root, base_sha, path)
        tip_src = _git_show(repo_root, tip_sha, path)
        if base_src is None and tip_src is not None:
            node_class[module] = "added"
        elif base_src is not None and tip_src is None:
            node_class[module] = "removed"
        else:
            node_class.setdefault(module, "kept")

        if tip_src is not None:
            for imported in _parse_imports(tip_src, module):
                if imported in known_at_tip and imported != module:
                    edges.add((module, imported))
                    node_class.setdefault(imported, "kept")
        elif base_src is not None:
            for imported in _parse_imports(base_src, module):
                if imported in known_at_base and imported != module:
                    edges.add((module, imported))
                    node_class.setdefault(
                        imported, "kept" if imported in known_at_tip else "removed"
                    )

    if not node_class:
        return "%% architecture delta unavailable: no Python files changed in this run"

    lines = [
        "flowchart LR",
        "    classDef added fill:#d1f5d3,stroke:#2f9e44,color:#1a1a1a;",
        "    classDef removed fill:#ffd6d6,stroke:#c92a2a,color:#1a1a1a;",
        "    classDef kept fill:#e7e7e7,stroke:#868e96,color:#1a1a1a;",
    ]
    node_ids: dict[str, str] = {}
    for module in sorted(node_class):
        node_id = _mermaid_id(module)
        node_ids[module] = node_id
        lines.append(f'    {node_id}["{module}"]:::{node_class[module]}')
    for source, target in sorted(edges):
        if target not in node_ids:
            node_id = _mermaid_id(target)
            node_ids[target] = node_id
            lines.append(f'    {node_id}["{target}"]:::kept')
        lines.append(f"    {node_ids[source]} --> {node_ids[target]}")

    return "\n".join(lines)


# ------------------------------------------------------------------ bundle


def render_all(facts: RunFacts, repo_root: Path) -> Diagrams:
    return Diagrams(
        timeline=timeline_html(facts),
        plan_outcome=plan_outcome_flowchart(facts),
        architecture_delta=architecture_delta(facts, repo_root),
    )
