"""Markdown trials — a run changelog entry (trial A) and the PR body used as
the merge record (trial C), with a postmortem-lite section on either when the
run had trouble (plan U3).

Every renderer here is a pure text function over ``RunFacts`` (report U1) and
``Diagrams`` (report U2): no run JSON is read here, no transcript is opened,
no LLM is called. Markdown output stays asset-free on purpose — mermaid
blocks are plain ```mermaid fences that GitHub renders natively, never an
embedded browser tag or a CDN reference (those belong to the HTML trial only).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from orchestrator.report.diagrams import Diagrams
from orchestrator.report.facts import GroupFacts, RunFacts, TimelineEvent, UnitFacts

_RUN_ID_DATE_RE = re.compile(r"r(\d{4})(\d{2})(\d{2})-\d{6}")
_MAX_TIMELINE_EVENTS = 6


def _run_id_date(run_id: str) -> str:
    match = _RUN_ID_DATE_RE.match(run_id)
    if not match:
        return run_id
    year, month, day = match.groups()
    return f"{year}-{month}-{day}"


def _short(sha: str | None) -> str:
    return sha[:8] if sha else "unknown"


def _trim_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text.strip()
    return " ".join(words[:limit]) + "…"


def _bullet(label: str, body: str, pointer: str) -> str:
    """One ``- **label**: body (pointer)`` line — the one shape every bullet
    in these renderers takes, so the "every bullet ends with ``)``" rule
    (plan U3 Non-goals) holds by construction."""
    return f"- **{label}**: {body} ({pointer})"


def _group_tokens_by_model(group: GroupFacts) -> dict[str, int]:
    totals: dict[str, int] = {}
    for session in group.sessions:
        model = session.model or "unknown"
        totals[model] = totals.get(model, 0) + sum(session.tokens.values())
    return totals


def _tokens_by_model(groups: list[GroupFacts]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for group in groups:
        for model, count in _group_tokens_by_model(group).items():
            totals[model] = totals.get(model, 0) + count
    return totals


def _format_token_breakdown(totals: dict[str, int]) -> str:
    if not totals:
        return "no tokens recorded"
    return ", ".join(f"{model}={count}" for model, count in sorted(totals.items()))


def _elapsed_str(group: GroupFacts) -> str:
    starts = [s.started_at for s in group.sessions if s.started_at]
    ends = [s.ended_at for s in group.sessions if s.ended_at]
    if not starts or not ends:
        return "unknown"
    try:
        start = min(datetime.fromisoformat(s) for s in starts)
        end = max(datetime.fromisoformat(e) for e in ends)
    except ValueError:
        return "unknown"
    total_minutes = max(int((end - start).total_seconds() // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes}m" if hours else f"{minutes}m"


def _units_for_group(facts: RunFacts, group_id: str) -> list[UnitFacts]:
    return [unit for unit in facts.units if unit.group_id == group_id]


def _verification_counts(units: list[UnitFacts]) -> tuple[int, int, int]:
    total = passed = failed = 0
    for unit in units:
        for item in unit.verification:
            total += 1
            if item.status == "pass":
                passed += 1
            elif item.status == "fail":
                failed += 1
    return total, passed, failed


def _verification_table(units: list[UnitFacts]) -> str:
    rows = ["| item | status | evidence |", "| --- | --- | --- |"]
    for unit in units:
        for item in unit.verification:
            evidence = item.evidence.replace("\n", " ").replace("|", "/") or "—"
            rows.append(f"| {item.item_id} | {item.status} | {evidence} |")
    if len(rows) == 2:
        return ""
    return "\n".join(rows)


# --------------------------------------------------------------- fragments


def render_fragments(facts: RunFacts) -> dict[str, str]:
    """One markdown fragment per group: state, a trimmed summary, pass/fail
    counts, surprises, escalation actions, tokens, and elapsed time — the
    building block ``render_changelog_entry`` compiles under each group's
    heading (plan U3)."""
    fragments: dict[str, str] = {}
    for group in facts.groups:
        units = _units_for_group(facts, group.id)
        total, passed, failed = _verification_counts(units)
        lines = [f"### {group.id}: {group.name} — state: {group.state}"]

        summary_text = group.report_summary or group.summary or "(no summary recorded)"
        lines.append(_bullet("Summary", _trim_words(summary_text, 20), f"`{group.id}`"))

        verification_text = f"{passed}/{total} pass" + (f", {failed} fail" if failed else "")
        lines.append(_bullet("Verification", verification_text, f"`{group.id}`"))

        if group.verdict_status:
            lines.append(_bullet("Verdict", group.verdict_status, f"`{group.id}`"))

        if group.surprises:
            for surprise in group.surprises:
                lines.append(
                    _bullet(
                        f"Surprise ({surprise.get('kind', 'other')})",
                        surprise.get("description", ""),
                        f"`{surprise.get('path', group.id)}`",
                    )
                )
        else:
            lines.append(_bullet("Surprises", "none recorded", f"`{group.id}`"))

        if group.required_changes:
            for change, path in zip(group.required_changes, group.required_change_paths):
                lines.append(_bullet("Required change", change, f"`{path}`"))
        else:
            lines.append(_bullet("Required changes", "none", f"`{group.id}`"))

        if group.escalations:
            for escalation in group.escalations:
                action = escalation.get("action") or "no action recorded"
                lines.append(
                    _bullet(
                        f"Escalation ({escalation.get('kind', 'escalation')})",
                        action,
                        f"`{escalation.get('request_path', group.id)}`",
                    )
                )
        else:
            lines.append(_bullet("Escalations", "none", f"`{group.id}`"))

        tokens = _group_tokens_by_model(group)
        total_tokens = sum(tokens.values())
        lines.append(
            _bullet(
                "Tokens",
                f"{total_tokens} total across {len(group.sessions)} session(s) "
                f"({_format_token_breakdown(tokens)})",
                f"`{group.id}`",
            )
        )
        lines.append(_bullet("Elapsed", _elapsed_str(group), f"`{group.id}`"))

        table = _verification_table(units)
        if table:
            lines.append("")
            lines.append(table)

        fragments[group.id] = "\n".join(lines)
    return fragments


# ------------------------------------------------------------- postmortem


def _timeline_bullets(facts: RunFacts) -> list[str]:
    events: list[TimelineEvent] = facts.timeline[:_MAX_TIMELINE_EVENTS]
    bullets = []
    for event in events:
        pointer = f"`{event.group_id}`" if event.group_id else f"`{facts.run_id}`"
        bullets.append(_bullet(event.kind, f"{event.label} at {event.at}", pointer))
    return bullets


def _root_cause_bullets(facts: RunFacts) -> list[str]:
    bullets: list[str] = []
    for group in facts.groups:
        if group.failure:
            bullets.append(_bullet(f"Failure ({group.id})", group.failure, f"`{group.id}`"))
        for session in group.sessions:
            if session.retirement_reason:
                bullets.append(
                    _bullet(
                        f"Retirement ({group.id}, {session.role} gen{session.generation})",
                        session.retirement_reason,
                        f"`{group.id}`",
                    )
                )
        for change, path in zip(group.required_changes, group.required_change_paths):
            bullets.append(_bullet(f"Required change ({group.id})", change, f"`{path}`"))
        for surprise in group.surprises:
            bullets.append(
                _bullet(
                    f"Surprise ({group.id}, {surprise.get('kind', 'other')})",
                    surprise.get("description", ""),
                    f"`{surprise.get('path', group.id)}`",
                )
            )
    return bullets


def _followup_bullets(facts: RunFacts) -> list[str]:
    bullets: list[str] = []
    for group in facts.groups:
        for change, path in zip(group.required_changes, group.required_change_paths):
            bullets.append(_bullet(f"Open required change ({group.id})", change, f"`{path}`"))
    for unit in facts.units:
        for item in unit.verification:
            if item.status == "fail":
                bullets.append(
                    _bullet(f"Failing item ({unit.unit_id})", item.description, f"`{item.item_id}`")
                )
    if not bullets:
        bullets.append(_bullet("Follow-ups", "none open", f"`{facts.run_id}`"))
    return bullets


def _impact_bullets(facts: RunFacts) -> list[str]:
    bullets: list[str] = []
    for unit in facts.units:
        if not unit.landed:
            bullets.append(
                _bullet(f"Unit not landed ({unit.unit_id})", unit.title, f"`{unit.unit_id}`")
            )
    for rid in facts.rids:
        if not rid.landed:
            bullets.append(_bullet(f"Requirement not met ({rid.rid})", "unmet", f"`{rid.rid}`"))
    if not bullets:
        bullets.append(
            _bullet("Impact", "every unit landed despite the trouble below", f"`{facts.run_id}`")
        )
    return bullets


def _render_postmortem(facts: RunFacts) -> str:
    lines = ["## Postmortem", "", "### Impact", ""]
    lines.extend(_impact_bullets(facts))
    lines.extend(["", "### Timeline", ""])
    timeline_bullets = _timeline_bullets(facts)
    lines.extend(
        timeline_bullets
        or [_bullet("Timeline", "no timestamped events recorded", f"`{facts.run_id}`")]
    )
    lines.extend(["", "### Root-cause candidates", ""])
    lines.extend(_root_cause_bullets(facts))
    lines.extend(["", "### Follow-ups", ""])
    lines.extend(_followup_bullets(facts))
    return "\n".join(lines)


# ------------------------------------------------------------ changelog


def render_changelog_entry(facts: RunFacts, diagrams: Diagrams) -> str:
    """The full changelog entry (trial A): a TL;DR, one block per group, the
    timeline and plan-outcome diagrams, the ADR delta, and — only when
    ``facts.trouble`` — a postmortem-lite section (plan U3)."""
    date = _run_id_date(facts.run_id)
    lines = [f"## {date} — {facts.run_id} — {facts.plan_title or facts.run_id}", ""]

    completed = sum(1 for g in facts.groups if g.state in ("completed", "resolved"))
    units_landed = sum(1 for u in facts.units if u.landed)
    lines.append(
        _bullet(
            "Outcome",
            f"{completed}/{len(facts.groups)} groups completed, "
            f"{units_landed}/{len(facts.units)} units landed",
            "`state.json`",
        )
    )
    added = sum(cf.added for cf in facts.changed_files)
    deleted = sum(cf.deleted for cf in facts.changed_files)
    lines.append(
        _bullet(
            "Scope",
            f"{len(facts.changed_files)} files changed, +{added}/-{deleted} lines",
            f"`{_short(facts.git_range.base_sha)}..{_short(facts.git_range.tip_sha)}`",
        )
    )
    tokens = _tokens_by_model(facts.groups)
    n_sessions = sum(len(g.sessions) for g in facts.groups)
    lines.append(
        _bullet(
            "Cost",
            f"{sum(tokens.values())} tokens across {n_sessions} session(s) "
            f"({_format_token_breakdown(tokens)})",
            "`manifest.json`",
        )
    )
    lines.append("")

    fragments = render_fragments(facts)
    for group in facts.groups:
        lines.append(fragments[group.id])
        lines.append("")

    lines.append("## Diagrams")
    lines.append("")
    lines.append("### Run timeline")
    lines.append("")
    lines.append("```mermaid")
    lines.append(diagrams.timeline)
    lines.append("```")
    lines.append("")
    lines.append("### Plan → outcome")
    lines.append("")
    lines.append("```mermaid")
    lines.append(diagrams.plan_outcome)
    lines.append("```")
    lines.append("")

    lines.append("## ADR delta")
    lines.append("")
    if facts.adr_delta:
        for delta in facts.adr_delta:
            lines.append(_bullet(delta.change, delta.path, f"`{delta.path}`"))
    else:
        lines.append(
            _bullet(
                "ADR delta",
                "no ADR changes",
                f"`{_short(facts.git_range.base_sha)}..{_short(facts.git_range.tip_sha)}`",
            )
        )
    lines.append("")

    if facts.trouble:
        lines.append(_render_postmortem(facts))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------- RUNLOG.md


def _runlog_markers(run_id: str) -> tuple[str, str]:
    return f"<!-- run:{run_id} -->", f"<!-- /run:{run_id} -->"


def update_runlog(runlog_path: Path, run_id: str, entry: str) -> str:
    """Replace the text between this run's own ``<!-- run:<id> -->`` /
    ``<!-- /run:<id> -->`` markers with ``entry``, or append a new marked
    block when they are absent. Idempotent per run; every other run's marked
    block is left byte-identical (plan U3 Non-goals)."""
    start_marker, end_marker = _runlog_markers(run_id)
    block = f"{start_marker}\n{entry.rstrip()}\n{end_marker}\n"

    existing = runlog_path.read_text() if runlog_path.is_file() else "# Run log\n\n"
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker) + r"\n?", re.DOTALL
    )
    if pattern.search(existing):
        return pattern.sub(block, existing)
    if not existing.endswith("\n"):
        existing += "\n"
    return existing + "\n" + block


# ---------------------------------------------------------------- PR body


def render_pr_body(facts: RunFacts) -> str:
    """The PR body as the merge record (trial C): fixed headings
    ``Motivation``/``Changes``/``Risks``/``Testing``/``Handoff``, plus a
    ``Postmortem`` section when ``facts.trouble`` (plan U3). ``finish``
    passes this straight to ``gh pr create --body-file -``."""
    lines = [f"Orchestrator run `{facts.run_id}` — {facts.plan_title or facts.run_id}", ""]

    lines.append("## Motivation")
    lines.append("")
    objective = facts.plan_objective.split("\n\n")[0].strip() if facts.plan_objective else ""
    if objective:
        lines.append(objective)
    if facts.rids:
        rid_list = ", ".join(rid.rid for rid in facts.rids)
        lines.append("")
        lines.append(_bullet("Requirements", rid_list, f"`{facts.plan_path or facts.run_id}`"))
    lines.append("")

    lines.append("## Changes")
    lines.append("")
    if facts.units and len(facts.units) <= 7:
        for unit in facts.units:
            gid = unit.group_id or "unassigned"
            lines.append(f"- [{gid}] {unit.unit_id}: {unit.title} (`{unit.unit_id}`)")
    else:
        for group in facts.groups:
            lines.append(f"- [{group.id}] {group.name} (`{group.id}`)")
    lines.append("")

    lines.append("## Risks")
    lines.append("")
    risk_bullets: list[str] = []
    for group in facts.groups:
        for surprise in group.surprises:
            risk_bullets.append(
                _bullet(
                    f"Surprise ({group.id})",
                    surprise.get("description", ""),
                    f"`{surprise.get('path', group.id)}`",
                )
            )
        for change, path in zip(group.required_changes, group.required_change_paths):
            risk_bullets.append(_bullet(f"Required change ({group.id})", change, f"`{path}`"))
        if group.state not in ("completed", "resolved"):
            risk_bullets.append(_bullet("Unmerged group", group.id, f"`{group.id}`"))
    lines.extend(risk_bullets or [_bullet("Risks", "none recorded", f"`{facts.run_id}`")])
    lines.append("")

    lines.append("## Testing")
    lines.append("")
    for group in facts.groups:
        if group.tests.ran:
            lines.append(
                _bullet(
                    group.id,
                    f"{group.tests.total} test(s), {group.tests.failures} failure(s), "
                    f"{group.tests.errors} error(s)",
                    f"`{group.tests.junit_path}`",
                )
            )
        else:
            lines.append(_bullet(group.id, "no tests ran", f"`{group.id}`"))
    lines.append("")

    lines.append("## Handoff")
    lines.append("")
    lines.append(_bullet("Report", "the full run record", f"`docs/runs/{facts.run_id}/`"))
    lines.append("")

    if facts.trouble:
        lines.append(_render_postmortem(facts))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
