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
multi-context repos) and any obviously related docs under `docs/`. Read
`STRATEGY.md` at the repo root if it exists — anchor grill questions to its
target problem, approach, and tracks, and flag immediately if the topic
conflicts with a stated track or a "Not working on" entry (`/orchestrator-strategy`
produces this file; skip if absent).

**If a question can be answered by exploring the codebase, explore the codebase
instead of asking.** The user's time goes to decisions only they can make.

## Phase 2 — Grill relentlessly

Interview the user until you share an understanding of what they actually want.
Walk down each branch of the design tree, resolving dependencies between
decisions one by one.

Rules of engagement (question wording follows
`skills/orchestrator-plan/question-format.md` — handles, legend, stakes,
running decisions list, visual rule — read it once before the first round):

- **Name every decision before asking about it.** There are no IDs yet, so
  the handle is `Decision · <name>` (`Decision · retry policy`). Between
  rounds print the running one-line-per-decision list; later questions
  refer to entries by that name, never by "the earlier question".
- **Batch independent questions, explain the stakes.** Group questions that
  don't depend on each other's answer into a single `AskUserQuestion` call —
  up to 4 questions per call, the tool's own limit. Only serialize when a
  later question's options genuinely can't be framed until an earlier one is
  answered. Every question carries a short explanation of why it matters or
  what it trades off, not just a bare stem — this is a conversational mode
  and the extra sentence earns its keep. **Always give a recommended
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
- **Draw the mechanism when words drift.** When a question is about how
  something flows (a lifecycle, a request path, a failure cascade) and the
  two of you may hold different pictures, render a ≤ 6-node Mermaid
  mechanism diagram per the format doc's visual rule — one page per topic,
  republished as the discussion moves. Never the unit/group DAG.
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
`R1.`, `R2.`, … — **and a short tag** (1–3 kebab-case words, as stable as
the ID) that /orchestrator-plan will carry through to plan units: a unit
that mainly implements an R-ID reuses its tag as the unit slug, and every
question in every later phase cites `R2 · bundle-v2 · <gist>`, never `R2`.>

- R1. `flow-doc` — **Concise flow doc.** <one gist sentence, then detail>
- R2. `bundle-v2` — **Run Bundle contract v2.** ...

## Non-Goals

- ...

## Open Questions

<Only questions genuinely deferred to planning time — an empty section is the
goal.>

## Next Step

Run `/orchestrator-plan docs/brainstorms/YYYY-MM-DD-<topic>-requirements.md`.
```

**R-IDs and their tags are stable forever** — never renumber or rename on
edit; retire IDs by marking them, append new ones at the end.

## Phase 5 — Self-review, then the user gate

Before presenting, re-read the document end to end and fix inline:

- **Placeholders** — no TODO/TBD/`<fill in>` anywhere.
- **Contradictions** — decisions that conflict with each other or with a
  requirement.
- **Ambiguity** — any sentence two readers would implement differently.
- **Scope creep** — requirements nobody asked for and no decision justifies.

Then present the summary, the decisions, and the R-ID list (as
`R2 · bundle-v2 · gist` lines) to the user for review. **The document is not done until the user approves it.** End by pointing
at `/orchestrator-plan <path>`.

## Non-negotiable rules

- **No code, no plan.** The only files this skill may write are the requirements
  document and `CONTEXT.md`.
- Batch independent questions per `AskUserQuestion` call (max 4 per call);
  serialize only on genuine dependency. Each question carries a recommended
  answer and a short explanation, and follows
  `skills/orchestrator-plan/question-format.md`: named decisions, stakes in
  the stem, a legend before the call, no bare IDs.
- Explore instead of asking whenever the codebase can answer.
- Update `CONTEXT.md` inline as terms resolve; glossary only, never a spec.
