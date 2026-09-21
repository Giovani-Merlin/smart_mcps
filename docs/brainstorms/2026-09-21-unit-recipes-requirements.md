---
date: 2026-09-21
topic: unit-recipes
---

# Unit Recipes — a flexible pipeline beyond coding — Requirements

## Summary

Every group today runs the same machine: a coder session looping on a worktree,
optionally paired with a reviewer, committing code, merged through Preflight.
Work that is not coding — reading and interpreting sources, deciding between
alternatives, running costly jobs — is forced through that machine and fails in
characteristic ways. This work introduces the **Unit Recipe**: a named bundle
declared per plan unit that determines the prompt, tool allowlist, completion
contract, reviewer prompt, merge behaviour, pricing function and handoff prompt
used to execute it. Three recipes ship: `code` (today's machine, unchanged),
`research` (web research producing a findings artifact) and `synthesize`
(reducing several prior artifacts into a decision record). A run-level
**Artifact Manifest** carries what each unit produced, so a downstream unit
receives an entry and a bounded summary rather than a payload. Recipes are
opt-in twice over — a unit that declares none runs `code` exactly as today, and
a plan that declares any requires a config flag or fails loudly. Dynamic
fan-out, the `evaluate`/`run`/`llm-batch`/`interview` recipes, and the
KPI-optimization loop are explicitly out of scope.

## Problem Frame

Six failures across five `learning_podcast` runs were the coder-and-reviewer
machine being used for work that was not coding
(`docs/ideation/2026-09-15-B-agent-kinds-and-plan-checks.md`). The sharpest is
P1: two units whose file lists were two markdown files were merged as "tiny",
and the resulting coder overflowed at 272,048 tokens against a 250,000 budget,
because the real work was six renders, twelve judge passes and three EPUB
builds. The estimator prices a task as `source_bytes / bytes_per_token + file_count × per_file_tool_allowance` (`orchestrator/grouping/estimator.py:55`)
— it is entirely file-based, so work that reads the web or runs commands prices
at essentially zero. Patching each symptom individually (a wider allowlist, a
timeout exception, a plan-skill rule) treats the symptom; letting a unit declare
what kind of work it is, and giving each kind its own machine, treats the cause.

Prior art was surveyed before committing to this design, and two independent
investigations — one via Perplexity Deep Research, one via a WebSearch agent,
assessing largely disjoint sets of systems — reached the same conclusion: no
published system offers a pluggable unit-type registry that simultaneously owns
the prompt, tool allowlist, completion contract, review mode, merge behaviour
and budget model. Close relatives exist. Block Goose recipes bundle prompt,
extensions, model, a `response` schema and `retry` checks into one registerable
file. LangChain `deepagents` registers `SubAgent` entries carrying
`system_prompt`, `mode`, `tools`, `model`, `interrupt_on`, `permissions` and
`response_format`, dispatched through a single `task()` tool. Argo Workflows
treats a template as a tagged union and exposes a `plugin` type as its
third-party extension point; Flyte carries a `task_type` string into a backend
plugin registry with an unstructured `custom` field. Three things were found
nowhere: per-recipe merge behaviour, a machine-readable cost estimate gating an
LLM step, and review mode as a per-type field. Those are this design's own
contribution, and the research is recorded under `.orchestrator/` (ten
documents, of which seven are source-cited).

The substrates this needs mostly exist. The scheduler already treats the
executor as opaque — `Executor = Callable[[GroupContext], Awaitable[GroupState]]`
(`orchestrator/execution/scheduler.py:281`) has exactly one construction site
(`orchestrator/cli.py:2286`). `WorkspaceConfig.data_dirs`
(`orchestrator/config.py:610`) already symlinks shared directories into every
worktree outside git. Generation retirement and handoff already restart a
session from a summary of existing work. `ReviewIntensity` already decides
whether a reviewer session runs at all. What is missing is the declaration, the
registry, and the pricing that makes non-code work visible to the grouper.

## Key Decisions

- **The term is "Unit Recipe", and prose never says bare "recipe".** `kind` is
  taken by Preflight Kind; `machine` is taken by machine suspend and wake, on
  which Suspend Cure depends; `role` is taken by `SessionRole`
  (base/coder/reviewer); `executor` is taken by the scheduler's `Executor`
  seam. `recipe` has zero occurrences in `orchestrator/`, is Block Goose's own
  word for this exact bundle, and reads correctly as a fillable template.
  Rejected: `profile` (no prior-art lineage, weak fit for something carrying a
  prompt), `lane` (says nothing about templating), `agent` (11 loose uses
  already, and `code` is a category of work rather than of agent).

- **The Unit Recipe is declared on the unit, not the group.** Units are what a
  plan writes and what `depends_on` connects; groups are a derived partition.
  Every prior-art system types at node level (Argo templates, Flyte
  `task_type`, Airflow operator subclasses). The task map
  (`docs/orchestrator-task-map.md`) is therefore the carrier, and because
  unlisted keys are a hard error there, this is a versioned contract change
  with `parse_task_map()` on the other side — not an additive tweak.

- **Three recipes in v1: `code`, `research`, `synthesize`.** Two would leave
  the fan-in half of a research pipeline undesigned — parallel research with
  nothing to combine it. All seven would pull in detached execution with
  wall-clock and dollar caps (`run`), shard/schema/retry machinery
  (`llm-batch`) and a measurement contract that does not yet exist
  (`evaluate`). Three is the smallest set that runs a real research →
  decide → implement pipeline end to end.

- **`synthesize` is genuinely distinct from `research`, over a shared base.**
  Their tool allowlists differ in a way that matters: a `synthesize` unit with
  web search stops being a reduce over its declared inputs and becomes a fourth
  researcher. Their contracts differ (findings-with-sources versus a decision
  record with rationale and rejected alternatives), and so do their reviewer
  prompts (source-checking versus consistency-with-declared-inputs). The two
  are separate registry entries whose shared behaviour lives in one
  well-factored base — shared code, not an inheritance mechanism built for its
  own sake.

- **Dynamic fan-out is deferred.** A research unit cannot spawn downstream work
  in v1. Doing it safely requires a validated work manifest, a server-side
  template allow-list, server-side cost re-estimation, atomic budget
  reservation, immutable manifest versioning, and the replay discipline that a
  retry must replay the recorded manifest rather than re-prompt a
  nondeterministic planner. That is a subsystem and a plan of its own; the
  research was unambiguous that treating the planner as trusted is how people
  got hurt. Recipes are valuable on the existing static DAG.

- **A group is single-recipe, as a hard invariant.** A `Group`
  (`orchestrator/model.py:64`) has one spec, one intensity and one task list,
  and the executor runs one machine for the whole group — a mixed group has no
  coherent machine to run. The grouper contracts by recipe exactly as it
  already contracts by slice, and a mixed group fails loudly naming the units.
  The rejected alternative — letting `code` absorb non-code units — reproduces
  P1 directly.

- **The recipe owns pricing.** A per-recipe pricing function is the seam that
  `run` (wall-clock and dollars) and `llm-batch` (per-shard cost) will need and
  that file arithmetic cannot express. `code` keeps today's arithmetic
  untouched. Rejected: a single shared unit-level size class (enough for three
  recipes, but a seam we would have to add anyway) and a flat per-recipe
  constant (no per-unit control at all).

- **Verification is schema-always plus the existing intensity dial.** The
  completion contract is validated mechanically on every unit regardless of
  recipe — free, and it closes MAST's task-verification bucket, which is where
  heterogeneous multi-agent systems fail silently. Whether a reviewer *session*
  also runs stays `ReviewIntensity`'s decision; the recipe supplies only which
  reviewer prompt is used. Rejected: a separate per-recipe review-mode field,
  which would create two overlapping dials in v1 with no rule for which wins.

- **The executor seam is a dispatcher, and the shared plumbing is extracted
  first.** `make_executor` reads the group's recipe and returns one of N
  executors; the scheduler never learns recipes exist. The honest cost is that
  round accounting, heartbeat and Sign of Life, escalation and Artifact
  Manifest updates must be lifted out of the 1,993-line
  `orchestrator/execution/review.py` into a layer all executors share, or the
  new executors start life without liveness parity — reintroducing precisely
  the bug the stall-detection work just fixed. **This extraction is the primary
  risk in the whole plan**: it touches the file we least want to destabilise,
  and it must leave `code`-recipe behaviour identical.

- **The Artifact Manifest is a real run-level index, not a convention.** A
  downstream unit's prompt receives the entry and a bounded summary; it opens
  the file only when it needs detail. Rejected: folding the record into each
  recipe's completion contract (cheaper, but leaves nothing queryable across
  the run) and a path convention alone (zero machinery, but nothing bounds what
  a downstream unit pulls into context — the exact failure this is meant to
  prevent).

