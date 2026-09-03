"""Compiled base-context document: the shared prefix every worker session loads.

Compilation is byte-stable (plan U4): same repo state + plan + codegraph summary
→ identical bytes, because the document feeds prompt-cache-sensitive session
heads (fork-first decision, plan Key Technical Decisions). No timestamps, no
environment-dependent content.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.grouping.plan_sections import parse_plan_sections
from orchestrator.prompts import load_template

CONVENTION_FILES = ("CLAUDE.md", "AGENTS.md")
#: The heading under which the plan digest lands in ``base-context.md``. The
#: report reads it back (``report.html.base_context_plan_mode``) to state how
#: much of the plan the workers saw, so keep it in one place.
PLAN_DIGEST_HEADING = "## Plan digest ("
#: What the compiler wrote before c526237 (2026-08-28, U3): the whole plan
#: document, every unit's section included. Runs snapshotted before then —
#: the ``r20260828-220035`` fixture among them — still carry it.
LEGACY_PLAN_DOCUMENT_HEADING = "## Plan document ("


def compile_base_context(repo_root: Path, plan_path: Path, codegraph_summary: str) -> str:
    """Worker ground rules + repo conventions + codegraph architecture summary +
    the plan digest.

    The ground rules come first (plan U15) so every forked worker session reads
    its behavioural invariants once, from the cached prefix, before the
    plan-specific and repo-specific material that follows. The digest — preamble
    plus per-unit ``Summary:`` lines plus the implements/consumes registry —
    replaces the full plan text (plan U3/R22/R23): no worker context anywhere
    carries the full plan document, and the task-map block never survives into
    it either way.
    """
    sections = ["# Base context\n", load_template("worker_ground_rules").strip() + "\n"]

    for name in CONVENTION_FILES:
        path = repo_root / name
        if path.is_file():
            sections.append(f"## Repo conventions ({name})\n\n{path.read_text().strip()}\n")

    if codegraph_summary.strip():
        sections.append(f"## Codebase architecture (codegraph)\n\n{codegraph_summary.strip()}\n")

    digest = parse_plan_sections(plan_path.read_text()).digest
    sections.append(f"{PLAN_DIGEST_HEADING}{plan_path.name})\n\n{digest.strip()}\n")
    return "\n".join(sections)
