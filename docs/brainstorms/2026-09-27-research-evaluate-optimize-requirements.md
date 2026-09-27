---
date: 2026-09-27
topic: research-evaluate-optimize
---

# Research, Evaluate and the KPI loop — the next Unit Recipes — Requirements

## Summary

Unit Recipes v1 shipped `code` and `run` and a registry to hang more on. This
work adds three entries: `research` (a worker session that grounds a question
in the codebase, reads Perplexity and the web, and commits a Findings Artifact
whose every finding carries a source), `evaluate` (`run` plus a KPI Contract:
the command's measurements JSON names one objective key, its direction, a
minimum effect and guard metrics, behind a hash-checked Evaluation Harness),
and `optimize` (a coder-and-reviewer group whose every round proposes one
candidate, scores it with an evaluate child, and lets the orchestrator — never
the coder — decide keep, promising, inconclusive, discard or crash against the
Champion, recording every attempt in an Attempt Ledger). A document unit may
refine the spec of the downstream unit it feeds; no unit ever creates one. Two
live runs validate the increment in order: an analysis pipeline in
infinity-skills over five ingested orchestrator runs, then a grouping-quality
loop in smart_mcps whose KPI is cross-group read-set overlap on stored runs.
`synthesize`, NotebookLM, a dollar cap, an orchestrator-owned judge and any
Infinity Skills ingestion change are out.

## Problem Frame

The recipe machinery exists and has survived two live runs
(`r20260924-134934`, `r20260925-101742`), but every recipe still either codes
or runs a command. Two kinds of work the human now wants to automate do
neither. First, *analysis*: the orchestrator's own runs have grown to fifteen
in this repo alone, their transcripts are too long to review by hand even
through Infinity Skills' summaries, and nobody knows which errors repeat
because of the orchestrator or which groups read the same files and could have
shared a context. Second, *optimisation*: the grouper's quality is measured
today by a Grouping Scorecard of the partition's shape
(`orchestrator/grouping/scorecard.py:32`) and never by what the coders later
read; improving it is a loop of "change the grouper, re-partition stored plans,
compare" that a coder session cannot run honestly, because the same session
would write the metric and the change.

The prior art was researched before this brainstorm and is recorded in
`docs/research/2026-09-21-kpi-optimization-prior-art.md` and
`docs/research/2026-09-21-agent-pipeline-prior-art.md`. Its load-bearing
findings: Karpathy's `autoresearch` found ~20 keeps in ~650 experiments (a
~3% hit rate, so at 20 evaluations a working loop finds nothing ~50% of the
time), its `lower → keep` rule accepted a random-seed change as its final
improvement, and its protections were enforced by nothing. The 2026
reward-hacking literature shows prompting is not a mitigation (~6pp) while
structural controls are. Nobody in open source runs construction and KPI
optimisation as separate recipes over git worktrees with the orchestrator
deciding keep-or-revert; that separation is this design.

Two in-house facts shape the first use case. Infinity Skills already has a
mature judge-driven prompt-tuning loop (`eval/summary_prompt_tuning.py`:
pairwise position-swapped judge calls, a 1–5 rubric with per-field `why`, a
multi-objective `keep_variant` gate, tabu normalisation of tried variants,
disk-cached LLM calls, a live-production baseline arm, a human promotion gate).
Its *patterns* are what an LLM-judge harness should copy; its *loop* stays
where it is. And the grouping KPI needs no Infinity Skills at all: every coder
transcript carries the `Read`/`Edit` paths per group, and the report already
derives `touched_files` per group (`orchestrator/report/facts.py:684`).

## Key Decisions

