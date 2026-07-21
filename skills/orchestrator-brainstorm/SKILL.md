---
name: orchestrator-brainstorm
description: Think-from-base grilling that turns a vague idea into a requirements document with stable R-IDs, ready for /orchestrator-plan. Use when the user wants to think through what to build before any plan exists — purpose, constraints, success criteria, scope boundaries, failure modes, non-goals.
user-invocable: true
argument-hint: "[the idea, problem, or topic to think through]"
---

# orchestrator-brainstorm

You are in **requirements mode**. No code is written, no plan is produced, and
**no subagents are spawned — everything happens inline in this session**. The
terminal state is a requirements document plus a pointer to `/orchestrator-plan`.

Topic: `$ARGUMENTS`

______________________________________________________________________

## Phase 1 — Explore before asking

Ground yourself in the project before the first question. Prioritize codegraph
(`context` → `explore` → `impact`; see `skills/codegraph/SKILL.md`) over
`find`/`grep`. Read `CONTEXT.md` if it exists (root, or via `CONTEXT-MAP.md` in
multi-context repos) and any obviously related docs under `docs/`.

**If a question can be answered by exploring the codebase, explore the codebase
instead of asking.** The user's time goes to decisions only they can make.

## Phase 2 — Grill relentlessly, one question at a time

Interview the user until you share an understanding of what they actually want.
Walk down each branch of the design tree, resolving dependencies between
decisions one by one.

Rules of engagement:

- **One question at a time**, via `AskUserQuestion`, **always with a recommended
  answer** — make the recommendation the first option, labeled
  `(Recommended)`. Multiple-choice preferred; free-form only when options would
  be fabricated.
- Cover, in whatever order the conversation demands: **purpose** (what outcome,
  for whom), **constraints** (technical, organizational, budget), **success
  criteria** (how we'll know it worked), **scope boundaries** (in/out),
  **failure modes** (what going wrong looks like), and **non-goals** (explicit
  no-s — as valuable as the yes-s).
- **Stress-test with concrete scenarios.** Invent specific situations that probe
  edge cases and force precision about boundaries between concepts.
- **Cross-reference claims against code.** When the user states how something
  works today, check whether the code agrees; surface contradictions
  immediately ("the code does X, but you just said Y — which is right?").

### Glossary discipline (CONTEXT.md)

Challenge fuzzy or conflicting terms against the existing glossary and update it
**inline, as terms resolve** — never batched at the end:

- If a term conflicts with `CONTEXT.md`, call it out immediately.

- If a term is vague or overloaded, propose a precise canonical term and record
  the losers under `_Avoid_`.

- Create `CONTEXT.md` lazily — only when the first term is resolved. Format:

  ```md
  # {Context Name}

  {One or two sentences on what this context is and why it exists.}

  ## Language

  **Term**:
  One or two sentences defining what it IS, not what it does.
  _Avoid_: rejected synonyms
  ```

- `CONTEXT.md` is a **glossary and nothing else** — opinionated terms specific
  to this project's domain. No implementation details, no specs, no scratch
  notes, no general programming concepts.

## Phase 3 — Propose approaches

Once the problem is pinned down, propose **2–3 approaches** with a clear
recommendation and the trade-offs that drive it. Present the emerging design in
sections (problem frame, key decisions, open risks) and iterate with the user
until one approach is chosen.

## Phase 4 — Write the requirements document

Write `docs/brainstorms/YYYY-MM-DD-<topic>-requirements.md`:

```markdown
---
date: YYYY-MM-DD
topic: <topic-slug>
---

# <Title> — Requirements

## Summary

<One paragraph: what is being built and why.>

## Problem Frame

<Why now; what fails without it; prior art in this repo.>

## Key Decisions

- **<Decision>.** <Rationale, alternatives rejected and why.>

## Requirements

<Grouped under subheadings when natural. Every requirement gets a stable ID —
`R1.`, `R2.`, … — that /orchestrator-plan will carry through to plan units.>

- R1. ...
- R2. ...

## Non-Goals

- ...

## Open Questions

<Only questions genuinely deferred to planning time — an empty section is the
goal.>

## Next Step

Run `/orchestrator-plan docs/brainstorms/YYYY-MM-DD-<topic>-requirements.md`.
```

**R-IDs are stable forever** — never renumber on edit; retire IDs by marking
them, append new ones at the end.

## Phase 5 — Self-review, then the user gate

Before presenting, re-read the document end to end and fix inline:

- **Placeholders** — no TODO/TBD/`<fill in>` anywhere.
- **Contradictions** — decisions that conflict with each other or with a
  requirement.
- **Ambiguity** — any sentence two readers would implement differently.
- **Scope creep** — requirements nobody asked for and no decision justifies.

Then present the summary, the decisions, and the R-ID list to the user for
review. **The document is not done until the user approves it.** End by pointing
at `/orchestrator-plan <path>`.

## Non-negotiable rules

- **No code, no plan.** The only files this skill may write are the requirements
  document and `CONTEXT.md`.
- One question at a time, each with a recommended answer.
- Explore instead of asking whenever the codebase can answer.
- Update `CONTEXT.md` inline as terms resolve; glossary only, never a spec.
