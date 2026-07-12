---
name: plan-to-plan
description: Research planning — pins down the final objective, explores the codebase (codegraph-first) for what's already known, decomposes the remaining gaps into tagged external-knowledge questions (perplexity/notebooklm), and writes an approved research_plan.md (objective + resolved context + questions) ready for /apply-research-plan to execute.
user-invocable: true
argument-hint: "[your doubt, question, or topic to research]"
---

# plan-to-plan

You are in **research decomposition mode**. No code is written. No files are edited during the planning phase except `research_plan.md`. Only exploration and question structuring happen here.

Topic/doubt: `$ARGUMENTS`

## Notebook map

| Topic      | Notebook alias or ID  |
| ---------- | --------------------- |
| Your topic | `your-notebook-alias` |

______________________________________________________________________

## Phase 1 — Understand & scope (do this before generating questions)

Read the doubt in `$ARGUMENTS` carefully, then work through these in order — each step builds on the last, and skipping ahead produces vague questions that don't serve anything:

1. **Pin down the final objective — first, before anything else.** What does the user want to *accomplish* once these questions are answered — not the topic itself, but the outcome it's in service of. ("Research caching strategies" is a topic; "decide which caching strategy fits this service and walk away with enough grounding to write its implementation plan" is an objective.) Read this directly off `$ARGUMENTS` if it's explicit; otherwise infer your best reading from the surrounding ask and state it plainly. Ask the user only if you genuinely cannot tell what they're trying to accomplish. Everything downstream — which questions matter, how deep the answers need to go, what the final implementation plan looks like — gets measured against this, so get it right before moving on.
2. **Explore the codebase** to establish **what we already know** — prioritize `codegraph context` (see the codegraph skill: `skills/codegraph/SKILL.md`); fall back to `find`/`grep` only if it doesn't surface enough. Capture concrete discoveries: file paths, existing behaviors, prior decisions, established patterns — anything a research question would be wasteful to defer externally because the answer is already sitting in this repo.
3. **Identify what we don't know.** The gap between the objective (step 1) and what we already know (step 2) is exactly the set of candidate research questions — Phase 2 turns this gap into atomic, externally-answerable questions. If the gap turns out to be empty or tiny, say so plainly; not every doubt needs external research.
4. **Optionally** run 1–2 quick scoping queries (perplexity or notebooklm) to sharpen that gap — use these to refine question generation, not to answer the questions themselves.
5. Write a short **Scope block** (3–5 bullets): goal, in/out of scope, available sources, what is NOT safe to send externally (secrets, PII, proprietary logic).

## Phase 2 — Decompose into research questions

Generate **atomic research questions** from the gap identified in Phase 1 step 3 — each one answerable by a single external-knowledge query, and each one something "what we already know" (Phase 1 step 2) could *not* answer. Aim for 3–8 questions. Each question gets a stable ID `Q-001`, `Q-002`, …

Codebase doubts do not belong here: resolve them immediately during Phase 1 exploration (prioritize `codegraph`). A deferred question must be answerable without touching this repo's code — `/apply-research-plan` runs purely against external sources.

For each question, assign:

| Field        | Values                                                                                 |
| ------------ | -------------------------------------------------------------------------------------- |
| `SRC`        | `perplexity` / `notebooklm` / `perplexity+notebooklm`                                  |
| `CAT`        | `design_decision` / `implementation_detail` / `risk` / `requirement` / `open_question` |
| `P`          | `P1` (must answer) / `P2` (important) / `P3` (nice to have)                            |
| `BLOCKING`   | add tag if this must be answered before implementation starts                          |
| `DEPENDS_ON` | Q-IDs this depends on, if any                                                          |

**Routing rules:**

- Questions about industry practices, libraries, standards, comparisons → `perplexity`
- Questions related to an existing notebook → `notebooklm`
- Questions needing both web + existing notebook (broader and cross-concepts) → `perplexity+notebooklm`

## Phase 3 — Write the plan file

