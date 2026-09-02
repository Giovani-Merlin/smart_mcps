"""Trial B: one self-contained ``report.html`` (plan U4). Fills
``templates/report.html.j2`` from ``RunFacts`` and the mermaid sources in
``Diagrams`` — no other computation happens here beyond assembling the rows
the template iterates over. ``StrictUndefined`` means a missing context key
raises at render time instead of silently rendering a blank.
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from orchestrator.report.diagrams import Diagrams
from orchestrator.report.facts import GroupFacts, RunFacts, VerificationFacts

_env = Environment(
    loader=PackageLoader("orchestrator.report", "templates"),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
)


def _short_sha(sha: str | None) -> str:
    return sha[:8] if sha else ""


def _group_verification(facts: RunFacts, group_id: str) -> list[VerificationFacts]:
    items: list[VerificationFacts] = []
    for unit in facts.units:
        if unit.group_id == group_id:
            items.extend(unit.verification)
    return items


def _evidence_rows(facts: RunFacts) -> list[dict]:
    return [
        {"group": group, "verification": _group_verification(facts, group.id)}
        for group in facts.groups
    ]


def _group_tokens(group: GroupFacts) -> int:
    total = 0
    for session in group.sessions:
        total += sum(session.tokens.values())
    return total


def render_html(facts: RunFacts, diagrams: Diagrams, one_pager: str | None = None) -> str:
    template = _env.get_template("report.html.j2")
    evidence_rows = _evidence_rows(facts)
    return template.render(
        facts=facts,
        diagrams=diagrams,
        one_pager=one_pager,
        trouble=facts.trouble,
        base_short=_short_sha(facts.git_range.base_sha),
        tip_short=_short_sha(facts.git_range.tip_sha),
        evidence_rows=evidence_rows,
        group_tokens={group.id: _group_tokens(group) for group in facts.groups},
    )
