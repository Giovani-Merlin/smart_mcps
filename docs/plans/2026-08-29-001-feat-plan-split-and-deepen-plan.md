---
title: Mechanical plan split and the deepen skill
type: feat
date: 2026-08-29
origin: docs/brainstorms/2026-08-28-grouper-speccer-flow-requirements.md
---

# Mechanical plan split and the deepen skill

## Objective

Ship wave 2 of the grouper-speccer-flow requirements — R16 (mechanical plan
split), R17 (`/orchestrator-deepen`), and R18 (its question policy and
enrichment template, grounded in the R19 research already received) — so that
the advisory grouper shipped in wave 1 has somewhere to lead:

- **R16**: `smart-mcps-orchestrate split` turns a chosen seam into N plan
  documents by moving unit sections and task-map entries **verbatim**, with no
  LLM regeneration of any prose.
- **R17/R18**: `/orchestrator-deepen <plan>` is a standalone interactive skill
  that spawns read-only explorers, grills the human with a capped, EVPI-ranked
  question set, and writes the answers back into the plan as per-unit
  enrichment — edge cases, non-goals, and sharpened `Run:`/`Pass:` verification
  items.

Both commands write into a plan document, which is the fingerprint-drift bug
class this repo has been bitten by before. So both go through **one shared,
tested plan-surgery module** whose guarantee is byte-level: the task map and
unit ids either survive a rewrite untouched, or the rewrite is refused.

R19 is already satisfied — both Perplexity reports were received 2026-08-28 and
their findings are folded into the origin brainstorm; this plan consumes them
rather than re-commissioning them. R20 (the eval harness) stays out of scope,
blocked on Infinity Skills ingestion.

## What we already know (resolved context)

Ground truth verified 2026-08-29 against `main` at `8adabbf`, with the full
suite green (`uv run pytest` → 1505 passed, 20 deselected).

### Wave 1 landed, but has never driven a live worker