- **Export changes are additive only.** `docs/run-bundle-contract.md` states
  that a new optional key or a new enum value is free and does not bump the
  version. The Artifact Manifest rides Run Bundle v2 as a new optional key and
  `SessionRole` gains two values; **no `schema_version = 3`**.

## Requirements

### The Unit Recipe contract

- R1. `recipe-field` — **A unit declares its Unit Recipe in the task map.** The
  task map gains an optional `recipe` key naming a registered Unit Recipe,
  which mints `orchestrator-task-map v2`; `parse_task_map()` accepts both
  versions, and a v1 map behaves exactly as today. An unknown recipe name is a
  hard error naming the unit and listing the registered names. A unit that
  declares no recipe is a `code` unit.

- R2. `recipe-registry` — **A registry maps a recipe name to its bundle.**
  One registered entry supplies: prompt template, tool allowlist, completion
  contract schema, reviewer prompt, merge behaviour, pricing function, handoff
  prompt, and a `custom` mapping the core never interprets (Flyte's escape
  hatch, so a later recipe can carry configuration without a core change).
  Registration is by name, and the registry is the single place the set of
  recipes is enumerated.

- R3. `recipe-gate` — **Recipes are opt-in twice, and never silently.** A unit
  declaring no recipe runs `code` with behaviour identical to today, so every
  existing plan is untouched. A config flag must additionally be enabled before
  any non-`code` unit will run: a plan declaring recipes against a
  configuration without the flag fails loudly, naming the units and the flag,
  rather than degrading to `code`.

