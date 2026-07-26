---
name: orchestrator-plan
description: Produce an orchestrator-ready plan with an embedded task map — grounded in codegraph, grilled with the user, verified by a subagent — so `smart-mcps-orchestrate group` runs deterministically with the mapper LLM skipped. Input is a brainstorm requirements doc (from /orchestrator-brainstorm) or a direct feature description.
user-invocable: true
argument-hint: "[brainstorm doc path OR direct description of what to plan]"
---

# orchestrator-plan

You are in **planning mode**. No implementation code is written. The deliverable
is a plan document under `docs/plans/` whose `## Task Map` block the
orchestrator's parser (`orchestrator/grouping/plan_reader.py`) consumes 1:1 —
the format contract is **`docs/orchestrator-task-map.md`**; read it before
writing the map.

Input: `$ARGUMENTS`

______________________________________________________________________

## Phase 1 — Origin

- If `$ARGUMENTS` is (or names) a brainstorm requirements doc, read it and
  **carry its R-IDs** — every requirement must be traceable to plan units, and
  the verifier checks coverage.
- If it's a direct description, run a **short bootstrap grill** (same
  one-question-at-a-time discipline as below) covering only: objective, scope
  in/out, and success criteria. Don't re-run a full brainstorm — if the topic is
  genuinely unformed, stop and point at `/orchestrator-brainstorm` instead.

## Phase 2 — Explore (codegraph-first, inline)

For **every area the plan will touch**, establish ground truth before grilling:

- `codegraph context` for how the area works, `explore` for exact symbol names,
  `impact` for the blast radius of planned changes (see
  `skills/codegraph/SKILL.md`).
- Record **exact existing symbols and file paths** — the task map's `symbols`
  must exist in the index (unknown ones get dropped with a flag), and its
  `files` are checked against the working tree.
- Files the plan will create are **prospective**: they go in the map like any
  other file and get marked `*(new)*` in the unit prose — or
  `*(new, small|medium|large)*` when you have a confident size estimate, which
  renders 1:1 into the task map's `size_hints: {path: class}` key (priced
  500/2000/5000; see `docs/orchestrator-task-map.md`). Leave the class off when
  unsure — an unhinted prospective file still prices at today's flat rate.
- **Explore instead of asking** whenever the codebase can answer.

## Phase 3 — Grill

Interview the user relentlessly about every load-bearing aspect of the design,
walking down each branch of the decision tree:

- **One question at a time** via `AskUserQuestion`, **each with a recommended
  answer** (first option, labeled `(Recommended)`).
- **Stress-test with concrete scenarios** that probe edge cases and force
  precision.
- **Cross-reference claims against code** — when the user asserts how something
  works, verify it; surface contradictions immediately.
- **Challenge terms against `CONTEXT.md`** and update the glossary inline as
  terms resolve (lazy creation; glossary only, never a spec — same rules as
  `/orchestrator-brainstorm`).

## Phase 4 — Decide

Every load-bearing choice lands in the plan's **Decisions** section as
*decision / rationale / alternatives rejected*.

Promote a decision to an ADR (`docs/adr/NNNN-slug.md`, lazy directory, scan for
the highest number and increment) **only when all three hold**:

1. **Hard to reverse** — changing it later costs something real.
2. **Surprising without context** — a future reader would wonder why.
3. **A real trade-off** — genuine alternatives existed.

ADR format is minimal — a title plus 1–3 sentences (context, decision, why).
Skip optional sections unless they earn their place. Most plans produce zero
ADRs; that's the expected outcome, not a failure.

## Phase 5 — Write the plan

Write `docs/plans/YYYY-MM-DD-NNN-<type>-<name>-plan.md` (NNN = next free number
that day; `<type>` = feat/fix/refactor/…):

```markdown
---
title: <Title>
type: <feat|fix|refactor|docs|…>
date: YYYY-MM-DD
origin: <brainstorm doc path, or "direct">
---

# <Title>

## Objective

<The outcome, measured against the origin's R-IDs when there is one.>

## What we already know (resolved context)

<Concrete discoveries from Phase 2 — exact file paths, symbol names, existing
behaviors, prior decisions. The plan is the handoff to the orchestrator's cold
worker sessions via base-context: anything a worker would otherwise re-derive
belongs here.>

## Decisions

- **<Decision>.** <Rationale; alternatives rejected.> <(→ ADR NNNN when promoted)>

## Units

### U1. <name> — <goal in one line>

- **Goal**: <what done looks like>
- **Files**: `existing/path.py`, `new/path.py` *(new, medium)*
- **Symbols**: `existing_fn`, `ExistingClass`
- **Depends-on**: — <or U-IDs>
- **Slice**: — <or slice label>
- **Implements / Consumes**: — <or route/contract tags>
- **Verification**: <concrete checks — these feed the speccer's verification
  items, so write them as testable statements>

## Task Map

<The fenced YAML block per docs/orchestrator-task-map.md, generated **1:1 from
the units above** — same ids, same files, same deps, same slices, same tags.
Any divergence is a bug the verifier will catch.>
```

