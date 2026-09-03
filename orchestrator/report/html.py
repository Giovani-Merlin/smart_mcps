"""One self-contained ``report.html`` (plan U4, reworked in report v2). Fills
``templates/report.html.j2`` from ``RunFacts``, the diagram sources in
``Diagrams``, and — when present — the run-driver session's ``one-pager.md``,
rendered from markdown to HTML here with each bullet's trailing pointer
turned into an anchor onto the matching group/unit/item card.
``StrictUndefined`` means a missing context key raises at render time
instead of silently rendering a blank.

Report v2.1 adds what a reader needs to judge a group without leaving the
page: the shared ``base-context.md`` every worker received (read from
``run_dir``), each group's spec and its units' plan sections (from facts),
and each merged group's diff, side by side (``gitview.group_diff`` +
``split_diff`` against ``repo_root``; the template escapes every cell). Both paths are optional keyword arguments so a synthetic
``RunFacts`` still renders with neither.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined
from markdown_it import MarkdownIt
from markupsafe import Markup

from orchestrator.grouping.base_context import LEGACY_PLAN_DOCUMENT_HEADING, PLAN_DIGEST_HEADING
from orchestrator.report.diagrams import Diagrams
from orchestrator.report.facts import GroupFacts, RunFacts, VerificationFacts, session_tokens
from orchestrator.report.gitview import DEFAULT_MAX_BYTES, GroupDiff, group_diff, split_diff
from orchestrator.report.onepager import strip_pointer_comment

_env = Environment(
    loader=PackageLoader("orchestrator.report", "templates"),
    autoescape=True,  # the template is .j2, which select_autoescape(["html"]) never matched
    undefined=StrictUndefined,
)
#: CommonMark plus GFM tables — CLAUDE.md and plan sections use pipe tables,
#: which plain CommonMark renders as one paragraph of pipes.
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

#: Same shape ``onepager.validate`` matches: a top-level bullet whose last
#: parenthesis holds the pointer. ``pointer`` excludes ``(``/``)`` so a
#: bullet may still use parentheses in its body. Anchored on the raw line —
#: an indented ``  - how: …`` continuation is body text, not a pointer bullet.
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
    """Top-level bullets only (``line.startswith("- ")`` on the raw line), the
    same rule ``onepager._bullets`` applies — so an indented continuation or
    ``  - how:`` sub-bullet is left exactly as written."""
    out: list[str] = []
    for line in markdown_text.splitlines():
        match = _POINTER_BULLET_RE.match(line) if line.startswith("- ") else None
        if match:
            pointer = match.group("pointer").strip().strip("`")
            href = targets.get(pointer)
            if href:
                line = f"{match.group('lead')}([{pointer}]({href}))"
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


# ------------------------------------------------- diffs, specs, context


def markdown_html(text: str) -> Markup:
    """A plan section, group spec, or ``base-context.md`` rendered as
    CommonMark with raw HTML disabled — so a ``<script>`` in a spec is text."""
    return Markup(_md.render(text))


def _group_diffs(facts: RunFacts, repo_root: Path) -> dict[str, GroupDiff]:
    return {
        group.id: group_diff(repo_root, group.merge_sha)
        for group in facts.groups
        if group.merge_sha
    }


_H2_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)


def base_context_plan_mode(text: str) -> str | None:
    """How much of the plan the workers saw, read off the file itself — the
    same heading ``grouping.base_context.compile_base_context`` writes — so
    the report never asserts more than the run did: ``"digest"`` for
    ``PLAN_DIGEST_HEADING`` (preamble + one summary line per unit + registry;
    runs from 2026-08-28 U3 on), ``"document"`` for the legacy
    ``LEGACY_PLAN_DOCUMENT_HEADING`` (the whole plan, every unit's section
    included), ``None`` when neither is present."""
    for heading, mode in (
        (PLAN_DIGEST_HEADING, "digest"),
        (LEGACY_PLAN_DOCUMENT_HEADING, "document"),
    ):
        if re.search(rf"^{re.escape(heading)}", text, re.MULTILINE):
            return mode
    return None


def _base_context(facts: RunFacts, run_dir: Path | None) -> dict | None:
    if run_dir is None or not facts.base_context_path:
        return None
    path = run_dir / facts.base_context_path
    if not path.is_file():
        return None
    text = path.read_text()
    return {
        "name": facts.base_context_path,
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "html": markdown_html(text),
        "plan_mode": base_context_plan_mode(text),
        "headings": _H2_RE.findall(text),
    }


# ------------------------------------------------------------------ render


def render_html(
    facts: RunFacts,
    diagrams: Diagrams,
    one_pager: str | None = None,
    *,
    repo_root: Path | None = None,
    run_dir: Path | None = None,
) -> str:
    """``repo_root`` enables the per-group diffs (git runs there); ``run_dir``
    enables the shared-context section (``base-context.md`` is read there).
    Either may be omitted, in which case that part of the page is absent."""
    template = _env.get_template("report.html.j2")
    group_diffs = _group_diffs(facts, repo_root) if repo_root is not None else {}
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
        diffs_enabled=repo_root is not None,
        group_diffs=group_diffs,
        diff_files={gid: split_diff(d.text) for gid, d in group_diffs.items()},
        diff_max_kb=DEFAULT_MAX_BYTES // 1000,
        base_context=_base_context(facts, run_dir),
        spec_html={group.id: markdown_html(group.spec) for group in facts.groups if group.spec},
        section_html={
            unit.unit_id: markdown_html(unit.section_text)
            for unit in facts.units
            if unit.section_text
        },
        short_sha=_short_sha,
    )