### The three v1 recipes

- R4. `code-unchanged` — **The `code` recipe is today's machine, unchanged.**
  Its prompt, tool allowlist, reviewer behaviour, merge path, pricing and
  generation handling are behaviour-identical to the current implementation. A
  run of an existing plan produces the same groups, the same prompts and the
  same merges as before this work.

- R5. `research-recipe` — **`research` performs web research and produces a
  findings artifact.** It reads sources, writes a findings document under
  `docs/research/`, and registers it in the Artifact Manifest. Its completion
  contract requires every substantive claim to carry a source reference. Its
  tool surface prefers the repo's Perplexity integration and falls back to the
  built-in web tools when Perplexity is unavailable — **and the fallback is
  recorded in the run log and in the unit's Artifact Manifest entry, never
  silent**, because an unreachable provider degrading quietly is
  indistinguishable from a provider that was never configured. It may not spawn
  nested `claude` sessions; worker confinement governs writes, not reads or
  network, so web access needs no change to `confinement.py`.

- R6. `synthesize-recipe` — **`synthesize` reduces several declared artifacts
  into a decision record.** It reads the Artifact Manifest entries it declares
  as inputs, and emits a decision record naming the decision, the rationale with
  each claim tied to a specific input artifact, the rejected alternatives with
  reasons, and any open questions. **It has no web search**: its inputs are the
  artifacts it declares, and reaching outside them makes it a researcher rather
  than a reduce. Its output is committed and registered like a research
  artifact.

- R7. `recipe-base` — **`research` and `synthesize` share one factored base.**
  Behaviour common to artifact-producing recipes — reading declared input
  entries, writing an artifact, registering it, and the document-merge path —
  lives in one place that both entries use, and that a later recipe can use.
  The two remain distinct registry entries with distinct tools, contracts and
  reviewer prompts; this is shared implementation, not a recipe-inheritance
  feature.

### Execution

- R8. `executor-dispatch` — **`make_executor` dispatches on the group's
  recipe.** It returns the executor registered for that recipe; the scheduler's
  `Executor` type and `GroupContext` are unchanged, and the scheduler never
  learns recipes exist. `Group` carries the recipe its units share, set by the
  grouper under R11.

- R9. `shared-plumbing` — **Round accounting, liveness, escalation and manifest
  updates are extracted before dispatch exists.** Round accounting and the
  round budget, heartbeat and Sign of Life reporting, escalation raising and
  answering (including the Operator Decision ledger), stranded-work handling and
  Artifact Manifest updates move out of the review loop into a layer every
  executor shares. Every recipe gets liveness parity — a wedged `research` unit
  reports Not Live on the same evidence a wedged coder does. **The extraction
  must leave `code`-recipe behaviour identical**, demonstrated against the
  existing tests before any new executor is written.

- R10. `recipe-generations` — **Every recipe retires and forks generations,
  with its own handoff prompt.** A `research` or `synthesize` session whose
  context fills is retired and a fresh generation resumes from a handoff, using
  the recipe's own handoff prompt — for `research`, what has been found, which
  sources are exhausted, and what remains open. Generation caps, re-entry and
  Warm Resume are unchanged.

### Grouping and pricing