- **One requirements document, three recipes.** The 2026-09-23 revision split
  the KPI loop into its own brainstorm because its accept criterion and trust
  boundary differ from construction's. Both still differ, but the registry,
  dispatcher, Run Child and Artifact Manifest now exist, so `evaluate` is `run`
  plus a contract and `optimize` is `code` plus a policy; planning them apart
  from `research` would duplicate the shared plumbing work. Rejected: two
  documents (the human's call: the skeleton is in place).

- **`research` is a worker session, not a one-shot.** A one-shot cannot ground
  a question in codegraph, follow Perplexity's answer into the web, and write a
  reviewed document; the existing `perplexity-explorer` agent brief already
  does the first two and is the prompt's starting point. Generations, handoff
  and Warm Resume apply unchanged (R10 of the origin brainstorm). Rejected: a
  `run`-like one-shot (unobservable, unreviewable); reusing the
  `plan-to-plan`/`apply-research-plan` skills' file contract as the artifact
  (an input format for a human-driven flow, not a schema).

- **Repo-only research is not a recipe.** A question answerable from the code
  is the Explore agent's job inside a coder session; `research` exists for
  external knowledge applied to this codebase.

- **Sources are enforced by schema, support by the reviewer.** A finding with
  no source reference fails the completion contract mechanically (R13 of the
  origin, applied); whether the source actually supports the claim is the
  reviewer prompt's job, and `ReviewIntensity` still decides whether that
  reviewer runs.

- **A document unit may refine its consumer's spec; it may never create a
  node.** The human chose this over both extremes. The refinement travels as
  a field of the Findings Artifact, is applied through the existing
  spec-rewrite path to the unit the plan's `depends_on` names, and is refused
  for any other unit. ADR 0010 stands: this is the same "unit repeats or a
  human re-plans" model with one bounded edit allowed.

- **`synthesize` is deferred.** Across five learning_podcast runs it was
  needed zero times, and the analysis pipeline's reduce step is covered by a
  `research` unit that reads upstream manifest entries and refines one
  consumer. It lands on the same registry when a pipeline needs N→1 reduction
  its consumer cannot do. Rejected: shipping it now on the shared base (a
  recipe with no live consumer); shipping it *instead of* research (the
  analysis needs external knowledge).

- **`evaluate` is `run` plus a KPI Contract, and the measurements JSON is the
  only channel.** The orchestrator never computes a metric or calls a judge.
  An LLM-as-judge KPI is a harness script that makes the LLM calls (pairwise,
  position-swapped, cached, against a live-production baseline arm — Infinity
  Skills' patterns) and writes the same JSON. One accept rule then covers
  scripts and judges, and the judge lives inside the protected harness where
  the optimizer cannot reach it. Rejected: an orchestrator-owned judge mode
  (two code paths; judge cost becomes orchestrator-owned; the judge prompt
  leaves the harness).

- **The harness is built or named before the loop, by a different unit.** The
  human-planned unit that builds the scoring script, held-out inputs, seeds and
  budget constants merges first; the optimize unit's `files:` exclude those
  paths and the evaluate child hash-checks them before every scoring run. A
  plan may point at an existing harness (drummAI's eval scripts) and only
  declare the paths. Rejected: the optimizer writes its metric (the
  CUDA-Engineer and issue #466 failure, verbatim).

- **The loop lives inside one group.** An `optimize` group's rounds are: coder
  commits one candidate → evaluate child scores it → orchestrator decides →
  worktree reset or Champion advanced → next round with the ledger in the
  prompt. Rejected: unrolling `code → evaluate → code` at plan level (the
  human writes N units by hand and the keep-or-revert decision has no owner).

- **The orchestrator decides keep-or-revert from numbers; the coder never
  does.** Accept requires the KPI delta to clear both the declared minimum
  effect and twice the noise floor, where the noise floor is the median
  absolute deviation over the last five scored deltas — zero extra
  evaluations. A deterministic KPI (the grouping replay) has a zero noise
  floor and reduces to the minimum-effect rule; a confirmation run is required
  only when the noise floor is non-zero. Guards may not worsen beyond their
  declared bound. Rejected: `lower → keep` (autoresearch's proven failure);
  n≥3 repeats per candidate (triples the cost at this budget).

- **A zero-hit loop completes.** Expected hits at 20 evaluations are under
  one; a loop that finds nothing and leaves a ledger of what was ruled out has
  done its job. The group is COMPLETED, its artifact is the Attempt Ledger,
  nothing merges. Rejected: Work Failure on zero hits (a human looks at every
  correct outcome).

- **All anti-cheating controls ship in v1.** The human was explicit: the loop
  is worthless unless a keep is trustworthy. Mutable region bounded by
  `files:`; harness paths hash-checked; held-out inputs the coder never sees;
  evaluate runs as a separate confined Run Child; a reviewer with a second job
  ("not tried before, no harness path or seed or budget line touched") on
  every `promising` candidate; production-aligned worker model as today.
  Rejected: prompt-only mitigation (~6pp in BAITBENCH).

- **What was tried reaches the coder through the prompt; no embeddings.** The
  Attempt Ledger table is folded into every round's prompt and the reviewer
  checks for semantic repeats. Rejected: an orchestrator novelty check via
  embeddings or an LLM call per round (uncosted, and premature at 20
  evaluations).

- **v1 dials.** Evaluation cap and per-evaluation wall clock are
  plan-declared; a good-enough threshold is optional; patience (4
  non-improving evaluations) and the consecutive-revert cap (3) are config
  defaults; hitting either escalates rather than stops, since at this budget
  "converged" and "stuck" are indistinguishable and a human sentence is cheap.
  Crashes count against the round budget, not the evaluation cap. Rounds,
  rewrites and generations keep their existing caps.

- **Evaluation inputs are frozen, never live.** The grouping harness replays
  `group` on stored plans against cached task graphs, never a live codegraph
  index — the same commit has produced three index fingerprints in fifteen
  minutes (`grouper-index-fingerprint-drift`), which would make every score
  noise. This generalises: an Evaluation Harness declares its inputs and they
  are content-hashed with it.

- **Infinity Skills is a data source, not a consumer, in this increment.**
  The analysis pipeline ingests five old runs through the existing `export` +
  `ingest-run` path; nothing in Infinity Skills changes to read the ledger or
  the new session roles. Rejected: extending the Framework Adapter (a lot of
  code for a consumer that has one run today).

- **Two live runs, analysis first.** Run 1 (infinity-skills) validates
  `research`, the ingestion `run` unit and a Spec Refinement. Run 2
  (smart_mcps) validates `evaluate` and `optimize` on the grouping KPI. Each
  ships with its own driver checklist. Rejected: one combined run (two repos
  in one run is not supported; the KPI would be defined before the analysis
  that motivates it).

- **Prompts stay in the plugin; recipe names are `research`, `evaluate`,
  `optimize`.** Per-project prompt override is later work; the registry's
  `custom` mapping and prompt-name fields already leave room. Session roles
  gain `researcher`; evaluate children are `runner`; optimize keeps `coder`
  and `reviewer`.

## Requirements

### The `research` recipe

- R1. `research-recipe` — **`research` is a worker session that answers one
  external-knowledge question about this codebase and commits a Findings
  Artifact.** The unit declares its question and optional focus paths in
  `recipe_args`; the session grounds the question in codegraph, queries
  Perplexity, follows into the web when needed, and writes the artifact under
  `docs/research/`. It runs under the worker Landlock profile (writes are
  confined; reads and network are not) and may not spawn a nested `claude`.
- R2. `findings-contract` — **The completion contract is a schema: findings
  with sources.** Each finding carries a claim, at least one source reference
  (URL or repo path with span), a confidence word, and a freshness date where
  the source has one; the artifact carries a manifest summary of at most 2,000
  characters and an optional Spec Refinement (R5). A report missing any of
  these fails validation naming the field, regardless of `ReviewIntensity`.
- R3. `research-tools` — **Perplexity first, built-in web tools as a recorded
  fallback, codegraph available, nothing else.** The recipe's allowlist adds
  the Perplexity CLI/MCP, WebSearch and WebFetch to the worker set; NotebookLM
  is not included. When Perplexity is unavailable the session may fall back to
  WebSearch/WebFetch, and the fallback is written into the run log and the
  artifact entry — never silent.
- R4. `research-inputs` — **A research unit receives the plan's question, the
  manifest entries of its non-`code` upstreams, and codegraph.** The upstream
  block is the same injection `run` entries get today (summary and
  measurements, never file bodies); codegraph is offered, not mandated, so a
  question with no code anchor costs nothing.
- R5. `spec-refinement` — **A Findings Artifact may refine the spec of the
  downstream unit it feeds, and only that unit.** The refinement is applied
  through the existing spec-rewrite path to a unit named in the plan's
  `depends_on` from the research unit; a refinement naming any other unit, or
  proposing new, split or removed units, is a validation error naming the
  unit. The rewritten spec records its provenance (the artifact id).
- R6. `research-review` — **The reviewer prompt checks that sources support
  claims and that any refinement follows from the findings.** `ReviewIntensity`
  decides whether the reviewer session runs, as for `code`.
- R7. `research-merge` — **A research unit commits only under `docs/research/`
  and merges through Preflight.** Detection is the existing
  `git status --porcelain` check against the declared globs; anything else
  fails the merge naming the paths.
- R8. `research-handoff` — **Generation retirement uses a research handoff:
  what was found, which sources are exhausted, what remains open.**
  Generation caps, re-entry and Warm Resume are unchanged.
- R9. `research-pricing` — **A research unit is priced by a declared size
  class (`small`/`medium`/`large`), default `medium`, recorded as defaulted in
  the grouping trace.** Research units stay singleton groups.

### The `evaluate` recipe

- R10. `evaluate-recipe` — **`evaluate` is `run` plus a KPI Contract.** It
  reuses the Run Child, wall-clock cap, measurements JSON, triage, commit
  paths and manifest registration unchanged, and adds the contract in R11. A
  standalone evaluate unit reports the KPI, the guards and whether a declared
  threshold was cleared as *observations* — it never gates its own
  completion (seed B's P2).
- R11. `kpi-contract` — **The contract names one objective key, its
  direction, a minimum effect, guard metrics with bounds, and the harness
  paths.** All keys are read off the command's measurements JSON; a missing
  objective key after a zero exit is a Work Failure naming the key. An
  optional `smoke` command runs first; its failure is a `crash` that never
  becomes a measurement.
- R12. `harness-protection` — **Harness paths are hash-checked before every
  scoring run and are never in an optimizer's mutable region.** The evaluate
  child records the harness hash in its measurements; a mismatch against the
  hash captured when the harness unit merged is a Work Failure naming the
  path. A KPI Contract whose harness paths overlap the same plan's optimize
  unit `files:` is a parse-time error.
- R13. `judge-in-harness` — **The orchestrator never calls a judge; a judge is
  a harness script.** An LLM-as-judge KPI is a protected script that makes
  its own LLM calls — pairwise, position-swapped, disk-cached, scored against
  the live baseline arm, per Infinity Skills' `summary_prompt_tuning` patterns
  — and writes the same measurements JSON. Its cost is a recipe child's,
  reported "unknown" per the standing rule.
- R14. `evaluate-frozen-inputs` — **An Evaluation Harness declares its inputs
  and they are content-hashed with it.** For the grouping harness that means
  stored plans plus cached task graphs, never a live codegraph index; for a
  judge, the fixed instance set and the baseline outputs.

### The `optimize` recipe

- R15. `optimize-recipe` — **`optimize` runs a coder-and-reviewer group whose
  every round is one candidate, one evaluation, one decision.** The coder
  commits exactly one candidate per round on the group's branch; the
  orchestrator launches the unit's evaluate child against that commit; the
  Keep-or-Revert decision (R17) advances the Champion or resets the worktree
  to it; the next round's prompt carries the Attempt Ledger (R18). The coder
  is told the outcome and why, in numbers.
- R16. `mutable-region` — **The candidate may touch only the unit's `files:`.**
  A diff outside them, or in any harness path, is a `discard` recorded in the
  ledger with the offending paths, and the candidate is never scored.
- R17. `keep-or-revert` — **The orchestrator decides from numbers: keep,
  promising, inconclusive, discard or crash.** `keep` when the KPI delta
  clears both the minimum effect and twice the noise floor (MAD over the last
  five scored deltas) and no guard worsens past its bound; `promising` when it
  clears but the noise floor is non-zero, becoming `keep` after one identical
  confirmation run; `inconclusive` within the noise floor; `discard` on
  regression or a guard breach; `crash` on a non-zero exit or a smoke failure.
  Only `keep` moves the Champion; every other outcome resets the worktree to
  it. Confirmation runs count against the evaluation cap; crashes count
  against the round budget.
- R18. `attempt-ledger` — **Every attempt is appended to a per-group ledger
  and the ledger reaches the coder every round.** Columns: candidate commit,
  KPI value, guard values, delta to Champion, noise floor at the time,
  outcome, harness hash, one line of why. The ledger is a file under the
  group's directory, rides the Run Bundle as an additive key, and is folded
  into every round's prompt as a table capped like the artifact block.
- R19. `loop-dials` — **Bounded, and a bound escalates rather than stops.**
  Plan-declared: evaluation cap, per-evaluation wall clock, optional
  good-enough threshold. Config defaults: patience 4 non-improving
  evaluations, 3 consecutive reverts. Reaching patience or the revert cap
  raises an escalation with the ledger attached; the evaluation cap or the
  threshold ends the loop. Rounds, rewrites and generations keep their caps;
  exhausting them is a Work Failure as today.
- R20. `zero-hit-completes` — **A loop that ends with the baseline as Champion
  is COMPLETED.** Its manifest entry is the ledger with a summary of what was
  ruled out; nothing is merged; the unit is landed.
- R21. `champion-merge` — **When the loop ends with a kept Champion, that
  commit merges through the `code` ladder and Preflight.** The finish
  one-pager lists every keep with its delta and links the ledger, so the human
  reads the winning diff before the PR merges — the human transfer test.
- R22. `cheat-review` — **Every `promising` candidate gets a reviewer with a
  second job before its confirmation run.** The reviewer prompt checks that
  the candidate is not a semantic repeat of a ledger row, touches no harness,
  seed or budget-enforcing line, and does not special-case held-out inputs.
  This reviewer runs regardless of `ReviewIntensity`; a failed check is a
  `discard` with the reviewer's reason in the ledger.
- R23. `optimize-inputs-and-pricing` — **The optimizer is seeded with the
  working code, the ledger and its non-`code` upstream entries, and is priced
  as a `code` unit plus its evaluations.** Tokens follow the `code`
  arithmetic; wall clock is the evaluation cap times the declared
  per-evaluation clock; both are recorded in the grouping trace. Optimize
  units are singleton groups.

### Shared plumbing

- R24. `session-roles` — **`SessionRole` gains `researcher`; evaluate children
  are `runner`; optimize sessions are `coder` and `reviewer`.** Attribution,
  cost lines and the Observatory's role union follow additively.
- R25. `registry-generalised-merge` — **The registry's merge policy becomes
  "declared commit globs through Preflight", of which `code_ladder` is the
  unrestricted case.** `run`'s `commit_paths`, research's `docs/research/`
  and optimize's Champion merge are three declarations of one policy.
- R26. `bundle-additive` — **Export changes stay additive.** The ledger and
  the Findings Artifact ride the Run Bundle as manifest entries and a new
  optional key; `schema_version` stays 2; consumers that ignore the new roles
  and keys must not break.
- R27. `recipe-gate` — **Each new recipe must be listed in `[recipes] enabled`
  or the plan fails loudly**, exactly as `run` today.
- R28. `live-checklists` — **Each new recipe ships with a driver checklist of
  verifiable checks**, written into this document's Validation section and
  carried by the plan's `Run (driver):` items.

### Validation — the two live runs

- R29. `analysis-run` — **Run 1, in infinity-skills, validates `research`.**
  The pipeline: a `run` unit exports and ingests five chosen orchestrator runs
  (three small, two with many groups) through the existing `export` and
  `ingest-run`, then summarises and enriches them with Infinity Skills'
  Claude-backed summarizer (a nested `claude -p` inside the Run Child, cost
  "unknown" by the standing rule) — its outputs, caches and a measurements
  JSON (sessions ingested, summaries written) live under the repo's
  `[workspace] data_dirs` so the confined child can write them and downstream
  units receive the entry; `code` units write extraction scripts; a `run` unit executes
  them; `research` units read the manifest entries and Perplexity. Success is
  four artifacts: a repeated-error table across the five runs (denials, error
  signatures, repeated text) ranked by count with one proposed orchestrator
  fix each; a per-run read-set overlap measurement naming which groups read
  the same files; a session-flow findings document; and a Spec Refinement
  applied to a declared fix unit. Checklist: the fallback line appears if
  Perplexity fails; a finding without a source is rejected by the contract;
  the refinement lands on the named unit and is refused for another; a
  research unit's manifest entry is injected into its consumer's prompt;
  the researcher role appears in the bundle with its cost.
- R30. `grouping-loop-run` — **Run 2, in smart_mcps, validates `evaluate` and
  `optimize` on the grouper.** A harness unit builds the scoring script over
  the five stored runs: KPI = cross-group read-set overlap between sequential
  groups that would fit one budget (lower is better), from the runs'
  transcripts; guards = estimate error against real coder context and group
  count, neither worsening; inputs = stored plans and cached task graphs.
  The optimize unit's `files:` are the grouper modules; 20 evaluations,
  half a day. Success is a completed loop with a full ledger — one keep, or a
  ruled-out list. Checklist: a candidate touching a harness path is discarded
  unscored; a hash mismatch is a Work Failure naming the path; a
  deterministic KPI shows noise floor 0 and the minimum-effect rule; a
  `promising` candidate triggers the cheat reviewer then a confirmation run;
  a kill -9 mid-evaluation re-adopts the child; the ledger appears in the
  round prompt and in the bundle; patience escalates instead of stopping; a
  zero-hit end reads COMPLETED. A later loop may set the KPI to estimate
  error with the same harness — only the contract changes.

## Non-Goals

- **`synthesize`.** Deferred until a pipeline needs N→1 reduction its
  consumer cannot do; R5's refinement covers the analysis pipeline.
- **Any node creation by a unit.** ADR 0010 stands; a refinement edits one
  existing downstream spec and nothing else.
- **An orchestrator-owned LLM judge.** Judges are harness scripts (R13).
- **A dollar cap or nested-cost estimation.** Recipe children stay "cost
  unknown"; count and wall-clock caps bound the loop.
- **Embedding or LLM novelty rejection before evaluation.** The ledger in the
  prompt and the cheat reviewer carry this in v1.
- **NotebookLM in the research allowlist.**
- **Per-project prompt override.** Prompts live in the plugin.
- **Changes to Infinity Skills** (ingestion, adapter, ledger consumption).
- **Multi-unit document or optimize groups.** Singletons, as for `run`.
- **An Observatory surface** for the ledger or findings.
- **Statistical tests beyond the noise-floor band** (Wilcoxon, trend tests);
  reconsider when a loop runs past ~50 evaluations.

## Open Questions

- The exact read-set overlap formula (which reads count, how "would fit one
  budget" is computed) is the harness unit's design work in Run 2's plan,
  not a requirement.
- Which five runs are ingested in Run 1 is chosen at plan time from the runs
  whose transcripts still resolve.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-09-27-research-evaluate-optimize-requirements.md`.
