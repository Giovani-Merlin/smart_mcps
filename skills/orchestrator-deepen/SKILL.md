---
name: orchestrator-deepen
description: Grills the human with codegraph-grounded, EVPI-ranked edge-case questions per group of an orchestrator plan, and writes the answers back into the plan as per-unit edge cases, non-goals, refined Goal/Summary prose, and Run:/Pass: verification items — without ever touching the task map or unit ids. Exploration is context-budgeted via `group --advise`: inline, batched explorers, or per-group, chosen with the human.
user-invocable: true
argument-hint: "<path to a plan document under docs/plans/>"
---

# orchestrator-deepen

You are in **enrichment mode**. No implementation code is written, and the
plan's **task map and unit ids are never touched** — this skill edits only
the prose inside existing unit sections: it adds optional bullets
(`Edge cases`, `Non-goals / must-not`, `Run:`/`Pass:` verification lines)
and may **refine existing unit prose** (a `Goal`/`Summary` line the human's
answer contradicts or sharpens) — always through the shared verbatim-surgery
module (`orchestrator/grouping/plan_edit.py`, plan U1). Every write is
followed by `plan-check --against` the pre-edit copy; a refusal aborts the
write immediately. A refined plan is a *new* plan — after deepening, the
grouping must be regenerated (`group <plan>`); never hand-edit an existing
`groups.json` to match.

Plan: `$ARGUMENTS`

______________________________________________________________________

## Phase 1 — Load the plan and its groups

- Read the plan document and its
  `.orchestrator/groupings/<name>/preview/advisory.json` (produced by
  `group --advise`) — this skill does not compute grouping itself. If the
  advisory file is missing, stale against the plan, or predates the
  `context` section, refresh it:

  ```sh
  smart-mcps-orchestrate group <plan> --advise
  ```

  The report's **`context` section is the exploration budget** for Phase 2:
  per-group file sets and estimated tokens, file-overlap clusters, a
  bin-packed `batches` assignment, and a `recommendation`
  (`inline` / `batched` / `per-group`). Its `group_index` numbering is the
  advisory's own — match advisory groups to `groups.json` groups by their
  member `tasks` sets, never by id.

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

## Phase 2 — Explore, at the cheapest mode that covers the context

**Never default to one explorer per group.** A 20-group plan is not 20
exploration jobs — groups exist for merge scheduling, but exploration has no
precedence: its only cost is reading files, and groups share neighborhoods.
The advisory `context` section already computed the right shape; confirm it
with the human in **one** `AskUserQuestion`, quoting the real numbers, with
the advisory `recommendation` as the first (Recommended) option:

- **`inline`** — total estimated tokens fit the inline budget: spawn
  **nothing**. Read the groups' files yourself in this session and walk the
  explorer process (all ten categories, divergence test, scoring, `Run:`
  grounding) directly. This is the normal case for small and medium plans —
  a cold subagent that re-derives context you already hold is pure waste.
- **`batched`** — one read-only explorer **per advisory batch**, each
  covering every group in its batch (shared files read once). Say how many
  explorers and the estimated tokens each, e.g. "3 explorers, ~90k tokens
  each, instead of 25 spawns".
- **`per-group`** — the legacy shape; only worth offering when the advisory
  itself recommends it (each group is its own neighborhood near the cap).

A batch whose estimate is marked over the explorer cap gets flagged to the
human — it signals a group too fat to explore in one pass, which is a
grouping/split problem, not a reason to shard exploration further.

**In the same `AskUserQuestion` call, add a second question: enable
Perplexity research?** It is paid API spend, so it is opt-in and defaults to
**No**. When enabled, external research is a *step inside* the explorer (or
the inline pass) — never a second agent per group, which would re-derive the
context the explorer already holds.

Scope it honestly when asking: **name each research-worthy group with a tag
and a one-phrase reason**, so the human sees exactly what the spend buys:

- `[external]` — a member unit touches a third-party library, an OS/system
  API, a wire protocol, or an external service
  (*"g4 — calls the GitHub REST API"*);
- `[research]` — the work is AI/LLM behavior, a non-trivial algorithm
  (clustering, graph, scheduling, parsing…), or a framework/technique new to
  this repo — even when the code is fully internal
  (*"g2 — Louvain resolution tuning, internal but literature-heavy"*);
- `[hard]` — `groups.json` marks the group's `difficulty` at or above the
  configured `d_hard` review threshold (*"g7 — difficulty 0.81"*). A `[hard]`
  tag alone is a nudge to say yes, not a query trigger by itself — a hard but
  well-understood group gains nothing from the web.

Queries fire for the `[external]` and `[research]` groups the human approves
— untagged groups fire none even when research is enabled. If no group earns
any tag, skip the question entirely and note why.

Each explorer (or the inline pass) uses the template in
[`explorer-prompt.md`](./explorer-prompt.md), filling in the plan path, the
batch's group ids, and those groups' member unit sections verbatim — one
report section per group, so Phase 3 is identical regardless of mode. The
explorer:

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

Batches may always run in parallel: a downstream group's explorer does not
need an upstream group's answers, since edge cases and verification are
recorded per-unit, not per-dependency — dependency order constrains the
*run*, never the exploration.

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

- **Existing unit prose may be refined, not just appended to**: when an
  answer contradicts or sharpens a unit's current `Goal`/`Summary`/boundary
  text, rewrite that line rather than leaving the stale sentence beside a
  correcting bullet. Every changed line must trace to a recorded human
  answer — never regenerate a unit's prose wholesale or "improve" text no
  answer touched. The task map and unit ids stay immutable regardless.

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
still parses clean. If any existing prose was refined (not merely appended
to), remind the human to regenerate the grouping with
`smart-mcps-orchestrate group <plan>` before running — the persisted
`groups.json` was derived from the pre-deepen plan. The next step is
`/orchestrator-run <plan>` (it regenerates a stale grouping itself in its
preflight), not a bare `smart-mcps-orchestrate run`.

## Non-negotiable rules

- **The task map and unit ids are never rewritten** — every write goes
  through `plan_edit.py` and is checked with `plan-check --against` the
  pre-edit copy; a refusal aborts.
- **3–5 questions per group, always with candidate answers**, dominant over
  any plan-global cap.
- **A `Run:` command is written only when grounded** (real runner idiom, every
  path in the unit's declared `Files`); otherwise `Pass:`-only.
- **Edge cases only where they fire** — no `N/A` filler.
- **Data inputs go through the data layer.** Before drafting any `Run:`/`Pass:`
  that opens a data file (corpus, PDF, model, archive), check
  `.orchestrator/config.toml` has `[workspace] data_dirs` covering it and
  write the path under that directory (`data/…`); if the block is missing,
  ask the human to add it rather than pointing a verification item at a file
  workers will never see.
- **At least one real-oracle verification item per unit.** When a unit's
  items all reduce to "the worker's own tests pass", the explorer proposes —
  and the human confirms — one `Pass:` condition that exercises the real
  dependency, input file, or command output (a real model rendering real
  audio, a real PDF yielding N chapters). Mocks are how r20260830-211717's
  four groups passed everything with no working environment.
- **Exploration mode follows the advisory `context` budget** — inline when
  the total fits the inline budget, batched by the advisory's packing
  otherwise; one-explorer-per-group is never the default, only a confirmed
  choice.
- **Perplexity research is opt-in, defaults to No, and lives inside the
  explorer** — never a separate per-group researcher; it fires only for
  groups the human approved from the tagged `[external]`/`[research]`/`[hard]`
  listing, at most two queries per group, and its candidates still must pass
  the divergence test like any other.
- No implementation code. Every new bullet and every refined prose line is a
  direct write of an explicit human answer — prose no answer touched is
  never regenerated.
