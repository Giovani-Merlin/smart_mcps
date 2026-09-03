"""One self-contained ``report.html`` (plan U4, reworked in report v2). Fills
``templates/report.html.j2`` from ``RunFacts``, the diagram sources in
``Diagrams``, and — when present — the run-driver session's ``one-pager.md``,
rendered from markdown to HTML here with each bullet's trailing pointer
turned into an anchor onto the matching group/unit/item card.
``StrictUndefined`` means a missing context key raises at render time
instead of silently rendering a blank.
"""

from __future__ import annotations

import re

from jinja2 import Environment, PackageLoader, StrictUndefined
from markdown_it import MarkdownIt
from markupsafe import Markup

from orchestrator.report.diagrams import Diagrams
from orchestrator.report.facts import GroupFacts, RunFacts, VerificationFacts, session_tokens
from orchestrator.report.onepager import strip_pointer_comment

_env = Environment(
    loader=PackageLoader("orchestrator.report", "templates"),
    autoescape=True,  # the template is .j2, which select_autoescape(["html"]) never matched
    undefined=StrictUndefined,
)
_md = MarkdownIt("commonmark", {"html": False})

#: Same shape ``onepager.validate`` matches: a bullet whose last parenthesis
#: holds the pointer. ``pointer`` excludes ``(``/``)`` so a bullet may still
#: use parentheses in its body.
_POINTER_BULLET_RE = re.compile(r"^(?P<lead>-\s+.*)\((?P<pointer>[^()]*)\)\s*$")
_ESCALATION_PROMPT_CHARS = 200


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
    return sum(session_tokens(session) for session in group.sessions)


def _group_cache_read(group: GroupFacts) -> int:
    return sum(session.cache_read_tokens for session in group.sessions)


# ---------------------------------------------------------------- anchors


def anchor_targets(facts: RunFacts) -> dict[str, str]:
    """``{pointer: "#anchor"}`` for every one-pager pointer that resolves to
    an element the template renders with that id: group cards, unit lines,
    verification items, requirement rows. File paths and shas have no card,
    so they stay plain text."""
    targets: dict[str, str] = {}
    for group in facts.groups:
        targets[group.id] = f"#group-{group.id}"
        for escalation in group.escalations:
            eid = str(escalation.get("id") or "")
            if eid:
                targets[eid[:12]] = f"#escalation-{eid[:12]}"
        for session in group.sessions:
            targets[f"{group.id}/{session.role}/gen{session.generation}"] = f"#group-{group.id}"
    for unit in facts.units:
        targets[unit.unit_id] = f"#unit-{unit.unit_id}"
        for item in unit.verification:
            targets[item.item_id] = f"#item-{item.item_id}"
    for rid in facts.rids:
        targets[rid.rid] = f"#rid-{rid.rid}"
    return targets


def _link_pointers(markdown_text: str, targets: dict[str, str]) -> str:
    out: list[str] = []
    for line in markdown_text.splitlines():
        match = _POINTER_BULLET_RE.match(line.strip())
        if match:
            pointer = match.group("pointer").strip().strip("`")
            href = targets.get(pointer)
            if href:
                indent = line[: len(line) - len(line.lstrip())]
                line = f"{indent}{match.group('lead')}([{pointer}]({href}))"
        out.append(line)
    return "\n".join(out) + ("\n" if markdown_text.endswith("\n") else "")


def render_one_pager_html(one_pager: str, facts: RunFacts) -> Markup:
    """The one-pager as HTML: the scaffold's valid-pointers comment stripped,
    each bullet's trailing pointer linked to its card when one exists, then a
    plain CommonMark render. Returned as ``Markup`` so the template embeds it
    unescaped; every fragment of it came through markdown-it's own escaping."""
    text = _link_pointers(strip_pointer_comment(one_pager), anchor_targets(facts))
    return Markup(_md.render(text))


# ------------------------------------------------------------ escalations


def escalation_rows(facts: RunFacts) -> list[dict]:
    rows: list[dict] = []
    for group in facts.groups:
        for escalation in group.escalations:
            eid = str(escalation.get("id") or "")
            prompt = (escalation.get("prompt") or "").strip()
            if len(prompt) > _ESCALATION_PROMPT_CHARS:
                prompt = prompt[:_ESCALATION_PROMPT_CHARS].rstrip() + "…"
            rows.append(
                {
                    "id": eid[:12],
                    "kind": escalation.get("kind") or "",
                    "group_id": group.id,
                    "generation": escalation.get("generation"),
                    "created_at": escalation.get("created_at") or "",
                    "prompt": prompt,
                    "action": escalation.get("action") or "",
                    "answer": (escalation.get("answer") or "").strip(),
                }
            )
    return rows


# ------------------------------------------------------------------ render


def render_html(facts: RunFacts, diagrams: Diagrams, one_pager: str | None = None) -> str:
    template = _env.get_template("report.html.j2")
    return template.render(
        facts=facts,
        diagrams=diagrams,
        one_pager_html=render_one_pager_html(one_pager, facts) if one_pager else None,
        escalations=escalation_rows(facts),
        trouble=facts.trouble,
        base_short=_short_sha(facts.git_range.base_sha),
        tip_short=_short_sha(facts.git_range.tip_sha),
        evidence_rows=_evidence_rows(facts),
        group_tokens={group.id: _group_tokens(group) for group in facts.groups},
        group_cache_read={group.id: _group_cache_read(group) for group in facts.groups},
        timeline_html=Markup(diagrams.timeline),
    )
