"""Mermaid diagram sources rendered from ``RunFacts`` and git (plan U2).

Every function here is a pure text renderer: no LLM call, no invented data.
When the underlying fact is unavailable (no git range, no Python files
changed, ``codegraph`` not on ``PATH``), the renderer returns a short
``%%`` comment explaining why instead of raising or fabricating a diagram.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from orchestrator.report.facts import GroupFacts, RunFacts

_INVALID_ID_RE = re.compile(r"[^A-Za-z0-9_]")
_MERMAID_TEXT_BANNED = str.maketrans({":": "-", ",": ";", "\n": " "})
_ADD_PARSER_RE = re.compile(r'add_parser\(\s*"([\w-]+)"')
_RUN_ID_RE = re.compile(r"r(\d{8})-(\d{6})")


class Diagrams(BaseModel):
    timeline: str = ""
    plan_outcome: str = ""
    architecture_delta: str = ""
    howto_sequences: list[str] = Field(default_factory=list)
    howto_note: str | None = None


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


def _fmt_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _run_id_dt(run_id: str) -> datetime:
    match = _RUN_ID_RE.search(run_id)
    if not match:
        return datetime(1970, 1, 1)
    date_part, time_part = match.groups()
    try:
        return datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    except ValueError:
        return datetime(1970, 1, 1)


def _run_git(repo_root: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout


# -------------------------------------------------------------------- gantt


def timeline_gantt(facts: RunFacts) -> str:
    lines = [
        "gantt",
        f"    title {_mermaid_text('Run timeline — ' + facts.run_id)}",
        "    dateFormat YYYY-MM-DDTHH:mm:ss",
        "    axisFormat %H:%M",
    ]
    run_start = _run_id_dt(facts.run_id)
    for group in facts.groups:
        lines.append(f"    section {_mermaid_text(group.id + ': ' + (group.name or group.id))}")
        last_end = None
        for index, session in enumerate(group.sessions, start=1):
            start = _parse_dt(session.started_at)
            if start is None:
                continue
            end = _parse_dt(session.ended_at) or start
            if end <= start:
                end = start + timedelta(minutes=1)
            task_id = _mermaid_id(f"{group.id}_{session.role}_{session.generation}_{index}")
            label = _mermaid_text(f"{session.role} gen{session.generation}")
            lines.append(f"    {label} :done, {task_id}, {_fmt_dt(start)}, {_fmt_dt(end)}")
            last_end = max(last_end, end) if last_end else end
            if session.retirement_reason:
                mid = _mermaid_id(f"{task_id}_retired")
                label = _mermaid_text(f"retired: {session.retirement_reason}")
                lines.append(f"    {label} :milestone, {mid}, {_fmt_dt(end)}, 0d")
        for e_index, escalation in enumerate(group.escalations, start=1):
            at = _parse_dt(escalation.get("created_at")) or last_end or run_start
            mid = _mermaid_id(f"{group.id}_escalation_{e_index}")
            label = _mermaid_text(f"escalation: {escalation.get('kind', 'escalation')}")
            lines.append(f"    {label} :milestone, {mid}, {_fmt_dt(at)}, 0d")
            last_end = max(last_end, at) if last_end else at
        state_at = last_end or run_start
        mid = _mermaid_id(f"{group.id}_state")
        label = _mermaid_text(f"state: {group.state}")
        lines.append(f"    {label} :milestone, {mid}, {_fmt_dt(state_at)}, 0d")
    return "\n".join(lines)


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
        "    classDef ok fill:#d1f5d3,stroke:#2f9e44;",
        "    classDef fail fill:#ffd6d6,stroke:#c92a2a;",
        "    classDef resolved fill:#fff3bf,stroke:#e8a400;",
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
        "    classDef added fill:#d1f5d3,stroke:#2f9e44;",
        "    classDef removed fill:#ffd6d6,stroke:#c92a2a;",
        "    classDef kept fill:#e7e7e7,stroke:#868e96;",
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


# -------------------------------------------------------------- how-to-use


def _dispatch_handler(cli_text: str, name: str) -> str | None:
    pattern = re.compile(rf'args\.command == "{re.escape(name)}":\s*\n\s*return (\w+)\(')
    match = pattern.search(cli_text)
    return match.group(1) if match else None


def _public_top_level_functions(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]


def _entry_points(facts: RunFacts, repo_root: Path) -> list[tuple[str, str]]:
    base_sha = facts.git_range.base_sha
    tip_sha = facts.git_range.tip_sha
    assert base_sha is not None and tip_sha is not None

    points: list[tuple[str, str]] = []
    changed_paths = {cf.path for cf in facts.changed_files}

    if "orchestrator/cli.py" in changed_paths:
        diff = _run_git(repo_root, "diff", "-U0", base_sha, tip_sha, "--", "orchestrator/cli.py")
        added_text = "\n".join(
            line[1:]
            for line in (diff or "").splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        tip_cli = _git_show(repo_root, tip_sha, "orchestrator/cli.py") or ""
        for match in _ADD_PARSER_RE.finditer(added_text):
            name = match.group(1)
            handler = _dispatch_handler(tip_cli, name)
            if handler:
                points.append((name, handler))

    for path in sorted(changed_paths):
        if not path.endswith(".py"):
            continue
        base_src = _git_show(repo_root, base_sha, path)
        tip_src = _git_show(repo_root, tip_sha, path)
        if base_src is None and tip_src is not None:
            for fn_name in _public_top_level_functions(tip_src):
                points.append((fn_name, fn_name))

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, symbol in points:
        if symbol in seen:
            continue
        seen.add(symbol)
        unique.append((label, symbol))
    return unique[:8]


def _codegraph_callees(repo_root: Path, symbol: str) -> list[dict] | None:
    try:
        result = subprocess.run(
            ["codegraph", "callees", symbol, "--json", "--limit", "20", "-p", str(repo_root)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data.get("callees") or []


def _sequence_for(repo_root: Path, label: str, symbol: str) -> str | None:
    depth1 = _codegraph_callees(repo_root, symbol)
    if not depth1:
        return None

    participants: dict[str, str] = {label: _mermaid_id(label)}
    edges: list[tuple[str, str]] = []

    def participant_id(name: str) -> str:
        if name not in participants:
            participants[name] = _mermaid_id(name)
        return participants[name]

    for callee in depth1[:6]:
        name = callee.get("name")
        if not name:
            continue
        participant_id(name)
        edges.append((label, name))
        for callee2 in (_codegraph_callees(repo_root, name) or [])[:4]:
            name2 = callee2.get("name")
            if not name2:
                continue
            participant_id(name2)
            edges.append((name, name2))

    if not edges:
        return None

    lines = ["sequenceDiagram"]
    for participant_name, participant_ident in participants.items():
        lines.append(f"    participant {participant_ident} as {participant_name}")
    for source, target in edges:
        lines.append(f"    {participants[source]}->>{participants[target]}: calls")
    return "\n".join(lines)


def howto_sequences(facts: RunFacts, repo_root: Path) -> tuple[list[str], str | None]:
    if shutil.which("codegraph") is None:
        return [], "codegraph is not on PATH; how-to-use sequences skipped"
    if not (repo_root / ".codegraph").is_dir():
        return [], ".codegraph/ index is missing; how-to-use sequences skipped"
    if not facts.git_range.available:
        return [], "no git range for this run; how-to-use sequences skipped"

    entry_points = _entry_points(facts, repo_root)
    if not entry_points:
        return [], "no new entry points found in this run's diff"

    diagrams = [
        diagram
        for label, symbol in entry_points
        if (diagram := _sequence_for(repo_root, label, symbol)) is not None
    ]
    if not diagrams:
        return [], "codegraph found no callees for this run's new entry points"
    return diagrams, None


# ------------------------------------------------------------------ bundle


def render_all(facts: RunFacts, repo_root: Path) -> Diagrams:
    sequences, note = howto_sequences(facts, repo_root)
    return Diagrams(
        timeline=timeline_gantt(facts),
        plan_outcome=plan_outcome_flowchart(facts),
        architecture_delta=architecture_delta(facts, repo_root),
        howto_sequences=sequences,
        howto_note=note,
    )
