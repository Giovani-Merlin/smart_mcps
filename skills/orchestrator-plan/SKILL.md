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
- Record **exact existing file paths** — the task map's `files` are checked
  against the working tree.
- `symbols` is **optional** and only ever a supplement, never a requirement.
  Listed symbols contribute derived precedence edges between tasks that touch
  them — useful when the plan's own `depends_on` and shared-file affinity
  don't already capture the coupling. On a dense codebase, populating
  `symbols` heavily can backfire: on this repo it added over a hundred
  inferred edges and degenerated the partition (near-saturated the task
  graph into one component). Default to leaving `symbols` empty and let
  declared `depends_on` plus shared-file affinity carry the structure; add
  symbols deliberately, only where the coupling is real and otherwise
  invisible to the mapper.
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

Unit headings and task ids are the same identity in two forms — heading
`### U<N>. <name>` ↔ task id `u<N>-<slug>` — the parser and the digest builder
both key off this pairing, so a mismatch is a bug, not a style choice. Every
unit's **`Summary:`** line is required: it is the only piece of the unit that
ships into every other worker's shared context (the full unit body ships only
to workers on that unit's own group), so write it as a self-contained sentence
a stranger unit can consume with no other context.

### U1. <name> — <goal in one line>

- **Summary**: <one tagged sentence — what this unit ships, used verbatim in
  the shared digest every worker's context carries>
- **Goal**: <what done looks like>
- **Files**: `existing/path.py`, `new/path.py` *(new, medium)*
- **Symbols**: — <or `existing_fn`, `ExistingClass` — optional; see the
  dense-codebase trade-off in Phase 2>
- **Depends-on**: — <or U-IDs>
- **Slice**: — <or slice label>
- **Implements / Consumes**: — <or route/contract tags>
- **Verification**: <concrete checks, one per bullet — these feed the
  speccer's verification items, so write them as testable statements. A
  single check is still one bullet, not inline prose:
  - <first observable outcome>
  - <second observable outcome, if any>>
- **Edge cases**: — <optional; one-line entries, one per fired category, no
  `N/A` filler — this is `/orchestrator-deepen`'s territory. A planning
  session may leave this slot empty>
- **Non-goals / must-not**: — <optional; same rule — deepen's territory, a
  planning session may leave it empty>

`Verification` bullets may optionally carry the `Run:`/`Pass:` convention —
`Run:` a narrow, grounded command that proves the item (never a bare
full-suite invocation), `Pass:` the observable condition that proves it
passed. Both lines live inside one bullet and assemble into a single
`VerificationItem`; this is a prose convention, not a schema change. A
planning session may write plain behavioural sentences instead and leave the
`Run:`/`Pass:` split to `/orchestrator-deepen`.

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
- **Inter-slice `depends_on` should still be acyclic** — a cycle between slices
  becomes a group-DAG cycle. This is no longer a hard failure: `build_group_dag`
  repairs it automatically (merging the cyclic SCC, then re-splitting it back
  under the cap), but the repaired group can land larger and less clean than a
  merge that was never needed, and an unrepairable cycle is an orchestrator bug,
  not a routine planning error. Prevention is still cheaper than repair — plan
  dependencies acyclically rather than relying on the repair path.

### Verification-item guidance

- **Phrase verification items behaviourally — observable outcomes, never
  framework-internal introspection.** An item like *"the router is registered
  on the app object"* invites tests that walk private framework structure and
  break across versions (a FastAPI point release renamed an internal wrapper
  attribute and failed an otherwise-correct group). The same requirement
  phrased as *"`GET /openapi.json` lists these paths"* is both stronger and
  version-proof.

### Inputs workers can actually see

Workers run in git worktrees and see only **committed** files. If a unit or a
verification item reads a data file (a PDF, an archive, a corpus, a model),
say where it comes from: either it is tracked on the branch, or it lives
under a directory the operator has listed in `.orchestrator/config.toml`
`[workspace] data_dirs` — those are shared into every worktree without being
committed. An untracked file at the repo root is invisible to every worker.
The verifier flags such paths (check 9); prefer fixing them in the plan or
the config before the run rather than discovering it four groups in.