- R11. `single-recipe-groups` — **A group holds units of exactly one recipe.**
  The partitioner contracts units by recipe as it already contracts slice-mates,
  so the invariant holds through every later stage rather than only through the
  initial partition. A partition that would place two recipes in one group
  raises an error naming the group, the recipes and the offending units. The
  group records its recipe.

- R12. `recipe-pricing` — **Each recipe prices its own units.** The estimator
  calls the recipe's pricing function instead of assuming file arithmetic.
  `code` supplies today's arithmetic unchanged, so existing plans price
  identically. `research` and `synthesize` price from a size class declared on
  the unit, reusing the existing `small`/`medium`/`large` vocabulary rather than
  inventing a second one. A non-`code` unit with no declared size takes the
  recipe's documented default, and the default is recorded in the grouping
  trace so an unpriced unit is visible rather than invisible.

### Verification and merge

- R13. `completion-contract` — **Every recipe declares a completion contract,
  and it is validated mechanically on every unit.** The contract is a schema,
  not prose. A unit whose final report does not satisfy its recipe's contract
  is not complete, and the validation failure names the violated field. This
  validation runs regardless of `ReviewIntensity`, including for `self_verify`
  groups.

- R14. `recipe-review` — **The recipe supplies the reviewer prompt;
  `ReviewIntensity` still decides whether a reviewer runs.** `research`'s
  reviewer checks that claims are supported by the sources cited;
  `synthesize`'s checks that the decision follows from the declared input
  artifacts. Intensity semantics, tiers and the Preflight gate are unchanged.

- R15. `recipe-merge` — **Merge behaviour is per-recipe.** `code` merges as
  today. `research` and `synthesize` get a worktree, commit their document, and
  merge through the same Preflight gate and conflict ladder — their output is
  repo content and belongs in history. The recipe declares which side effects
  it may produce, and a recipe producing an effect it did not declare is an
  error rather than a silent merge.

### Artifacts and export

- R16. `artifact-manifest` — **A run-level Artifact Manifest records every
  declared output.** It lives in the Run Directory alongside the other run
  artifacts, and each entry carries the artifact id, path, sha256, producing
  unit and recipe, declared schema, a bounded summary, and a status that
  distinguishes a complete artifact from a partial one — so an artifact
  committed by a Resolve from a failed group is registered as partial rather
  than read as authoritative. A downstream unit's prompt receives the entries
  it declares as inputs, not the file bodies; the unit opens a file only when
  it needs the detail.

- R17. `bundle-additive` — **Export changes are additive and do not bump the
  Run Bundle version.** The Artifact Manifest is exported as a new optional key
  in the Run Bundle, and `SessionRole` gains `researcher` and `synthesizer` so
  session attribution and per-role cost tracking stay honest. `schema_version`
  remains 2, and a consumer that does not know the new role values must not
  break on them.

## Non-Goals

- **Dynamic fan-out.** No unit may spawn downstream units, emit a work
  manifest, or change the run's DAG. The DAG is computed up front and
  snapshotted at run start, as today.
- **The `evaluate`, `run`, `llm-batch` and `interview` recipes.** Each needs
  machinery this work does not build — a measurement contract, detached
  execution with wall-clock and dollar caps, shard/schema/retry handling, or
  typed human gates with replay-safe resume.
- **The KPI-optimization loop.** Starting a coder from working code and
  optimizing a measured metric is its own brainstorm, already scoped.
- **The planning-skill checks P3, P6, P7 and P8** from the seed document. They
  do not depend on recipes and get their own small plan.
- **A cost-estimate approval gate.** No recipe estimates dollars or blocks on
  an approval threshold in v1; that arrives with `run` and `llm-batch`, which
  are the recipes that need it.
- **An Observatory surface for the Artifact Manifest.** The file lands in the
  Run Directory, which the Observatory can read, but no UI work is in scope.
- **Cross-run artifact queries.** The manifest is per-run.
- **Recipe inheritance as a general mechanism.** R7 is shared implementation
  between two entries, not a feature for expressing recipe hierarchies.
- **Loosening worker confinement.** Nested `claude` sessions stay unavailable
  to workers; no recipe requires them.

## Open Questions

None. Every question raised during the brainstorm was resolved into a decision
above or an explicit non-goal. One item is a known risk rather than an open
question: R9's extraction from `orchestrator/execution/review.py` must preserve
`code`-recipe behaviour exactly, and whether it can be done cleanly is an
empirical question the plan must answer before R8 depends on it.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-09-21-unit-recipes-requirements.md`.