Determine a short slug from the topic (e.g., `rate-limiting`, `auth-flow`, `cache-strategy`). **This slug is the spine of the whole research effort** — `research_plan.md`, `research_answers.md`, and the eventual `implementation_plan.md` all live together in `research/<SLUG>/` and all declare `plan_id: RP-<SLUG>`. Choose it now and do not let it drift across phases — a mismatched folder is exactly what breaks the chain for `/apply-research-plan` downstream.

Write the plan to: `research/<SLUG>/research_plan.md`

Use this exact format:

```markdown
---
plan_id: RP-<SLUG>
version: 1
topic: "<original question from $ARGUMENTS>"
---

# Research Plan: <Title>

## Objective

<What the user wants to accomplish once these questions are answered — the destination this
whole effort points at (Phase 1 step 1). Not a restatement of the topic: the *outcome* it
serves. /apply-research-plan holds every answer, and the final implementation plan, up
against this — write it precisely enough that it can.>

## What we already know (resolved context)

<Concrete discoveries from Phase 1 step 2 — file paths, existing behaviors, prior decisions,
established patterns. This is the explicit "don't re-derive this" handoff to
/apply-research-plan: anything answerable by reading this repo belongs here, never deferred
as a question.>

- ...

## Scope

- **Goal:** ...
- **In scope:** ...
- **Out of scope:** ...
- **External-safe:** yes/no (and what is NOT safe to send out)

## Question Summary

| ID    | Summary | SRC        | P   | Blocking |
| ----- | ------- | ---------- | --- | -------- |
| Q-001 | ...     | perplexity | P1  | Yes      |

## Questions

### Q-001 [SRC:perplexity] [CAT:design_decision] [P1] [BLOCKING]

**Question**

...

**Expected output**

- ...

**Notes**

...
```

After writing the file, present the **Objective**, the **resolved-context bullets**, and the **question table** to the user in your plan output — these three are the things they're approving, not just the questions.

## Phase 4 — Exit plan mode for approval

Call `ExitPlanMode`. The user will review `research_plan.md` — may sharpen the objective, edit resolved context, add/remove/reprioritize questions — then approve.

When telling the user it's ready, be explicit about both the path *and* the slug — they need the exact slug to invoke the next phase, and every later artifact depends on it staying the same:

> Plan written to `research/<SLUG>/research_plan.md`. Once approved, run `/apply-research-plan <SLUG>` in a clean context — it will write its outputs alongside this file in `research/<SLUG>/`.

## Non-negotiable rules

- Write `research_plan.md` **before** calling `ExitPlanMode`.
- The plan must state an explicit **Objective** and a **What-we-already-know** block — these are load-bearing context for `/apply-research-plan`, not optional color. A plan with a vague objective produces answers (and an implementation plan) that drift from what the user actually wants.
- The chosen **slug is the spine of the chain**: `research_plan.md`, `research_answers.md`, and `implementation_plan.md` all live in `research/<SLUG>/` and share `plan_id: RP-<SLUG>`. Never let it drift between phases or write phases into different folders.
- Do not execute the plan's research questions during planning (phases 1–4) — the optional scoping queries in Phase 1 inform question generation; they do not answer Q-001, Q-002, ….
- Resolve codebase doubts immediately via `codegraph` during Phase 1 — never defer them as research questions (`/apply-research-plan` does not search code).
- Do not write code or edit files other than `research_plan.md`.
- Never send confidential code or secrets to `perplexity`/`notebooklm`.
- `research_plan.md` must declare a `plan_id` and stable question IDs — `/apply-research-plan` mirrors them in `research_answers.md`.

## Failure conditions

- Writing a `research_plan.md` with a missing or vague Objective, or no resolved-context block — `/apply-research-plan` then has nothing solid to anchor its answers (or the final implementation plan) to
- Letting the slug or folder drift — e.g. writing the plan into one `research/<SLUG>/` and leaving the door open for the answers or final plan to land somewhere else
- Answering the plan's research questions before it is approved
- Deferring a codebase doubt as an `SRC:perplexity`/`notebooklm` question instead of resolving it via `codegraph` in Phase 1
- Sending confidential local code to external tools
- Writing code or editing files other than `research_plan.md`