**Check the config while planning, not after.** If any unit reads or produces
data files, open `.orchestrator/config.toml` and confirm `[workspace]
data_dirs` exists and names the directory those files live in (create the
block with the human if it does not — e.g. `data_dirs = ["data"]`), then
write every such path in the plan relative to that directory (`data/…`), so
the units, the verification items, and the workers all point at the shared,
uncommitted copy.

- **Every unit gets at least one external-oracle item.** An item whose only
  oracle is the tests the worker will write ("`pytest tests/test_tts.py`
  passes") is self-referential: a worker who mocks the library under test
  satisfies it honestly and the run learns nothing. Pair such items with one
  that touches the real thing — the actual library installed and called, a
  real input file from a data dir, a real command's output ("`gab render
  data/sample.txt` produces a WAV ≥ 2 s using the installed `piper` model").
  If no real oracle exists yet because the data or model is not available,
  say so in the item and route the input through `[workspace] data_dirs`
  rather than accepting a mock as the oracle.

### No-placeholder rules

No TODO/TBD, no "figure out later", no unresolved `<angle-bracket>` stubs, no
unit without verification items. A plan with holes produces workers with holes.

## Phase 6 — Verify

Spawn **one** verifier subagent — **sonnet** — with the prompt template in
[`verifier-prompt.md`](./verifier-prompt.md), filling in the plan path and the
origin doc path. It checks origin coverage, placeholders, prose↔map 1:1
consistency, codegraph existence of files/symbols, prospective markers,
`depends_on` resolvability and acyclicity (including inter-slice), slice caps,
route-tag consistency, whether referenced input files are visible to workers
(tracked, or under a configured data dir), and whether every unit has at
least one verification item with a real oracle rather than "my tests pass".

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

  Once `--no-spec` is clean, run the advisory pass — also zero LLM, one graph
  build:

  ```sh
  smart-mcps-orchestrate group docs/plans/<the-plan>.md --advise
  ```

  Present its diagnostics to the user as-is: the granularity comparison across
  presets, and any cohesion flags ("this reads as N separate plans", "this
  reads as serial phases", "structurally monolithic"), each printed with its
  numbered seam. Then **ask** the user whether to split the plan along a
  reported seam or proceed as one plan — proceed-as-one is an explicit option,
  not just the silent default. Never rewrite plan prose based on the advisory
  yourself — if the user chooses to split, run the mechanical, zero-LLM split
  on their behalf and let it move the plan's text:

  ```sh
  smart-mcps-orchestrate split docs/plans/<the-plan>.md --seam <N>
  ```

  This moves unit sections and task-map entries verbatim into new documents
  beside the original (see `docs/orchestrator-grouping.md`'s `split`/`plan-check`
  section); it never rewrites prose and never deletes the source plan. If the
  user disagrees with the reported seam, `--tasks u1,u2 --tasks u3,u4` takes an
  explicit assignment instead — every task id in the plan's task map must
  appear in exactly one `--tasks` group.

  Then present the plan summary + unit list to the user, and point at:

  ```sh
  smart-mcps-orchestrate group docs/plans/<the-plan>.md --dry-run
  ```

  The dry run must show `task map: parsed from plan — mapper LLM skipped` in its
  flags; if it shows mapper output instead, the map block is malformed or
  missing — fix the plan, never hand-edit `groups.json`.

  Finally, print the ready-to-run enrichment command as a recommendation —
  this skill never runs it itself:

  ```sh
  /orchestrator-deepen docs/plans/<the-plan>.md
  ```

## Non-negotiable rules

- The task map is generated **from the units, 1:1** — never written first, never
  allowed to drift from the prose.
- Exactly **one** verifier subagent, sonnet, at the end. No other subagents.
- No implementation code. Files this skill may write: the plan, `CONTEXT.md`,
  ADRs, `STATUS.md`/`docs/session-log.md` on interruption.
- Explore instead of asking whenever the codebase can answer.
- Never hand-edit `groups.json` to fix a grouping — fix the plan's map instead.
