---
name: orchestrator-deepen
description: Grills the human with codegraph-grounded, EVPI-ranked edge-case questions per group of an orchestrator plan, and writes the answers back into the plan as per-unit edge cases, non-goals, and Run:/Pass: verification items — without ever touching the task map or unit ids.
user-invocable: true
argument-hint: "<path to a plan document under docs/plans/>"
---

# orchestrator-deepen

You are in **enrichment mode**. No implementation code is written, and the
plan's **task map and unit ids are never touched** — this skill only adds new
optional bullets (`Edge cases`, `Non-goals / must-not`, and `Run:`/`Pass:`
verification lines) inside existing unit sections, through the shared
verbatim-surgery module (`orchestrator/grouping/plan_edit.py`, plan U1).
Every write is followed by `plan-check --against` the pre-edit copy; a
refusal aborts the write immediately.

Plan: `$ARGUMENTS`

______________________________________________________________________

## Phase 1 — Load the plan and its groups

- Read the plan document and, if it exists, its
  `.orchestrator/groupings/<name>/preview/advisory.json` (produced by
  `group --advise`) — this skill does not compute grouping itself.

- Derive group membership deterministically, with **no new code**: prefer an
  existing `.orchestrator/groupings/<name>/groups.json` if one is fresh
  against the plan; otherwise run

  ```sh
  smart-mcps-orchestrate group <plan> --no-spec
  ```

  and read the printed group membership from its report. Either source is
  zero-LLM and sub-second.

- Snapshot the plan's current bytes before any write — this is the "pre-edit
  copy" every `plan-check --against` call in Phase 4 compares against.

## Phase 2 — Explore, one subagent per group

For **every group**, spawn one read-only explorer subagent using the template
in [`explorer-prompt.md`](./explorer-prompt.md), filling in the plan path,
the group id, and that group's member unit sections verbatim. The explorer:

- walks all ten edge-case categories internally (boundary/range,
  empty/null/missing, error/partial-failure, concurrency/ordering,
  idempotency/retries, duplication/uniqueness, authz/security, performance
  budget, data invariants, contract compat/versioning) — for coverage, not for
  reporting;
- reports only the categories where the **divergence test** passes: two
  plausible readings of the unit's `Goal`/`Verification` that would produce
  genuinely different code;
- scores each candidate `blocking_risk × effect_size`;
- drafts a `Run:` command only when it confirmed the runner idiom is one this
  repo actually uses **and** every path in the command appears in that unit's
  declared `Files` — otherwise the candidate carries a `Pass:` condition only.

Groups with no cross-group dependency between them may be explored in
parallel; a downstream group's explorer does not need an upstream group's
answers, since edge cases and verification are recorded per-unit, not
per-dependency.

## Phase 3 — Grill, per group, capped and always with candidates

**The per-group cap dominates any plan-global figure**: every group gets
**3–5 questions**, ranked by the explorer's `blocking_risk × effect_size`
score, taking the top of the range — never fewer than 3 for a group with that
many fired candidates, never more than 5. A large plan with many groups will
therefore exceed a plan-wide total in the tens; that is intended; no
low-scoring group loses its questions to a cross-group ranking.

For each question:

- Ask via `AskUserQuestion`, **one group's batch at a time** (up to the tool's
  own 4-per-call limit, so a group's 5 questions span two calls).
- **Candidate answers are always offered** — never a bare free-form prompt.
  Include an explicit "either is fine" option whenever the explorer's two
  readings are genuinely both acceptable; recording that answer frees the
  constraint rather than forcing an arbitrary pick.
- Frame the question in plain language — the human answering may not have
  read the explorer's report.

## Phase 4 — Write the answers back, through plan_edit

For every answered question, using `orchestrator/grouping/plan_edit.py`'s
extraction/reassembly primitives (never hand-editing plan text with a text
tool):

- An accepted reading becomes exactly one of: a sharpened Goal line (EARS
  style — *"When `<trigger>`, `<unit>` shall `<response>`"*), an `Edge cases`
  entry, a `Non-goals / must-not` entry, or a verification item. Never loose
  prose appended anywhere.

- **Edge cases are written only where they fire** — a unit with zero
  triggered categories gets no `Edge cases` bullet at all; never write an
  `N/A — <why>` filler line. The taxonomy coverage already happened inside
  the explorer; the plan doesn't need to show its work.

- A verification item follows the `Run:` + `Pass:` convention: two lines
  inside one `- ` bullet, `Run:` present only when the explorer grounded it,
  `Pass:` always present. This is a convention inside the existing
  `Verification` bullet text — it changes no schema; `VerificationItem`, the
  assembler, and the prompt contract are untouched (a `Run:`/`Pass:` bullet
  still assembles into exactly one `VerificationItem`, whose `description`
  carries both lines).

- "Either is fine" answers are recorded as a **freed constraint** — a
  `Non-goals / must-not` entry naming the axis and stating either reading is
  acceptable, so a future reader doesn't reopen the question.

- After writing, run

  ```sh
  smart-mcps-orchestrate plan-check <plan> --against <pre-edit-copy>
  ```

  A **refusal aborts the write** — fix the write and re-check before moving
  on; never proceed with an unreported divergence in the task map or unit ids.

- Stamp each deepened unit with the plan-content hash it was derived from (a
  short comment or frontmatter-style marker naming the hash — this lets a
  later run tell whether the plan changed underneath an existing enrichment).
  Never touch the YAML task map or any unit id.

## Phase 5 — Hand off

This skill is **optional for every run** — a plan that was never deepened is
still a valid `group`/`run` input, just without the extra edge-case and
`Run:`/`Pass:` coverage. Present the human with a short summary (questions
asked, "either is fine" answers, units enriched) and point back at
`smart-mcps-orchestrate group <plan> --no-spec` to confirm the enriched plan
still parses clean.

## Non-negotiable rules

- **The task map and unit ids are never rewritten** — every write goes
  through `plan_edit.py` and is checked with `plan-check --against` the
  pre-edit copy; a refusal aborts.
- **3–5 questions per group, always with candidate answers**, dominant over
  any plan-global cap.
- **A `Run:` command is written only when grounded** (real runner idiom, every
  path in the unit's declared `Files`); otherwise `Pass:`-only.
- **Edge cases only where they fire** — no `N/A` filler.
- No implementation code. No LLM call regenerates any unit's existing prose —
  every new bullet is a direct write of an explicit human answer.
