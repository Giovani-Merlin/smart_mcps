"""Trial D's contract — a fixed scaffold the run-driver session fills in, and
a pure validator that checks the result without ever rewriting it (plan U5).

The one-pager is the single LLM-authored slot in the whole report: capped at
900 words, four fixed sections, and every top-level bullet must end in a
pointer drawn from ``RunFacts`` (a group id, a unit id, a verification item
id, an R-id, a changed file path, a short sha, an escalation id, or a session
label) so a claim can always be checked against the record it names.

Since report v2.1 a section may open with one plain paragraph of context
before its bullets, and a bullet may carry indented continuation lines
(``  `` prefix — plain text or ``  - `` sub-bullets). Neither needs a
pointer; both count toward the word cap. The pointer rule is for top-level
bullets only: a line that starts ``- `` in column 0.

Since report v2 the one-pager is also folded into ``report.html`` (Summary)
and is the body of the PR ``finish`` opens.
"""

from __future__ import annotations

import re

from orchestrator.report.facts import RunFacts

#: The four body sections, in the required order. The H1 title line is
#: checked separately (its text is free-form: "<plan_title> — <run_id>").
#: "Run notes" (report v2 U4) is the driver's narrative: hand fixes, causes
#: of escalations, what was recovered — sourced from the driver's own
#: ``.orchestrator/notes-<run_id>.md`` and the escalation record.
_SECTIONS = ("TL;DR", "Problems found", "Run notes", "Next steps")
#: Sections where a modal verb reads as a recommendation that belongs in
#: "Next steps" instead.
_NO_MODAL_SECTIONS = ("Problems found", "Run notes")
_TLDR_BULLETS = 3
_MIN_BULLETS = 1
_MAX_BULLETS = 8
_MAX_WORDS = 900

#: Vague summary filler that carries no evidence — the kind of phrase that
#: lets a one-pager sound complete without saying anything checkable.
_BANNED_PHRASES = (
    "overall",
    "in conclusion",
    "in summary",
    "it is worth noting",
    "it should be noted",
    "needless to say",
    "as you can see",
)

#: Banned inside "Problems found" and "Run notes": a modal verb there reads
#: as a recommendation ("this should be fixed"), which belongs in "Next steps".
_MODAL_VERBS = ("should", "would", "could", "might", "may", "must", "shall", "will")

_BULLET_RE = re.compile(r"^-\s+(?P<body>.*)\((?P<pointer>[^()]*)\)\s*$")
_HEADING_RE = re.compile(r"^(#{1,2})\s+(.*?)\s*$")
_POINTER_COMMENT_RE = re.compile(
    r"^[ \t]*<!--\s*valid pointers:.*?-->[ \t]*\n?", re.MULTILINE | re.DOTALL
)
_H1_LINE_RE = re.compile(r"^#\s+.*\n?", re.MULTILINE)
_ESCALATION_ID_CHARS = 12


def strip_pointer_comment(text: str) -> str:
    """The one-pager without the scaffold's ``<!-- valid pointers … -->``
    comment — what the HTML Summary and the PR body embed."""
    return _POINTER_COMMENT_RE.sub("", text).rstrip() + "\n"


def body_without_title(text: str) -> str:
    """``strip_pointer_comment`` plus the H1 dropped: the PR body's opening."""
    return _H1_LINE_RE.sub("", strip_pointer_comment(text), count=1).lstrip("\n")


def session_label(group_id: str, role: str, generation: int) -> str:
    """``<gid>/<role>/gen<n>`` — how a one-pager bullet cites one session."""
    return f"{group_id}/{role}/gen{generation}"


# --------------------------------------------------------------- pointers


def _valid_pointers(facts: RunFacts) -> set[str]:
    """Every string a one-pager bullet may point at: exactly the identifiers
    and paths ``RunFacts`` already carries — never re-derived from raw run
    JSON (plan U5)."""
    pointers: set[str] = set()
    pointers.update(cf.path for cf in facts.changed_files)
    for group in facts.groups:
        pointers.add(group.id)
        for escalation in group.escalations:
            eid = str(escalation.get("id") or "")
            if eid:
                pointers.add(eid[:_ESCALATION_ID_CHARS])
        for session in group.sessions:
            pointers.add(session_label(group.id, session.role, session.generation))
    for unit in facts.units:
        pointers.add(unit.unit_id)
        pointers.update(item.item_id for item in unit.verification)
    pointers.update(rid.rid for rid in facts.rids)
    if facts.git_range.base_sha:
        pointers.add(facts.git_range.base_sha[:8])
    if facts.git_range.tip_sha:
        pointers.add(facts.git_range.tip_sha[:8])
    return pointers


# --------------------------------------------------------------- scaffold


