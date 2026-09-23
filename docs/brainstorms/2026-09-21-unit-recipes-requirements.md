---
date: 2026-09-21
revised: 2026-09-23
topic: unit-recipes
---

> **Revision 2026-09-23.** Reviewed against the evidence in seed B and against
> `main`. Three changes: the v1 recipe set is now `code` + `run` (`research`
> and `synthesize` move to the next increment); the review-loop extraction is
> split out as its own zero-behaviour-change plan that lands *before* recipes;
> and the three questions the first draft had closed as "none" are answered
> below (R4/R13, R15, the manifest summary). Rationale in *Key Decisions*.

# Unit Recipes — a flexible pipeline beyond coding — Requirements

## Summary

Every group today runs the same machine: a coder session looping on a worktree,
optionally paired with a reviewer, committing code, merged through Preflight.
Work that is not coding — reading and interpreting sources, deciding between
alternatives, running costly jobs — is forced through that machine and fails in
characteristic ways. This work introduces the **Unit Recipe**: a named bundle
declared per plan unit that determines the prompt, tool allowlist, completion
contract, reviewer prompt, merge behaviour, pricing function and handoff prompt
used to execute it. Two recipes ship in v1: `code` (today's machine, unchanged) and `run`
(execute declared commands detached under a wall-clock cap, outputs to
`data_dirs`, measurements into the manifest, never a coder loop). `research`
and `synthesize` follow in the next increment on the same registry. A run-level
**Artifact Manifest** carries what each unit produced, so a downstream unit
receives an entry and a bounded summary rather than a payload. Recipes are
opt-in twice over — a unit that declares none runs `code` exactly as today, and
a plan that declares any requires a config flag or fails loudly. Dynamic
fan-out, the `evaluate`/`llm-batch`/`interview` recipes, and the
KPI-optimization loop are explicitly out of scope; `evaluate` is `run` plus a
required measurement contract and arrives with the KPI loop.

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

- **Two recipes in v1: `code` and `run`.** (Revised 2026-09-23; the first
  draft chose `code` + `research` + `synthesize`.) Seed B's six motivating
  failures map to recipes as follows — B6 `run`, B7 `run`, P1 `run`+measure,
  P2 measure, P4 `llm-batch`, P5 `run`. `research` was needed once across
  five runs and `synthesize` never; shipping them first would have fixed none
  of the six, including P1, which this document opens with. `run` is also the
  only recipe whose pricing is *not* file arithmetic (wall clock and expected
  output), so it is the first real consumer of R12's pricing seam, and it is
  the first brick of the KPI loop (`evaluate` = `run` + a required
  measurement). It is deliberately narrow in v1: the orchestrator itself
  executes the plan-declared commands as a detached child under a wall-clock
  cap; there is no coder session, so the worker Bash cap (B7) and the worker
  allowlist (B6) do not apply; outputs land in `data_dirs`; any measurement
  the plan asks for is written into the unit's Artifact Manifest entry as an
  *observation*, never a gate. An LLM is involved only on failure — a small
  triage session that reads the captured output and either reports a Work
  Failure with a diagnosis or asks for a decision. Verified on `main` before
  this revision: the 10-minute cap is Claude Code's own Bash-tool limit, not
  orchestrator code; nothing in the last merges removed it, and the worker
  ground rules still describe it. Passing `BASH_MAX_TIMEOUT_MS` in the worker
  env would raise the per-call cap but not stop a backgrounded task dying at
  turn end, so it is a stopgap, not the fix.

- **`research` and `synthesize` are the next increment, not v1.** Their
  rationale is unchanged and R5–R7 stay in this document so the next plan
  starts from them. Two facts made them the wrong first recipes: no web tool
  (WebSearch, WebFetch, the Perplexity MCP) is in the worker allowlist today,
  so `research` needs allowlist and confinement work of its own, and
  `api.perplexity.ai` is unreachable from the remote container. They were not
  the cheap option they looked like.

- **`synthesize` is genuinely distinct from `research`, over a shared base.**
  Their tool allowlists differ in a way that matters: a `synthesize` unit with
  web search stops being a reduce over its declared inputs and becomes a fourth
  researcher. Their contracts differ (findings-with-sources versus a decision
  record with rationale and rejected alternatives), and so do their reviewer
  prompts (source-checking versus consistency-with-declared-inputs). The two
  are separate registry entries whose shared behaviour lives in one
  well-factored base — shared code, not an inheritance mechanism built for its
  own sake.

- **The pipeline is human-planned. Agents never create nodes.** This is a
  standing design principle, not a v1 deferral waiting on machinery. A human
  plans the pipeline; the grouper partitions it deterministically; the DAG is
  snapshotted at run start. **No unit may create, spawn or expand another
  unit** — not in v1, not later, unless this principle is deliberately revisited
  and overturned.

  What a unit *may* do instead is the model the orchestrator already runs on:
  **repeat until it completes, or until it hits a declared cap — and then a
  human acts.** Rounds bounded by the round budget, spec rewrites bounded by
  `max_rewrites`, generations bounded by the generation cap; exhausting a cap is
  a Work Failure, which is terminal by design precisely so a human looks at it
  via Retry. Every recipe inherits that model unchanged (R10, R18). A research
  unit that finds three candidate strategies does not spawn three
  implementation units; it writes them into its artifact, and a human re-plans.

  The prior art supports this rather than merely permitting it. Every published
  mechanism for runtime expansion — Airflow dynamic task mapping, Argo
  `withParam`, Flyte `map_task`, LangGraph `Send`, Dagster `DynamicOut`,
  Metaflow `foreach` — creates *instances of nodes that already exist in the
  plan*; only GitLab child pipelines and Temporal child workflows permit an
  arbitrary new subgraph, and both push the entire admission problem onto the
  caller. The documented failure modes of letting an LLM shape its own
  downstream pipeline are unbounded decomposition, plans that are
  schema-valid but operationally nonsense, nondeterministic retry silently
  changing the work list, and re-planning loops — and the platform caps that
  exist (Airflow's 1,024 map length, Argo's depth 100, LangGraph's 25
  supersteps) exist to protect schedulers and event histories, not budgets.
  A human-planned DAG makes all of that moot rather than manageable.

  This is also why the artifact story matters so much (R16): with no automatic
  expansion, the Artifact Manifest is how one node's findings reach the next
  node and the human, and it is the only handoff channel that needs to work.

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
  **R4 and R13 do not conflict** (checked 2026-09-23): the `code` completion
  contract is already `CoderReport`, a pydantic model in `orchestrator/model.py`
  validated on every round today. R13 names that existing behaviour as the
  rule and requires each new recipe to bring its own model; `code` gains no
  new validation.

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

- **The extraction is its own plan, and it splits the file before it moves
  anything.** (Added 2026-09-23.) `review.py` is one 1,993-line
  `_GroupExecution` class with ~60 methods and 34 test files importing it;
  the same class owns generation lifecycle, round accounting, heartbeat and
  Sign of Life, escalation, merge and the Preflight ladder, rewrite/relaunch/
  retire, surprises, handoff and session records. Pulling the shared plumbing
  out of that in the same change that introduces a dispatcher is how a
  behaviour change slips through. So the sequence is:

  1. **Plan A — split `review.py` by responsibility, zero behaviour change.**
     Target modules, following the seams already visible in the method list:
     `generation.py` (run / `_run_generation*` / re-entry / retire / handoff),
     `rounds.py` (round accounting, breaker, `_review_round`),
     `merge_ladder.py` (`_merge`, untracked/flake/conflict handling, scratch
     archiving), `escalation.py` (`_escalate`, `_approve_gate`,
     `_resolve_needs_input`, stuck/hard/content-filter handlers, Operator
     Decision ledger), `surprises.py` (`SurpriseBoard`, residue) and
     `sessions_record.py` (`_record`, usage copies, transcript watch).
     Acceptance is mechanical: the full suite passes with no test edited
     other than import paths, and a replay of one recorded run produces the
     same run log. **This is the measurable beginning.**
  2. **Plan B — registry, dispatcher, `run`, pricing, manifest.** Built on the
     split modules, where "shared plumbing" is now a set of files a second
     executor imports rather than methods it has to be prised away from.
  3. **Validate on a second repository** (`learning_podcast`, whose renders
     and judge passes are the P1/B6/B7 evidence) before `research` and
     `synthesize` are planned.

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

### The v1 recipes — and the next increment

- R4. `code-unchanged` — **The `code` recipe is today's machine, unchanged.**
  Its prompt, tool allowlist, reviewer behaviour, merge path, pricing and
  generation handling are behaviour-identical to the current implementation. A
  run of an existing plan produces the same groups, the same prompts and the
  same merges as before this work.

- R5a. `run-recipe` — **`run` executes plan-declared commands detached and
  reports what happened.** (Added 2026-09-23.) A `run` unit declares its
  commands, an expected wall-clock per command, the `data_dirs` paths it
  writes, and optionally the measurements to extract from its output. The
  orchestrator launches each command as a detached child it owns — recorded
  like a worker child so a crash-and-resume can re-adopt or kill it — under
  the declared wall-clock cap, streams output to the group's artifacts, and
  writes exit status, duration, output paths and measurements into the
  unit's Artifact Manifest entry. No coder session runs; the worker Bash cap
  and allowlist do not apply. A measurement is an observation and never
  gates completion (seed B P2). On a non-zero exit or a cap hit, one bounded
  LLM triage step reads the captured output and either reports a Work
  Failure with a diagnosis or raises an escalation; it never relaunches on
  its own. Pricing (R12) is by declared wall-clock and expected output size,
  not files; two `run` units are never merged into one group.

- R5. `research-recipe` (next increment) — **`research` performs web research and produces a
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

- R6. `synthesize-recipe` (next increment) — **`synthesize` reduces several declared artifacts
  into a decision record.** It reads the Artifact Manifest entries it declares
  as inputs, and emits a decision record naming the decision, the rationale with
  each claim tied to a specific input artifact, the rejected alternatives with
  reasons, and any open questions. **It has no web search**: its inputs are the
  artifacts it declares, and reaching outside them makes it a researcher rather
  than a reduce. Its output is committed and registered like a research
  artifact.

- R7. `recipe-base` (next increment) — **`research` and `synthesize` share one factored base.**
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
  identically. `run` prices from its declared wall-clock and expected output size; the
  document recipes (next increment) price from a size class declared on the
  unit, reusing the existing `small`/`medium`/`large` vocabulary rather than
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
  `ReviewIntensity` still decides whether a reviewer runs.** `run` has no
  reviewer session at any intensity — its output is a mechanical record;
  `research`'s reviewer checks that claims are supported by the sources cited;
  `synthesize`'s checks that the decision follows from the declared input
  artifacts. Intensity semantics, tiers and the Preflight gate are unchanged.

- R15. `recipe-merge` — **Merge behaviour is per-recipe.** `code` merges as
  today. `research` and `synthesize` get a worktree, commit their document, and
  merge through the same Preflight gate and conflict ladder — their output is
  repo content and belongs in history. The recipe declares which side effects
  it may produce, and a recipe producing an effect it did not declare is an
  error rather than a silent merge. **Detection is `git status --porcelain`
  against the recipe's declared path globs** — the enumeration Preflight's
  untracked ladder already performs — so a `run` unit that touches tracked
  source, or a document recipe that writes outside its declared directory,
  fails the merge naming the paths. Writes into `data_dirs` are outside git
  and shared live between groups, so they are *declared* (the recipe lists
  which data directories it may write) but not *enforced* in v1; enforcing
  them needs per-recipe Landlock profiles, which is later work.

### Artifacts and export

- R16. `artifact-manifest` — **A run-level Artifact Manifest records every
  declared output.** It lives in the Run Directory alongside the other run
  artifacts, and each entry carries the artifact id, path, sha256, producing
  unit and recipe, declared schema, a bounded summary, and a status that
  distinguishes a complete artifact from a partial one — so an artifact
  committed by a Resolve from a failed group is registered as partial rather
  than read as authoritative. A downstream unit's prompt receives the entries
  it declares as inputs, not the file bodies; the unit opens a file only when
  it needs the detail. **The summary is written by the producing unit as a
  required field of its completion contract, capped by schema at 2,000
  characters** (roughly 500 tokens, the low end of the research's 500–2,000
  token handoff-packet band); a report whose summary is missing or over the
  cap fails R13 validation, so no manifest entry is ever registered without
  one. For `run`, the summary is generated by the orchestrator from the
  captured exit status, duration and measurements, since no LLM session
  writes a report.

- R17. `bundle-additive` — **Export changes are additive and do not bump the
  Run Bundle version.** The Artifact Manifest is exported as a new optional key
  in the Run Bundle, and `SessionRole` gains `runner` (the `run` triage step) in v1 and
  `researcher`/`synthesizer` with the next increment, so session attribution
  and per-role cost tracking stay honest. `schema_version`
  remains 2, and a consumer that does not know the new role values must not
  break on them.

### Bounded repetition

- R18. `repeat-then-human` — **A unit repeats until it completes or hits a cap;
  a cap then hands control to a human.** Every recipe is bounded by the same
  three dials the orchestrator already enforces — the round budget, the spec
  rewrite cap, and the generation cap — and exhausting any of them is a Work
  Failure, terminal by design so an operator looks at it via Retry. No recipe
  introduces an unbounded loop, a "run until satisfied" mode, or a
  self-extension path. **No unit may create, spawn or expand another unit under
  any circumstances**, and a recipe whose output attempts to declare new work is
  an error naming the unit, not an instruction the orchestrator follows.

## Non-Goals

- **Any form of automatic node creation.** No unit may spawn downstream units,
  emit a work manifest the orchestrator executes, or change the run's DAG. This
  is a standing design principle (see Key Decisions), not a v1 limitation: the
  human plans the pipeline, and a node that cannot finish escalates to a human
  rather than growing the graph. The DAG is computed up front and snapshotted
  at run start, as today.

- **The `evaluate`, `llm-batch` and `interview` recipes.** Each needs
  machinery this work does not build — a required measurement contract with
  keep/revert semantics, shard/schema/retry handling, or typed human gates
  with replay-safe resume. `evaluate` is `run` plus that contract and ships
  with the KPI loop.

- **`research` and `synthesize` in v1.** They are the next increment on the
  same registry; R5–R7 are kept here as their starting point.

- **A dollar cap on `run`.** v1 caps wall clock only; the plan declares an
  expected duration and, where known, a cost figure, which the deepen skill
  surfaces to the human. An automatic cost gate arrives with `llm-batch`.

- **The KPI-optimization loop.** Starting a coder from working code and
  optimizing a measured metric is its own brainstorm, already scoped.

- **The planning-skill checks P3, P6, P7 and P8** from the seed document. They
  do not depend on recipes and get their own small plan.

- **An Observatory surface for the Artifact Manifest.** The file lands in the
  Run Directory, which the Observatory can read, but no UI work is in scope.

- **Cross-run artifact queries.** The manifest is per-run.

- **Recipe inheritance as a general mechanism.** R7 is shared implementation
  between two entries, not a feature for expressing recipe hierarchies.

- **Loosening worker confinement.** Nested `claude` sessions stay unavailable
  to workers; no recipe requires them.

## Open Questions

Resolved 2026-09-23 (see Key Decisions): R4 versus R13, how R15 detects an
undeclared side effect, and who writes and bounds the manifest summary.

Still open, for Plan B's grilling:

- **`run` failure triage.** Whether the on-failure LLM step is a fresh small
  session or the run driver itself. The driver already triages escalations;
  a separate session is more observable but costs a launch.
- **Detached-child ownership across a crash.** `LivePid` records pid and
  starttime for worker children; a `run` child needs the same record so a
  resume after a crash can re-adopt or kill it rather than relaunch a render
  that is still running.
- **Per-recipe Landlock.** Whether `run` executes under the worker profile,
  a wider one, or none; the plan-declared command is trusted by the human who
  wrote the plan, but the Landlock profile is what makes that trust bounded.

## Next Step

1. `/orchestrator-plan` for **Plan A** — the zero-behaviour-change split of
   `orchestrator/execution/review.py` (R9's precondition). Acceptance: full
   suite green with only import-path edits in tests.
2. Then `/orchestrator-plan docs/brainstorms/2026-09-21-unit-recipes-requirements.md`
   for **Plan B** — R1–R4, R8–R18 with `run` as the second recipe.
3. Run Plan B's result against `learning_podcast` before planning
   `research`/`synthesize` (R5–R7).