### Vertical-slice guidance

- **Few, large, independently-testable slices** — a slice should verify
  end-to-end on its own (API + UI + tests of one feature beats three layers of
  half-features).
- **Shared-infra / cross-cutting units carry no slice** — they are hub material
  and must stay free to be isolated and scheduled first.
- **Slices respect the size cap** (≤ 5 tasks, and a slice's summed content must
  plausibly fit one worker's token budget). Slice must-link is a **hard output
  invariant** — a slice never splits across groups. An oversized slice is not
  quietly absorbed: `group` fails loudly naming the slice, its members, their
  work, the cap, and the overshoot, unless the plan is run with
  `--allow-oversized-slice` (which keeps it whole as one flagged group instead).
  Size it to fit; don't rely on the splitter to bail you out.
- **Inter-slice `depends_on` must be acyclic** — a cycle between slices becomes
  a group-DAG cycle and fails the whole grouping run loudly.

### Verification-item guidance

- **Phrase verification items behaviourally — observable outcomes, never
  framework-internal introspection.** An item like *"the router is registered
  on the app object"* invites tests that walk private framework structure and
  break across versions (a FastAPI point release renamed an internal wrapper
  attribute and failed an otherwise-correct group). The same requirement
  phrased as *"`GET /openapi.json` lists these paths"* is both stronger and
  version-proof.

### No-placeholder rules

No TODO/TBD, no "figure out later", no unresolved `<angle-bracket>` stubs, no
unit without verification items. A plan with holes produces workers with holes.

## Phase 6 — Verify

Spawn **one** verifier subagent — **sonnet** — with the prompt template in
[`verifier-prompt.md`](./verifier-prompt.md), filling in the plan path and the
origin doc path. It checks origin coverage, placeholders, prose↔map 1:1
consistency, codegraph existence of files/symbols, prospective markers,
`depends_on` resolvability and acyclicity (including inter-slice), slice caps,
and route-tag consistency.

**Fix every blocking finding inline and re-check.** Advisory findings are
judgment calls — apply or consciously decline them. Do not re-spawn the
verifier for each fix; one verification pass plus inline fixes is the budget.

## Phase 7 — Hand off

- **Interrupted?** Update `STATUS.md` **in place** (current state, the verbatim
  next action, open gates) and append one entry to `docs/session-log.md`
  (newest first: date — summary / done / found-decided / next).
  **Reference, don't duplicate** — link to the plan and docs by path; status
  lives only in `STATUS.md`. A resuming session orients from `STATUS.md` first
  and confirms the next action before continuing.

- **Done?** Before presenting anything, validate the plan yourself with the
  deterministic, zero-LLM fast path:

  ```sh
  smart-mcps-orchestrate group docs/plans/<the-plan>.md --no-spec
  ```

  This is sub-second and catches a malformed task map, a `depends_on` cycle, a
  dissolved or over-budget slice, and hub/Louvain shape — all before paying for
  the speccer. Fix any error it reports (never by hand-editing `groups.json` or
  a `grouping-trace.json` — fix the plan's map) and re-run until it's clean.

  Then present the plan summary + unit list to the user, and point at:

  ```sh
  smart-mcps-orchestrate group docs/plans/<the-plan>.md --dry-run
  ```

  The dry run must show `task map: parsed from plan — mapper LLM skipped` in its
  flags; if it shows mapper output instead, the map block is malformed or
  missing — fix the plan, never hand-edit `groups.json`.

## Non-negotiable rules

- The task map is generated **from the units, 1:1** — never written first, never
  allowed to drift from the prose.
- Exactly **one** verifier subagent, sonnet, at the end. No other subagents.
- No implementation code. Files this skill may write: the plan, `CONTEXT.md`,
  ADRs, `STATUS.md`/`docs/session-log.md` on interruption.
- Explore instead of asking whenever the codebase can answer.
- Never hand-edit `groups.json` to fix a grouping — fix the plan's map instead.