def scaffold(facts: RunFacts) -> str:
    """The fixed skeleton the run-driver session fills in: four sections,
    placeholder bullets, and an HTML comment enumerating every pointer that
    would validate. The placeholders point at the literal string
    ``POINTER``, which is never a member of the valid set, so an untouched
    scaffold always fails validation."""
    title = facts.plan_title or facts.run_id
    pointers = ", ".join(sorted(_valid_pointers(facts))) or "(none available for this run)"
    lines = [
        f"# {title} — {facts.run_id}",
        "",
        "## TL;DR",
        "",
        "one short paragraph of context, optional",
        "",
        "- one bullet naming the outcome (POINTER)",
        "- one bullet naming the cost or risk (POINTER)",
        "- one bullet naming what changed (POINTER)",
        "",
        "## Problems found",
        "",
        "one short paragraph of context, optional",
        "",
        "- one bullet per problem, each ending with the pointer that proves it (POINTER)",
        "  optional indented continuation lines: more detail, no pointer needed",
        "",
        "## Run notes",
        "",
        "one short paragraph of context, optional",
        "",
        "- one bullet per thing the driver did — a hand fix, an escalation's cause, "
        "what was recovered — each ending with the pointer it concerns (POINTER)",
        "",
        "## Next steps",
        "",
        "one short paragraph of context, optional",
        "",
        '- <action>: <why it matters and what "done" looks like> (POINTER)',
        "  - how: optional sub-bullet with the concrete first move",
        "",
        f"<!-- valid pointers: {pointers} -->",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- validate


def _headings(text: str) -> list[tuple[int, str]]:
    """``(level, title)`` for every ``#``/``##`` heading line, in document
    order. Anything deeper (``###``) is body text, not a section marker."""
    found: list[tuple[int, str]] = []
    for line in text.splitlines():
        if line.startswith("### "):
            continue
        match = _HEADING_RE.match(line)
        if match:
            found.append((len(match.group(1)), match.group(2)))
    return found


def _section_text(text: str, name: str) -> str:
    """The body between ``## <name>`` and the next ``##`` heading (or EOF).
    Looked up by name so a broken heading order still leaves every other
    rule checkable against whatever content exists under that name."""
    pattern = re.compile(
        rf"^##\s+{re.escape(name)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
    )
    match = pattern.search(text)
    return match.group("body") if match else ""


def _bullets(section_text: str) -> list[str]:
    """Top-level bullets only — a raw line starting ``- `` in column 0. An
    indented ``  - `` sub-bullet is a continuation of the bullet above it and
    is neither counted nor asked for a pointer."""
    return [line.rstrip() for line in section_text.splitlines() if line.startswith("- ")]


def _section_words(section_text: str) -> int:
    """Words in every non-heading, non-comment line of a section: a top-level
    bullet contributes its body (pointer excluded); a paragraph or indented
    continuation contributes all its words (a leading ``- `` marker dropped)."""
    total = 0
    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or stripped.startswith("#"):
            continue
        if line.startswith("- "):
            body, _pointer = _bullet_body_and_pointer(stripped)
            total += len(body.split())
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:]
        total += len(stripped.split())
    return total


def _bullet_body_and_pointer(bullet: str) -> tuple[str, str | None]:
    match = _BULLET_RE.match(bullet)
    if not match:
        return bullet[1:].strip(), None
    return match.group("body").strip(), match.group("pointer").strip().strip("`")


def validate(text: str, facts: RunFacts) -> list[str]:
    """Every rule the one-pager must satisfy, checked independently so one
    broken rule never masks another. Returns one violation string per broken
    rule instance; an empty list means the file is clean. Pure: reads
    ``text``, never touches a filesystem path (plan U5 constraint — the
    validator must never rewrite the one-pager)."""
    violations: list[str] = []

    headings = _headings(text)
    h1_titles = [title for level, title in headings if level == 1]
    h2_titles = [title for level, title in headings if level == 2]
    if len(h1_titles) != 1 or h2_titles != list(_SECTIONS):
        violations.append(
            "headings must be exactly one H1 title followed by "
            + ", ".join(f"## {name}" for name in _SECTIONS)
            + " in that order"
        )

    tldr_bullets = _bullets(_section_text(text, "TL;DR"))
    if len(tldr_bullets) != _TLDR_BULLETS:
        violations.append(
            f"TL;DR must have exactly {_TLDR_BULLETS} bullets, found {len(tldr_bullets)}"
        )

    section_bullets: dict[str, list[str]] = {"TL;DR": tldr_bullets}
    for name in _SECTIONS[1:]:
        bullets = _bullets(_section_text(text, name))
        section_bullets[name] = bullets
        if not (_MIN_BULLETS <= len(bullets) <= _MAX_BULLETS):
            violations.append(
                f"{name} must have {_MIN_BULLETS}-{_MAX_BULLETS} bullets, found {len(bullets)}"
            )

    valid_pointers = _valid_pointers(facts)
    for name in _SECTIONS:
        for bullet in section_bullets[name]:
            _body, pointer = _bullet_body_and_pointer(bullet)
            if pointer is None:
                violations.append(f"bullet missing a trailing (<pointer>): {bullet}")
            elif pointer not in valid_pointers:
                violations.append(f"unknown pointer {pointer!r} in bullet: {bullet}")

    word_total = sum(
        _section_words(_section_text(strip_pointer_comment(text), name)) for name in _SECTIONS
    )
    if word_total > _MAX_WORDS:
        violations.append(f"body exceeds {_MAX_WORDS} words (excluding pointers): {word_total}")

    haystack = text.lower()
    for phrase in _BANNED_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", haystack):
            violations.append(f"banned phrase: {phrase!r}")

    for name in _NO_MODAL_SECTIONS:
        section_lower = _section_text(text, name).lower()
        for verb in _MODAL_VERBS:
            if re.search(rf"\b{verb}\b", section_lower):
                violations.append(f"modal verb {verb!r} is not allowed in {name}")

    return violations