All 13 wave-1 units are merged (PR #5, groups g1–g11). But the run that *built*
wave 1 (`.orchestrator/runs/r20260828-220035`) still used the old LLM speccer —
`llm/01-speccer_output-a0.raw.txt` exists and that run's `groups.json` specs are
LLM prose. So assembled specs (U2), the layered digest base-context (U3),
contracts-only neighbours, the merge fill penalty (U12) and ADR-0007
fresh-start workers are all **merged but unexercised by real workers**. This
plan's own run is their first live validation; that is intentional and is why
wave 2 was chosen to be almost entirely additive (a new subcommand and a new
skill directory) rather than another rewrite of the execution path.

### The advisory report already carries machine-usable seams

`build_advisory_report` (`orchestrator/grouping/advisory.py:148`) writes
`preview/advisory.json` under `.orchestrator/groupings/<name>/`. Its
`AdvisoryReport` is `{version, plan_path, granularities, cohesion}`; each
`CohesionFinding` (`advisory.py:116`) is
`{kind: disconnected|serial|monolithic, message, task_sets, boundary}`, with
`task_sets` populated for `disconnected` and `boundary` for `serial` — never
both. Measured live on the 36-unit 2026-08-26 plan, the `disconnected` finding
returned `task_sets` of 35 and 1 tasks: a real seam, and a good illustration of
why the operator must be able to override it.

Two gaps for R16: the human rendering (`_print_advisory_report` in
`orchestrator/cli.py`) prints the finding's `message` but **not** a stable index
and **not** the task sets, so there is nothing for `--seam N` to address yet.

### The plan-surgery primitives that exist and the one that does not

- `parse_plan_sections` (`orchestrator/grouping/plan_sections.py:190`) returns
  `PlanSections{preamble, units, digest, flags}`; each `UnitSection` carries
  `unit_id, title, text (verbatim, found in source), summary,
  summary_is_fallback, verification, implements, consumes`.
- `_split_bullets` (`plan_sections.py:93`) collects **any** top-level
  `- **Label**: value` bullet into a dict and ignores labels it does not know.
  Verified by reading: new slots (`Edge cases`, `Non-goals / must-not`) parse
  harmlessly today and need no parser change to survive — unit sections travel
  verbatim into group specs, so deepen's enrichment reaches workers for free.
- `parse_task_map` / `parse_task_map_for_pricing` (`plan_reader.py`) locate the
  fenced map with the module-level `_BLOCK` regex and `yaml.safe_load` it.
  There is **no writer** anywhere in the codebase — no `yaml.dump`, no
  serializer for the map. That is a feature, not a gap: task entries are
  top-level `  - task_id:` list items, so a split can slice them as text and
  keep comments, ordering, and formatting byte-identical. What is missing is a
  helper exposing the fence's span; only the regex exists.
- `compute_partition` calls `compile_base_context` (`pipeline.py:615`), which
  calls `parse_plan_sections` (`base_context.py:41`). So `group --no-spec`
  **already** fails on structural breakage — a missing unit section, a mangled
  heading, a map entry with no matching `### U<N>`. What it cannot catch is
  silent semantic drift: a quietly edited `depends_on`, `size_hints` value, or
  unit id that still parses.

### How verification items reach a run today

`VerificationItem` is `{id, description, required}` (`orchestrator/model.py:32`).
Since wave 1 the items are assembled verbatim from plan Verification bullets
(`assemble_group_specs`, `orchestrator/grouping/assembler.py:170`) with ids
`^g\d+-\d+$`, and `_lint_verification_coverage` (`assembler.py:308`) already
enforces that every plan bullet lands in exactly one group. They are handed to
the coder (`orchestrator/prompts/coder.md:12`, `$verification`) and the reviewer
(`orchestrator/prompts/reviewer.md`, `$verification`), and every one must come
back in `verification_results` (shape at
`orchestrator/prompts/report_contract.md:7`) with status pass | fail | skipped
(enumerated at `orchestrator/prompts/worker_ground_rules.md:72-73`).
`reviewer.md:9-10` instructs the reviewer to "run the tests the spec calls
for" — i.e. **the reviewer infers the command from prose today**, which is the weak-self-verification failure mode the
R19a benchmarks named as dominant. Adding `Run:`/`Pass:` lines is a pure
convention change inside `description`; no schema, assembler, or prompt-contract
change is required.

### CLI conventions

`orchestrator/cli.py` registers flat verb subcommands — `group`, `run`,
`resume`, `groupings`, `status`, `answer`, `retry`, `finish`, `export`,
`calibrate`, `ui` (`cli.py:195–369`). There is no nested sub-subcommand parser
anywhere, so wave 2's commands are flat verbs too. `group` already carries the
zero-LLM preview flags `--dry-run`, `--no-spec`, `--advise`, `--price`
(`cli.py:204–240`), all of which write only under `preview/` and never touch a
persisted `groups.json`.

### Sizes that matter for budgeting

`orchestrator/cli.py` is 106 KB and `tests/test_cli.py` is 86 KB — together
~66k node work, which is why this plan puts the new CLI tests in new files
(`tests/test_plan_edit.py`, `tests/test_plan_split.py`) and never opens
`tests/test_cli.py`.

### Deliberately not fixed here

Running `--advise` on the 36-unit plan returned **identical** metrics for all
three granularity presets (29 groups, same makespan, same modularity, all three
flagged pareto-dominant). Either the dial does not bite on that plan's shape or
a preset is being applied three times. This is a tuning question with only two
or three good plans to test against; per the 2026-08-29 grill it waits for the
R20 eval harness and is recorded in the backlog below rather than guessed at
now.

### Symbols stay empty

Per R9/C4: on this dense codebase populating `symbols` added 103 inferred
precedence edges and degenerated the partition. This plan's map declares no
symbols; `depends_on` and shared-file affinity carry the structure.

## Decisions

- **One shared plan-surgery module is the write-safety mechanism, not a
  convention.** `orchestrator/grouping/plan_edit.py` does verbatim extraction
  and reassembly of unit sections and task-map entries, and exposes
  `verify_map_unchanged(before, after)` for a byte-level guarantee. Both
  `split` and `/orchestrator-deepen` write through it, and it is exposed as
  `orchestrate plan-check` so a skill can verify its own edit. Rejected:
  skill convention plus a `group --no-spec` re-run (catches structural breakage
  but not a silently edited `depends_on`); a guard inside deepen only (split
  needs the same verbatim-extraction primitives, so it would be duplicated).
- **Seams are addressable from the report *and* overridable, and the plan skill
  drives it too.** `split --seam N` consumes `preview/advisory.json`;
  `--tasks u1,u2 --tasks u3,u4` overrides when the operator disagrees — as they
  would with the measured 35-vs-1 seam; and `/orchestrator-plan` reads the
  advisory, asks, and invokes `split` with the chosen assignment. Per the
  2026-08-29 grill: a smart CLI the skill can steer, on the reasoning that
  every plan is a different plan and the eval harness has not yet told us what
  to hard-code. Rejected: `--tasks` only (re-types what `--advise` computed),
  skill-driven only (split unusable standalone or scripted).
- **Flat CLI verbs: `split` and `plan-check`.** Matches every existing
  subcommand; the codebase has no nested sub-parser pattern to follow.
- **`split` is non-destructive.** It writes N new documents beside the original
  and leaves the original in place, printing what it wrote and what to archive.
  Output naming keeps the plan-file convention: `<stem>-part<N>-plan.md` from
  `<stem>-plan.md`. Rejected: rewriting the original in place (unrecoverable if
  the seam was wrong), regenerating filenames with a fresh `NNN` (breaks the
  traceable link back to the source plan).
- **Deepen emits `Run:` + `Pass:` verification lines, explorer-grounded.**
  Grounding means two checks the explorer can actually make: the runner idiom is
  one this repo really uses, and every path in the command appears in that
  unit's declared `files`. A command failing either check degrades to a
  `Pass:`-only condition. `Run:` must be the narrowest command that proves the
  item — never a bare full-suite invocation, which the preflight baseline gate
  already covers. Rejected: `Pass:`-only v1 (leaves the reviewer inferring
  commands, the dominant measured failure mode); ungrounded commands (a wrong
  command burns a reviewer round every time it fires).
- **No schema change for verification.** `Run:`/`Pass:` are conventional lines
  inside the existing `description`; `VerificationItem`, the assembler, and the
  prompt contract are untouched. Rejected: structured command/condition fields
  (a migration across every existing plan and `groups.json` for no gain the
  convention does not already deliver).
- **The per-group question cap dominates the plan-global cap.** 3–5 questions
  per group, always offered with candidate answers, even when a large plan
  therefore exceeds ~10 questions overall. Per the 2026-08-29 grill: every group
  gets clarified rather than losing its questions to a cross-group ranking.
  Rejected: global cap dominant (a low-ranked group is silently skipped and
  ships unclarified), global-with-a-floor (the floor eats the budget on big
  plans anyway).
- **Edge cases are written only where they fire.** Taxonomy-keyed one-liners,
  no `N/A — <why>` filler. The explorer still walks all ten categories
  internally, so coverage lives in the process, not in ten lines per unit that
  every worker on that unit pays to read. Rejected: mandatory `N/A` entries
  (~10 extra lines per unit against the token-optimality preference); a
  high-risk-only subset (the always/never split becomes its own judgment call
  to maintain).
- **Granularity-preset convergence is deferred, not guessed.** Recorded in the
  backlog for the R20 harness.

## Units

### U1. plan-edit — verbatim plan surgery and the `plan-check` guard

- **Summary**: A shared `plan_edit` module that extracts and reassembles unit
  sections and task-map entries byte-verbatim and refuses any rewrite that
  perturbs the map or unit ids, exposed as `orchestrate plan-check`.
- **Goal**: `orchestrator/grouping/plan_edit.py` exists and is the single write
  path for programmatic plan edits. It offers: a task-map block span helper
  (added to `plan_reader.py` so the fence regex stays in one place), verbatim
  extraction of each `  - task_id:` entry and each `### U<N>` section, a
  reassembler that rebuilds a plan document from a chosen subset of units plus
  a preamble, and `verify_map_unchanged(before, after)` which compares the
  surviving task entries and unit ids byte-for-byte and reports every
  difference it finds (not just the first). `orchestrate plan-check <plan>
  --against <path>` runs the same comparison between two documents and exits
  non-zero on any drift; with no `--against` it validates the plan's internal
  consistency alone (map parses, every map task has a section, every section
  has a map entry). No LLM, no codegraph, no graph build — sub-second like
  `--price`.
- **Files**: `orchestrator/grouping/plan_edit.py` *(new, medium)*,
  `orchestrator/grouping/plan_reader.py`, `orchestrator/cli.py`,
  `tests/test_plan_edit.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `plan-edit`, `plan-check-cli`
- **Verification**:
  - Extracting all units from a plan and reassembling them into one document
    yields a file whose task-map block and every `### U<N>` section are
    byte-identical to the original.
  - `verify_map_unchanged` accepts a rewrite that only adds new bullets inside
    a unit body, and rejects a rewrite that alters a `depends_on` list, a
    `size_hints` value, or a `task_id`, naming each altered entry.
  - `verify_map_unchanged` reports **all** differences in one report, not the
    first — a document with three altered entries names three.
  - `orchestrate plan-check <plan>` exits 0 on a well-formed plan and non-zero
    with a message naming the offending unit when a map task has no `### U<N>`
    section, and again when a section has no map entry.
  - `plan-check` completes in under a second on this repo's largest plan and
    makes no codegraph or LLM call (a stub runner that raises on any call is
    not invoked).

### U2. plan-split — `orchestrate split`, seam-addressable and overridable

- **Summary**: `orchestrate split` partitions a plan into N documents by moving
  unit sections and task-map entries verbatim, taking its assignment either
  from a numbered advisory seam or from explicit `--tasks` groups.
- **Goal**: `orchestrate split <plan>` accepts `--seam N` (reads
  `.orchestrator/groupings/<name>/preview/advisory.json` and uses that
  finding's `task_sets`, or for a `serial` finding its `boundary` cut, as the
  task→document assignment) or one `--tasks` flag per output document, which
  overrides. Every task must be assigned exactly once; an unassigned or
  double-assigned task is an error listing all of them. Output documents are
  `<stem>-part<N>-plan.md` beside the original, each carrying the source
  plan's preamble, its own unit sections and map entries verbatim via U1, and
  frontmatter recording `split_from`. A split at a `serial` seam additionally
  writes a one-line predecessor note ("assumes `<part N-1>` is merged") into
  each downstream document — the only new prose the command ever writes. The
  original is left untouched. To make `--seam N` addressable,
  `_print_advisory_report` gains a stable index per cohesion finding and prints
  each finding's task sets (or its boundary cut) rather than only the message.
- **Files**: `orchestrator/cli.py`, `orchestrator/grouping/advisory.py`,
  `orchestrator/grouping/plan_edit.py`, `tests/test_plan_split.py`
  *(new, medium)*, `tests/test_advisory.py`
- **Symbols**: —
- **Depends-on**: u1-plan-edit
- **Slice**: —
- **Implements / Consumes**: implements `split-cli`, `advisory-seams`;
  consumes `plan-edit`
- **Verification**:
  - `group --advise` output numbers every cohesion finding and prints its task
    sets (disconnected) or boundary cut (serial), and the numbering is stable
    across two runs on an unchanged plan.
  - Splitting a fixture plan with `--seam 1` produces documents whose combined
    unit sections and task-map entries equal the original's, byte-for-byte,
    with no unit lost or duplicated.
  - `--tasks` overrides the seam: given both flags, the assignment used is the
    one from `--tasks`.
  - A task assigned to no document, and a task assigned to two, are each
    rejected with a message naming every offending task.
  - Each produced document passes `plan-check`, and running
    `group --price` on each succeeds with every slice under cap.
  - Splitting at a `serial` seam writes the predecessor note into the
    downstream document and not into the first.
  - The source plan file is unmodified after a split (byte-identical).

### U3. deepen-skill — `/orchestrator-deepen`, explorer-grounded and capped

- **Summary**: A standalone interactive skill that explores the codebase
  read-only per group, grills the human with 3–5 EVPI-ranked questions per
  group, and writes the answers back into the plan as per-unit edge cases,
  non-goals, and `Run:`/`Pass:` verification items.
- **Goal**: `skills/orchestrator-deepen/SKILL.md` defines a fresh-session
  interactive skill taking a plan path. It reads the plan and, if present, the
  advisory report; derives group membership deterministically from an existing
  `groups.json` or a `group --no-spec` run (no new code); spawns one read-only
  explorer subagent per group using `explorer-prompt.md`, which walks the
  ten-category edge-case taxonomy (boundary/range, empty/null/missing, error and
  partial-failure modes, concurrency/ordering, idempotency/retries,
  duplication/uniqueness, authz/security, performance budget, data invariants,
  contract compat/versioning) and drafts question candidates by the
  divergence test — two plausible readings that would produce different code.
  Candidates are scored (blocking risk × effect size) and capped at **3–5 per
  group**; each is asked with candidate answers, and "either is fine" is
  recorded as a freed constraint. Every accepted answer lands as an EARS-style
  Goal line ("When `<trigger>`, `<unit>` shall `<response>`"), an `Edge cases`
  entry, a `Non-goals / must-not` entry, or a verification item — never loose
  prose. Verification enrichment follows the `Run:` + `Pass:` convention, with
  `Run:` emitted only when the explorer confirmed the runner idiom is one this
  repo uses and every path in the command appears in that unit's declared
  `files`; otherwise the item is `Pass:`-only. Edge cases are written only
  where they fire — no `N/A` filler. All writes go through U1's module and are
  followed by `plan-check --against` the pre-edit copy; a refusal aborts the
  write. The skill stamps each deepened unit with the plan-content hash it was
  derived from, never touches the YAML task map or unit ids, and is optional for
  every run.
- **Files**: `skills/orchestrator-deepen/SKILL.md` *(new, large)*,
  `skills/orchestrator-deepen/explorer-prompt.md` *(new, medium)*,
  `orchestrator/grouping/plan_sections.py`, `tests/test_plan_sections.py`
- **Symbols**: —
- **Depends-on**: u1-plan-edit
- **Slice**: —
- **Implements / Consumes**: implements `deepen-skill`; consumes `plan-edit`
- **Verification**:
  - A unit section carrying `Edge cases`, `Non-goals / must-not`, and
    `Run:`/`Pass:` verification bullets parses without error, and its `summary`,
    `verification`, `implements`, and `consumes` fields are unchanged from the
    same section without those bullets.
  - The new bullets appear verbatim in the assembled group spec for that unit,
    and the shared digest still contains only the tagged summary lines — no
    edge-case text leaks into the digest every worker pays for.
  - A `Run:`/`Pass:` verification bullet becomes exactly one `VerificationItem`
    whose `description` carries both lines, and the coverage lint still maps it
    to exactly one group.
  - `SKILL.md` states the per-group cap of 3–5 questions as dominant over any
    plan-global figure, and states that candidate answers are always offered.
  - `SKILL.md` states the two grounding checks a `Run:` command must pass and
    the `Pass:`-only fallback when either fails.
  - `SKILL.md` requires every write to go through `plan_edit` followed by
    `plan-check --against` the pre-edit copy, and forbids editing the task map
    or unit ids.
  - `explorer-prompt.md` names all ten taxonomy categories and instructs the
    explorer to report only categories that fire.

### U4. planning-contract — the plan skill and the contracts learn about wave 2

- **Summary**: `/orchestrator-plan`'s advisory phase can now invoke `split`,
  and the task-map and grouping contracts document the new unit slots, the
  `Run:`/`Pass:` convention, and the two new commands.
- **Goal**: `skills/orchestrator-plan/SKILL.md` Phase 7 stops saying the
  mechanical split and deepen are "wave-2 capability that does not exist yet":
  after presenting `--advise` diagnostics it offers to run `split` with the
  chosen assignment (still asking first, still never splitting silently, still
  never rewriting unit prose), and it ends by printing the ready-to-run
  `/orchestrator-deepen <plan>` command as a recommendation. Its unit template
  gains the optional `Edge cases` and `Non-goals / must-not` slots and the
  `Run:`/`Pass:` verification convention, marked as deepen's territory so a
  planning session is not obliged to fill them. `docs/orchestrator-task-map.md`
  documents that a plan may be split mechanically and what the split preserves
  byte-for-byte. `docs/orchestrator-grouping.md` documents `split` and
  `plan-check` alongside the existing zero-LLM commands, including their
  preview/non-destructive semantics.
- **Files**: `skills/orchestrator-plan/SKILL.md`,
  `docs/orchestrator-task-map.md`, `docs/orchestrator-grouping.md`
- **Symbols**: —
- **Depends-on**: u2-plan-split, u3-deepen-skill
- **Slice**: —
- **Implements / Consumes**: consumes `split-cli`, `deepen-skill`,
  `advisory-seams`
- **Verification**:
  - `skills/orchestrator-plan/SKILL.md` no longer contains the sentence
    declaring the mechanical split and deepen unavailable, and its Phase 7
    describes offering `split` after the advisory and printing the deepen
    command at the end.
  - The skill's unit template lists `Edge cases` and `Non-goals / must-not` as
    optional slots and describes the `Run:`/`Pass:` verification convention,
    stating that a planning session may leave them empty.
  - `docs/orchestrator-grouping.md` documents both `split` and `plan-check`
    with their flags, and states that `split` is non-destructive and
    `plan-check` makes no LLM or codegraph call.
  - `docs/orchestrator-task-map.md` states what a mechanical split preserves
    byte-for-byte and that no map field is rewritten by it.
  - Every command and flag named in the three documents exists in
    `orchestrate --help` output.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-plan-edit
    description: Shared verbatim plan-surgery module plus the sub-second plan-check guard command
    files:
      - orchestrator/grouping/plan_edit.py
      - orchestrator/grouping/plan_reader.py
      - orchestrator/cli.py
      - tests/test_plan_edit.py
    size_hints:
      orchestrator/grouping/plan_edit.py: medium
      tests/test_plan_edit.py: medium
    symbols: []
    depends_on: []
    implements: ["plan-edit", "plan-check-cli"]
    consumes: []
  - task_id: u2-plan-split
    description: The split command partitions a plan verbatim from an advisory seam or explicit --tasks groups
    files:
      - orchestrator/cli.py
      - orchestrator/grouping/advisory.py
      - orchestrator/grouping/plan_edit.py
      - tests/test_plan_split.py
      - tests/test_advisory.py
    size_hints:
      tests/test_plan_split.py: medium
    symbols: []
    depends_on: ["u1-plan-edit"]
    implements: ["split-cli", "advisory-seams"]
    consumes: ["plan-edit"]
  - task_id: u3-deepen-skill
    description: /orchestrator-deepen — explorer-grounded, per-group-capped grilling writing enrichment back into the plan
    files:
      - skills/orchestrator-deepen/SKILL.md
      - skills/orchestrator-deepen/explorer-prompt.md
      - orchestrator/grouping/plan_sections.py
      - tests/test_plan_sections.py
    size_hints:
      skills/orchestrator-deepen/SKILL.md: large
      skills/orchestrator-deepen/explorer-prompt.md: medium
    symbols: []
    depends_on: ["u1-plan-edit"]
    implements: ["deepen-skill"]
    consumes: ["plan-edit"]
  - task_id: u4-planning-contract
    description: /orchestrator-plan offers split after the advisory, prints the deepen command, and the contracts document both
    files:
      - skills/orchestrator-plan/SKILL.md
      - docs/orchestrator-task-map.md
      - docs/orchestrator-grouping.md
    symbols: []
    depends_on: ["u2-plan-split", "u3-deepen-skill"]
    implements: []
    consumes: ["split-cli", "deepen-skill", "advisory-seams"]
```

## Post-eval-harness backlog (do not implement now)

Carried forward from the wave-1 plan, plus what the 2026-08-29 grill added:

- **Granularity presets converge.** `--advise` on the 36-unit 2026-08-26 plan
  returned identical metrics for `independent`, `balanced`, and `monolithic`
  (29 groups, same makespan, same modularity, all pareto-dominant). Something
  is off, but with two or three good plans to test against, tuning it now is
  guesswork; the harness should measure it first.
- Hyperedge / hub-routing re-modelling of symbol-derived edges (R19b).
- dagP-style acyclicity-safe refinement pass.
- Cross-group read-overlap-aware merging.
- `target_fill_ratio` calibration for the wave-1 fill penalty.
- `--advise` cross-invocation graph cache keyed by (plan sha, index
  fingerprint), only if R14's latency is violated in practice.
- **Deepen's `Run:` command hit rate** — once the harness records reviewer
  rounds, measure how often a deepen-authored command runs clean, and whether
  the two grounding checks are the right ones.

## Requirement coverage

R16 → U1 + U2 (verbatim surgery in U1, the command and seam addressing in U2),
R17 → U3 + U4 (the skill in U3, its hand-off from `/orchestrator-plan` in U4),
R18 → U3 (question policy, enrichment template, and edge-case taxonomy all
grounded in the R19 findings already folded into the origin brainstorm),
R19 → satisfied before this plan (both reports received 2026-08-28 and recorded
in the origin's Research Findings sections; nothing to build).
R1–R15 shipped in wave 1; R20 remains out of scope pending Infinity Skills.
