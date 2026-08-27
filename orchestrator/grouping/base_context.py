"""Compiled base-context document: the shared prefix every worker session loads.

Compilation is byte-stable (plan U4): same repo state + plan + codegraph summary
→ identical bytes, because the document feeds prompt-cache-sensitive session
heads (fork-first decision, plan Key Technical Decisions). No timestamps, no
environment-dependent content.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.grouping.plan_reader import strip_task_map
from orchestrator.prompts import load_template

CONVENTION_FILES = ("CLAUDE.md", "AGENTS.md")


def compile_base_context(repo_root: Path, plan_path: Path, codegraph_summary: str) -> str:
    """Worker ground rules + repo conventions + codegraph architecture summary +
    the plan document.

    The ground rules come first (plan U15) so every forked worker session reads
    its behavioural invariants once, from the cached prefix, before the
    plan-specific and repo-specific material that follows. The task-map block is
    grouper parser input, not worker context (R27): it is stripped from the plan
    text before embedding, never from the plan file itself.
    """
    sections = ["# Base context\n", load_template("worker_ground_rules").strip() + "\n"]

    for name in CONVENTION_FILES:
        path = repo_root / name
        if path.is_file():
            sections.append(f"## Repo conventions ({name})\n\n{path.read_text().strip()}\n")

    if codegraph_summary.strip():
        sections.append(f"## Codebase architecture (codegraph)\n\n{codegraph_summary.strip()}\n")

    plan_text = strip_task_map(plan_path.read_text())
    sections.append(f"## Plan document ({plan_path.name})\n\n{plan_text.strip()}\n")
    return "\n".join(sections)
