"""Prompt assembly for worker sessions: identity block, coder, reviewer, handoff.

The identity block opens every worker's FIRST prompt so the analyzer can join the
session to the run manifest from prompt content alone: the first genuine goal
prompt becomes the session title, the summarizer's goal, and the graph node text
(docs/research/infinity-skills-analysis.md §6 recs 1 and 7). The tag names must
not resemble injected-prefix patterns (``<command-``, ``<system-reminder>``, …) or
the prompt is silently dropped as harness noise instead of kept as the goal.
"""

from __future__ import annotations

from string import Template

from orchestrator.model import Group, VerificationItem
from orchestrator.prompts import load_template


def _attr(value: str) -> str:
    """Escape a value for use inside a double-quoted XML-ish attribute."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_identity(run_id: str, group: Group) -> str:
    return Template(load_template("identity")).substitute(
        run_id=_attr(run_id),
        group_id=_attr(group.id),
        group_name=_attr(group.name),
        summary=group.summary,
        spec=group.spec,
    )


def _verification_lines(items: list[VerificationItem]) -> str:
    if not items:
        return "- none specified; verify against the spec itself"
    return "\n".join(
        f"- [{item.id}] {item.description}" + ("" if item.required else " (optional)")
        for item in items
    )


def render_coder_prompt(run_id: str, group: Group) -> str:
    return Template(load_template("coder")).substitute(
        identity_block=render_identity(run_id, group),
        group_name=group.name,
        verification=_verification_lines(group.verification),
        report_contract=load_template("report_contract"),
    )


def render_reviewer_prompt(run_id: str, group: Group, *, report_path: str, base_ref: str) -> str:
    return Template(load_template("reviewer")).substitute(
        identity_block=render_identity(run_id, group),
        group_name=group.name,
        verification=_verification_lines(group.verification),
        report_path=report_path,
        base_ref=base_ref,
    )


def render_revision_prompt(verdict_path: str, required_changes: list[str]) -> str:
    """Resume trigger for a changes_required verdict — a pointer, not the payload."""
    changes = "\n".join(f"- {change}" for change in required_changes) or "- see the verdict file"
    return Template(load_template("revision")).substitute(
        verdict_path=verdict_path, required_changes=changes
    )


def render_reentry_prompt(group: Group) -> str:
    """Resume trigger for re-entering an interrupted coder session warm (R4/R6):
    the session already holds its identity and spec — this only re-orients it."""
    return Template(load_template("reentry")).substitute(group_name=group.name)


def render_conflict_resolve_prompt(
    group: Group, *, conflict_summary: str, integration_branch: str
) -> str:
    """Resume trigger for a merge conflict (plan U1): warm-resumes the coder that
    just built the work to resolve it in place, before falling back to a full
    spec rewrite. Re-orientation only, like ``render_reentry_prompt`` — the
    session already holds its identity and spec."""
    return Template(load_template("conflict_resolve")).substitute(
        group_name=group.name,
        conflict_summary=conflict_summary,
        integration_branch=integration_branch,
    )


def render_coder_answer_prompt(answer: str) -> str:
    """Resume trigger feeding an operator's answer back to a coder that ended its
    turn with ``needs_input`` (plan Phase D)."""
    return Template(load_template("answer")).substitute(answer=answer)


def render_re_review_prompt(report_path: str) -> str:
    return Template(load_template("re_review")).substitute(report_path=report_path)


def render_extra_pass_prompt() -> str:
    return load_template("extra_pass")


def render_ladder_summary_prompt() -> str:
    """70% checkpoint (plan U3): a quick summary while the round keeps going."""
    return load_template("ladder_summary")


def render_ladder_prioritized_prompt() -> str:
    """90% checkpoint (plan U3): prioritized conclusions, still mid-round."""
    return load_template("ladder_prioritized")


def render_ladder_compact_prompt() -> str:
    """100% checkpoint (plan U3): stop and report now. Cheap because it can
    reference the 70%/90% checkpoints already sitting in the same round."""
    return load_template("ladder_compact")


def render_handoff_prompt(
    run_id: str,
    group: Group,
    *,
    generation: int,
    retirement_reason: str,
    last_report: str,
    outstanding: str,
    diff_summary: str,
) -> str:
    """First prompt of a generation-respawn coder session (plan U7 breaker path)."""
    return Template(load_template("handoff")).substitute(
        identity_block=render_identity(run_id, group),
        group_name=group.name,
        generation=str(generation),
        retirement_reason=retirement_reason,
        last_report=last_report or "(no report survived the retired session)",
        outstanding=outstanding or "- none recorded",
        diff_summary=diff_summary or "(not summarized; inspect the worktree with git)",
        verification=_verification_lines(group.verification),
        report_contract=load_template("report_contract"),
    )
