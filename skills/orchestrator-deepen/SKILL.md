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
and a one-phrase reason**, so the human sees exactly what the spend buys —
and, as everywhere else, the group is `g4` plus the full plan heading of the
member unit(s) that trigger the tag, never `g4` alone:

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

**Once research is approved, recommend *where* the queries run and ask.**
Count the queries the approved groups earn (at most two per group) and size
the brief each needs (signatures + prose from the plan and codegraph):

- **Direct** — you run `smart-mcps-perplexity` yourself from this session:
  the right call when the approved set is small (roughly ≤2 queries total)
  and the briefs are context you already hold — a cold subagent would
  re-derive it for no gain. This is also the natural choice in `inline`
  exploration mode, where the explorer *is* this session.
- **Subagent** — the queries run inside the explorer (batched mode) or a
  `perplexity-explorer` agent: the right call when there are several queries
  or each brief needs fresh codegraph pulls large enough to bloat this
  session's context. Every answer still comes back as a self-contained
  report, so Phase 3 is unchanged.

State the recommendation with the count (*"3 queries, ~2 briefs of new
codegraph context → subagent"*) and confirm with the human in the same
`AskUserQuestion` round when possible; never spawn for a single query.

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

Question wording follows `skills/orchestrator-plan/question-format.md` —
read it once before the first group. For each question:

- Ask via `AskUserQuestion`, **one group's batch at a time** (up to the tool's
  own 4-per-call limit, so a group's 5 questions span two calls).
- **Open every group with its legend**: a group is never `g4` in chat — it
  is `g4 — <n> units` followed by the **full plan heading of every member
  unit, verbatim**, one per line
  (`- U14. rerender-acceptance — six chapters once into artifacts-0911, three EPUBs, acceptance report with the judges' measures`).
  Each stem then **repeats that full heading** before the stakes clause the
  explorer scored (`U14. rerender-acceptance — six chapters once into artifacts-0911, three EPUBs, acceptance report with the judges' measures · if wrong: the acceptance report scores stale renders. …`). `U14`, `U14 rerender-acceptance`, and any paraphrase of the goal are all forbidden —
  the explorer's `Handle` field is the heading copied from the plan, so
  quote it, never re-summarize it. Only the header chip abbreviates
  (`U14 rerend`), because the tool caps it at 12 chars; it is never the
  group id or a question number.
- **Visuals only when relational**: a preview table when a question spans
  3+ units/files and hinges on order, coverage, or lifecycle; a ≤ 6-node
  Mermaid *mechanism* diagram (the explorer's two readings drawn as two
  small flows, or one lifecycle with the disputed transition marked) when
  the readings differ in how something flows — one page per plan,
  republished per group. Never the group DAG.
- **Candidate answers are always offered** — never a bare free-form prompt.
  The `(Recommended)` option is the reading the repo's existing convention
  or the plan's own text favors, when one does; otherwise lead with the
  lower-effect-size reading and say so.
  Include an explicit "either is fine" option whenever the explorer's two
  readings are genuinely both acceptable; recording that answer frees the
  constraint rather than forcing an arbitrary pick.
- **Hard questions carry a card** (format doc §3b): when the explorer
  scored `blocking_risk` ≥ 2, or Reading A and Reading B differ in how a
  mechanism flows rather than in a parameter, print the four-line card in
  chat right before the call — *Today* (what the code does now, from the
  explorer's `Today` field with its file:line), *The fork* (both readings
  in plain words, each with one concrete consequence), *Our view* (the
  reading we favor, the plan unit or code that tipped it, a confidence
  word, and what would change our mind). The explorer deliberately gives no
  recommendation; forming and **stating** that view is this skill's job,
  and the `(Recommended)` option must be the same reading the card names.
  Run the self-check: if the fork cannot be restated in one sentence from
  the card alone, rewrite the card before asking.
- Frame the question in plain language — the human answering may not have
  read the explorer's report, and will not open the code to decode a term
  the plan never used.

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
  `Pass:` always present. A `Pass:` on a real-model output (an LLM's answer,
  a transcription, a generated summary) never uses a hard `= 0`: write
  "< baseline" with the baseline named, or "≤ N with the residuals listed". This is a convention inside the existing
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

## Phase 4b — Sandbox sweep: can a coder actually run each `Run:`?

The last pass before hand-off, over **every** `Run:` line the plan now
carries, deepened or not. A worker is Landlock-confined (see
`orchestrator/execution/confinement.py`) to: its own worktree, its own
`~/.claude/projects/<slug-of-that-worktree>`, the worktree's git dirs, the
probed `~/.claude` runtime dirs, `~/.claude/.credentials.json`, the
orchestrator cache root, `system_write_paths()` (`/tmp` among them), and
whatever `[session] extra_write_paths` adds. Reads are never restricted;
**writes outside that list fail with `PermissionError`**, and the worker
usually reports it as a mysterious environment defect.

For each `Run:` command, ask what it *writes* and where:

| the command writes…                                             | verdict                                                                |
| --------------------------------------------------------------- | ---------------------------------------------------------------------- |
| inside the worktree (build output, `.coder-scratch/`, test tmp) | fine — nothing to do                                                   |
| a repo-level data dir (a corpus, models, renders)               | add it to `[workspace] data_dirs` (it is symlinked in and allowlisted) |
| a fixed path outside the worktree (a shared cache, `/opt/...`)  | add it to `[session] extra_write_paths` and say so in the unit         |
| a path not knowable until the run (see below)                   | mark the item `Run (driver):`                                          |
| `/tmp`, or kills/resumes a child process                        | fine — `Run:`; process control is not a write                          |

**The default is `Run:`; `Run (driver):` only when the command spawns a nested
`claude` or writes to a path outside the allowlist that cannot be declared in
advance.** The standing case is the live tier — any `-m llm` test, or
anything else spawning a nested `claude`. Its transcript goes to
`~/.claude/projects/<slug-of-its-own-cwd>`, a directory named after a fixture
path that does not exist when the config is written, and the only blanket
allowlist that would cover it is `~/.claude/projects` wholesale — which is
exactly the rule that keeps a worker out of every other session's `memory/`.
Do not propose loosening it. (A test that pins its fixture cwd to a fixed
path *can* be allowlisted instead — offer that only if the human wants the
check to run inside the sandbox.)

Write the decision into the plan:

- Allowlisted → name the path in the unit's prose, and tell the human the
  config line to add before launch. This skill never edits
  `.orchestrator/config.toml`; a missing line is a launch-time failure the
  run-driver's preflight is supposed to catch.
- Driver-run → rewrite the item's command line as `Run (driver): <command>`.
  `smart-mcps-orchestrate group` sets `driver_run` on that item, the coder
  prompt tells the coder it may attempt it only if it spawns no nested
  `claude` and writes nowhere outside the sandbox (report `pass`/`fail` with
  evidence, otherwise `skipped` with notes `driver-run`), the verification
  gate never holds the group on it, and the run log names at merge the items
  the coder did not pass, for the driver to run.
- Driver-run **and the sweep found it sandbox-safe** (it must run after the
  merge, but it spawns no nested `claude` and writes only inside the
  worktree) → write `Run (driver, sandbox-safe): <command>` instead. The
  coder prompt then makes the attempt owed, not permitted: `pass`/`fail`
  with evidence, `skipped` only with the exact command and its error, and a
  bare `driver-run` skip is flagged in the merge log. Check the command and
  every flag exist (`<cmd> --help`) before writing either form.

Report the sweep as one short block: how many `Run:` lines, how many now
driver-run, and which config lines the human must add. An item that needs a
path nobody can name is not verifiable — say so rather than marking it
driver-run.

### `run` units in the sweep

A `run` unit (`recipe: run`) has no coder `Run:` lines to check — sweep its
`recipe_args.commands[].cmd` entries the same way, but against the **Run
Child profile**, not the worker profile: the worker's Landlock grants plus
`[workspace] data_dirs` plus that unit's own `recipe_args.allow_write` list
(`orchestrator/execution/run_executor.py::_confinement_preexec`). A command
writing somewhere the worker profile already covers needs nothing added; a
fixed path outside it goes in `allow_write` (never a bare `[session] extra_write_paths` entry — that config path is the worker's own, unrelated
list); a path not knowable until the run is the same "not verifiable" case as
above — say so rather than inventing an `allow_write` entry to guess it.
`allow_write` rejects (at parse time) any entry equal to or containing
`~/.claude` or `~/.claude/projects` — never propose routing around that.

For every `run` unit, also ask for two figures and write them into the unit
if missing: each command's `wall_clock_min` cap (a real estimate for the
work, not padding — a command that exceeds it is killed and the group fails)
and, if the human has a rough one, an expected cost/duration note for the
whole unit, since a `run` group prices as its declared wall clock plus a
fixed triage-token allowance rather than file arithmetic (see
`docs/orchestrator-task-map.md`'s v2 section). A command with no
`wall_clock_min` deserves a question, not a guessed default.

### The `PATH`-not-absolute-path rule, again

The same rule from `/orchestrator-plan` applies to every command this sweep
touches, coder or `run`: reference a tool by name on `PATH` (`uv run …`),
never by an absolute path baked in from this planning session's own
checkout — the worktree a command actually runs in is a different path on
disk. A command hardcoding a path is a defect to fix here, not a
sandbox-sweep table entry.

## Phase 5 — Hand off

This skill is **optional for every run** — a plan that was never deepened is
still a valid `group`/`run` input, just without the extra edge-case and
`Run:`/`Pass:` coverage. Present the human with a short summary (questions
asked, "either is fine" answers, units enriched) and point back at
`smart-mcps-orchestrate group <plan> --no-spec` to confirm the enriched plan
still parses clean. If any existing prose was refined (not merely appended
to), remind the human to regenerate the grouping with
`smart-mcps-orchestrate group <plan>` before running — the persisted
`groups.json` was derived from the pre-deepen plan.

**Always close with the launch line.** The very last thing this skill
prints — after the final group's write-back and the summary — is the
ready-to-paste next step, with the plan's real path substituted, never a
placeholder:

```
Deepening done — 4 groups, 17 questions, 3 "either is fine".
Next: run it with the orchestrator:

  /orchestrator-run docs/plans/2026-09-13-rerender-acceptance.md
```

That is the next step — `/orchestrator-run <plan>` regenerates a stale
grouping itself in its preflight — not a bare `smart-mcps-orchestrate run`.
If the human asked to stop after some groups, print the same block with
"deepened g1–g2 of 4" in the first line; the launch line does not change.

## Non-negotiable rules

- **The task map and unit ids are never rewritten** — every write goes
  through `plan_edit.py` and is checked with `plan-check --against` the
  pre-edit copy; a refusal aborts.
- **Every hard question is explained before it is asked** — card in chat
  (today / the fork / our view with confidence), plain words, one concrete
  consequence per reading; a terse hard question is a bug.
- **3–5 questions per group, always with candidate answers**, dominant over
  any plan-global cap — worded per
  `skills/orchestrator-plan/question-format.md`: legend first, the unit's
  full plan heading (verbatim) and stakes in every stem, no bare `U3`, no
  `U3 package-writer`, no `g4` without its member headings.
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
- **Perplexity research is opt-in, defaults to No, and runs where the
  context already is** — direct from this session for a handful of queries,
  inside the explorer / a `perplexity-explorer` subagent when the query count
  or brief data warrants it (recommended, then confirmed with the human);
  never a separate per-group researcher. It fires only for groups the human
  approved from the tagged `[external]`/`[research]`/`[hard]` listing, at
  most two queries per group, and its candidates still must pass the
  divergence test like any other.
- No implementation code. Every new bullet and every refined prose line is a
  direct write of an explicit human answer — prose no answer touched is
  never regenerated.
