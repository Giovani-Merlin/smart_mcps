---
title: Deterministic spec assembly, validation legibility, and the advisory grouper
type: feat
date: 2026-08-28
origin: docs/brainstorms/2026-08-28-grouper-speccer-flow-requirements.md
---

# Deterministic spec assembly, validation legibility, and the advisory grouper

## Objective

Ship waves 1a–1c of the grouper-speccer-flow requirements so that after this
run the grouper is the well-working core of the planning→run flow:

- **Wave 1a** (R1, R2, R4, R21, R22, R23 + superseded R3): `group` produces a
  launchable `groups.json` with **zero LLM calls** — specs assembled
  deterministically from the plan's own unit sections — and worker context is
  layered (shared digest + per-group full sections) instead of every worker
  carrying the full plan. The grouping-time speccer is **deleted** (ADR 0006),
  not made opt-in: R3 is superseded by the grill decision of 2026-08-28.
- **Wave 1b** (R5–R11): the six measured validation-legibility failures
  (C1–C6 in `docs/todos/grouping_improvements.md`) are fixed so a written plan
  never again costs 15 `group` invocations to validate.
- **Wave 1c** (R12–R15): `group --advise` — multi-granularity comparison and
  plan-cohesion diagnostics off one built graph, zero LLM — plus the merge-key
  fill penalty, and `/orchestrator-plan` consulting the advisory before asking
  the user about splits.

Waves 2–3 (mechanical split R16, deepen R17–R19, eval harness R20) are out of
scope. A **post-eval-harness backlog** section at the end records what waits.

## What we already know (resolved context)

Ground truth verified 2026-08-28 against the working tree (commit `d141d6e`):

- **The speccer call being deleted** is `write_specs`
  (`orchestrator/grouping/speccer.py:62`), invoked once by `run_grouping`
  (`orchestrator/grouping/pipeline.py:673`) with per-group skeletons
  `{tasks, descriptions, files}` and the stripped plan text. Its output type
  `GroupSpec` carries `group_id, name, summary (≤120 chars, SUMMARY_MAX_CHARS in orchestrator/model.py), spec, verification: list[VerificationItem]`.
  `VerificationItem` is `{id, description, required}` (`orchestrator/model.py:32`).
  The prompt template is `orchestrator/prompts/speccer.md`.
- **Two speccers exist**: the grouping-time one above (deleted) and the
  **mid-run rewrite speccer** (`orchestrator/cli.py:1919` `_rewrite_provider`
  area, `orchestrator/execution/review.py` escalation path, run-form knob
  `model_speccer` in `ui/src/components/launch/ExecutionOptions.tsx:261`).
  The rewrite speccer, the run-form knob, and the Observatory LLM-call viewer
  (`SpeccerCalls` in `ui/src/components/grouping/GroupingTab.tsx`) all **stay**.
  The grouping-form knob (`GroupingOptions.model_speccer`,
  `orchestrator/observatory/launch.py:121/147`, `ui/src/routes/Launch.tsx:193`,
  grouping-form parts of `ui/src/types.ts:532`) goes.
- **`compute_partition`** (`pipeline.py:436`) is the deterministic sub-second
  prefix: parse map → quiescence → graph → partition → group DAG; it returns
  `PartitionOutcome` with `graph, partition, dag, node_work, budget_cap, hub_roles, slice_atoms, flags, base_context, base_tokens`. `run_grouping`
  adds estimator + speccer and builds `Group` objects. `serialize_grouping`
  is the canonical `groups.json` writer.
- **Base context today embeds the full plan**: `compile_base_context`
  (`orchestrator/grouping/base_context.py:19`) = ground rules + CLAUDE.md /
  AGENTS.md + codegraph summary + `strip_task_map(plan)`. It is byte-stable by
  contract, feeds `start_base` (`orchestrator/execution/sessions.py:400`),
  which loads it into the run's base session that all workers fork from — so
  "shared block first in every worker prompt" (R23) is already the
  architecture; only the content changes.
- **Budget arithmetic**: `node_work` (`orchestrator/grouping/estimator.py:51`)
  = source_bytes/`bytes_per_token`(4.0) × `slack_multiplier`(1.3) + file
  allowances (`per_file_tool_allowance`, 2000; `size_hints` 500/2000/5000);
  `partition_budget_cap` (`estimator.py:79`) = `token_budget`(200,000) − head,
  head = (base_tokens + spec_tokens_allowance) × slack ×
  `coder_slack_multiplier`(2.5). The trace records this as `BudgetArithmetic`
  (`pipeline.py:526`).
- **C1 confirmed live**: `_check_slice_overflow` (`pipeline.py:214`) raises on
  the first over-cap slice inside its loop; `plan_reader.py` has ~20
  `raise TaskMapError` sites and no accumulator.
- **Merge key confirmed** (`orchestrator/grouping/partition.py:974`):
  `(-source_wave, candidate_makespan, -removed_affinity, -edge_weight, merged_work, source, target)` — `merged_work` is a 5th-place tiebreaker
  with no fill/balance term, matching the R19b greedy-fragmentation finding.
- **Advisory building blocks exist**: `_simulate_makespan` (`partition.py:783`),
  `_compute_waves` (`partition.py:998`), `compute_scorecard` + `_modularity`
  (`orchestrator/grouping/scorecard.py`), `Granularity`/`GRANULARITY_LEVELS`
  (`partition.py:40`), the `preview/` quarantine for `--no-spec`/`--dry-run`
  (`cli.py:665–742`), `EdgeProvenanceRecorder`/`edge_provenance_document`
  (`pipeline.py:306`), `_drop_inferred_cycles` (`graphing.py:626` — task-graph
  level only; the group-DAG repair `repair_cycles` in `partition.py` merges
  SCCs and re-splits by wave, never withdraws inferred edges).
- **Plan-unit conventions**: unit headings are `### U<N>. <name>`; task ids are
  `u<N>-<slug>`. `strip_task_map` (`plan_reader.py:133`) removes the fenced
  map block and its `## Task Map` heading from embedded copies only.
- **Symbols stay empty in this plan's own map** (R9/C4 lesson): on this dense
  codebase populating `symbols` added 103 inferred precedence edges and
  degenerated the partition; declared `depends_on` and shared-file affinity
  carry the structure instead.
- `config.session.speccer_model` powers the **mapper** LLM (foreign plans) and
  the rewrite speccer as well as the grouping speccer — the field and
  `--model-speccer` survive the deletion; only their grouping-time consumer
  goes, and help text is reworded.

## Decisions

- **Delete the grouping-time speccer; keep the rewrite speccer and the
  Observatory LLM-call viewer.** (→ ADR 0006) The planner session is the
  speccer; the paraphrase added cost and drift surface, not information.
  Deletion is one standalone commit, recorded in `docs/orchestrator-grouping.md`
  for cherry-pick recovery. Rejected: opt-in overlay (its enrichment dies on
  re-group; deepen in wave 2 is the durable path), deleting the rewrite path
  (it consumes genuinely new failure history — out of wave-1 scope).
- **Dry deterministic prose is acceptable** (brainstorm decision, unchanged):
  names/summaries derived from unit titles, relational header from graph
  facts only (R2) — no polish pass.
- **Verification items come only from the plan.** Assembled verbatim from unit
  Verification bullets with stable ids `<group_id>-<n>`, `required=true`.
  LLM-added verification happens in deepen (wave 2), written into the plan.
- **Layered context is the default, not a flag.** Shared digest = plan
  preamble (everything before `## Units`, task map stripped) + one tagged
  `Summary:` line per unit + implements/consumes registry. Group spec = member
  units' full sections verbatim + relational header + contracts-only lines for
  cross-group neighbors. No worker receives the full plan. Rejected: keeping
  the full plan in base context behind a flag (defeats R22's token economy).
- **R11 is edge withdrawal only.** `repair_cycles` withdraws *inferred*
  precedence edges (never declared ones) on the group DAG before merging.
  The hyperedge/hub-routing re-modeling of symbol edges is parked to the
  post-eval-harness backlog (user decision 2026-08-28).
- **The fill-penalty experiment lands in wave 1c** (user decision 2026-08-28):
  a fill/balance term promoted in the merge key so one group stops absorbing
  merges until it hits the cap; `--advise`'s granularity comparison is the
  immediate before/after readout.
- **Verification-coverage lint in wave 1a** (user decision 2026-08-28): after
  assembly, every unit Verification bullet must appear as a `VerificationItem`
  in exactly one group; a miss is a hard `GrouperError` naming the unit.
- **No cross-invocation graph cache for `--advise` in v1** (user decision
  2026-08-28): the three granularity partitions reuse one built graph within
  a single invocation; re-run latency is measured first, cached later only if
  needed.
- **`--price` compiles its cap estimate without codegraph** and labels it
  approximate: base context is compiled with an empty codegraph summary, so
  the printed cap is within a few thousand tokens of the real one, sub-second,
  with the delta stated in the output.
- **Advisory thresholds are named constants, tuned during implementation** on
  the existing runs/traces in this repo (brainstorm open question resolved as
  "tune in 1c"): near-disconnected WCC edge-weight threshold, seriality
  depth/width ratio, cut-sweep valley detection, and the low-modularity /
  high-conductance "structurally monolithic" report.

## Units

### U1. plan-sections — deterministic plan parsing: unit sections, summaries, digest

- **Goal**: A parser that splits a plan document into (a) the preamble
  (everything before the `## Units` heading, task map stripped), (b) unit
  sections keyed by unit id — heading `### U<N>.` ↔ task ids matching
  `u<N>-…` — each with its verbatim body, its tagged `Summary:` line, and its
  parsed Verification bullets, and (c) a digest builder producing the shared
  block: preamble + per-unit tagged summary lines + implements/consumes
  registry. Parse errors accumulate per phase and report together (same
  discipline as R5). A unit with no `Summary:` line falls back to its heading
  title with an info flag (older plans keep working); a task id with no
  matching unit section is a hard error.
- **Files**: `orchestrator/grouping/plan_sections.py` *(new, large)*,
  `tests/test_plan_sections.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: assembly
- **Implements / Consumes**: implements `plan-sections`
- **Verification**:
  - Parsing this plan document itself yields one section per task-map entry,
    each section's text found verbatim in the source, and a digest that
    contains every unit's `Summary:` line and none of the unit bodies.
  - A plan whose unit `U3` heading is missing while the map declares
    `u3-layered-context` raises one error naming `u3-layered-context`, and a
    plan with two missing sections reports both in the same error.
  - The digest and section split are byte-stable: parsing the same text twice
    yields identical bytes.
  - The task-map YAML block appears in neither the digest nor any section.

### U2. spec-assembly — assemble group specs deterministically, replacing write_specs

- **Goal**: `run_grouping` builds every group's `name`, `summary` (within the
  120-char cap), `spec`, and `verification` with zero LLM calls: name/summary
  derived from member unit titles; spec = a generated relational header
  (member list with descriptions, intra-group `depends_on` in topological
  order, upstream/downstream groups with the contract tags exchanged, slice
  membership — graph/DAG facts only, per R2) followed by the member units'
  plan sections verbatim; verification = the units' Verification bullets with
  ids `<group_id>-<n>`, `required=true`. The verification-coverage lint runs
  after assembly: every unit bullet lands in exactly one group or
  `GrouperError` names the unit. `groups.json` gains the flag
  `specs: assembled from plan — speccer LLM skipped`. The stage progress line
  becomes `stage: assemble` (no per-spec LLM progress).
- **Files**: `orchestrator/grouping/assembler.py` *(new, large)*,
  `orchestrator/grouping/pipeline.py`, `tests/test_assembler.py`
  *(new, medium)*, `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: u1-plan-sections
- **Slice**: assembly
- **Implements / Consumes**: consumes `plan-sections`; implements
  `assembled-specs`
- **Verification**:
  - `group` on a task-mapped plan completes with an LLM runner stub that
    raises on any call (`_llm_must_not_be_called`-style), and the produced
    `groups.json` validates against `GroupingResult` with non-empty
    name/summary/spec/verification for every group.
  - Each group's spec contains its member unit sections verbatim, and its
    relational header names exactly the upstream/downstream group ids that
    `build_group_dag` produced — regenerating after a changed partition
    changes the header accordingly (R2).
  - Every Verification bullet of every unit appears as exactly one
    `VerificationItem` across all groups, ids matching `^g\d+-\d+$`; removing
    one unit's Verification section makes grouping fail naming that unit.
  - `groups.json` output is byte-deterministic across two runs on an
    unchanged repo.

### U3. layered-context — digest in base context, contracts-only neighbors in specs

- **Goal**: `compile_base_context` embeds the plan **digest** (from U1)
  instead of the full stripped plan; the assembled spec (U2) appends, for each
  cross-group unit this group consumes from or provides to, one line with the
  contract tag, the counterpart task id, and its `Summary:` line — never a
  neighbor's full section. No worker context contains the full plan document.
  Byte-stability and the task-map-absence guarantees carry over to the digest.
  `base_tokens`/`budget_cap` now derive from the digest-based context
  (groups get roomier; no compensation is applied).
- **Files**: `orchestrator/grouping/base_context.py`,
  `orchestrator/grouping/assembler.py`, `orchestrator/grouping/pipeline.py`,
  `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: u1-plan-sections, u2-spec-assembly
- **Slice**: — *(deliberately outside `assembly`: the three-unit slice priced
  50k over the cap — u3 rides its `depends_on`/shared-file affinity instead)*
- **Implements / Consumes**: consumes `plan-sections`, `assembled-specs`
- **Verification**:
  - For a multi-unit fixture plan, `base-context.md` contains every unit's
    `Summary:` line and the preamble, contains no unit's full section body,
    and no `orchestrator-task-map` marker; compiling twice is byte-identical.
  - A group consuming `/api/x` implemented by another group carries a
    contracts line naming `/api/x` and the implementing task id, and does not
    contain that neighbor unit's section body.
  - The union of one group's assembled spec plus the base context contains
    that group's full unit sections exactly once.

### U4. speccer-removal — delete the grouping-time speccer in one recoverable commit

- **Goal**: Remove `orchestrator/grouping/speccer.py`,
  `orchestrator/prompts/speccer.md`, the grouping-form model knob
  (`GroupingOptions.model_speccer` in `orchestrator/observatory/launch.py`
  and its argv plumbing, the grouping-form parts of `ui/src/routes/Launch.tsx`,
  `ui/src/components/launch/ExecutionOptions.tsx`, `ui/src/types.ts`,
  `ui/src/api.ts`), and the grouping-time speccer wiring/help-text in
  `orchestrator/cli.py` (`_speccer_json_runner` stays for the mapper and
  rewrite speccer; its docstring and `--model-speccer` help are reworded to
  "mapper / rewrite speccer"). The mid-run rewrite speccer, the run-form
  `model_speccer` knob, and the `SpeccerCalls` viewer stay untouched. This
  unit's changes land as **one standalone commit** whose sha is recorded in a
  short "grouping-time speccer removed" note in `docs/orchestrator-grouping.md`
  (recovery = cherry-pick, per ADR 0006).
- **Files**: `orchestrator/grouping/speccer.py`,
  `orchestrator/prompts/speccer.md`, `orchestrator/cli.py`,
  `orchestrator/observatory/launch.py`, `ui/src/routes/Launch.tsx`,
  `ui/src/components/launch/ExecutionOptions.tsx`, `ui/src/types.ts`,
  `ui/src/api.ts`, `docs/orchestrator-grouping.md`,
  `tests/test_observatory_launch.py`, `ui/src/routes/launch.test.tsx`
- **Symbols**: —
- **Depends-on**: u2-spec-assembly
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `grep -r "write_specs\|prompts/speccer" orchestrator/ ui/src/` finds no
    references; `orchestrator/grouping/speccer.py` and
    `orchestrator/prompts/speccer.md` do not exist.
  - The grouping launch API rejects/ignores `model_speccer` for groupings
    while the **run** launch body still accepts it, and
    `pytest tests/test_observatory_launch.py` plus the UI test suite pass.
  - The rewrite-speccer path still works: `orchestrator/execution/review.py`'s
    rewrite flow and its tests are unchanged and green.
  - `docs/orchestrator-grouping.md` contains a removal note with a commit sha.

### U5. planning-contract-docs — Summary field, conventions, budget formula in the contracts

- **Goal**: `skills/orchestrator-plan/SKILL.md` and
  `docs/orchestrator-task-map.md` document the new authoring contract: the
  required per-unit `Summary:` tagged line (R21) and the `### U<N>.` ↔
  `u<N>-…` id convention; Verification written as a bulleted list (one item
  per bullet; a single-line Verification is one item); `symbols` is optional,
  contributes derived precedence, and on a dense codebase omitting it may give
  a better partition (R9, replacing the unconditional "record exact symbols");
  the pricing formula, multipliers, and cap documented next to `size_hints`
  with a pointer to `group --price` (doc half of R6); the plan-template unit
  skeleton gains the `Summary:` field.
- **Files**: `skills/orchestrator-plan/SKILL.md`,
  `docs/orchestrator-task-map.md`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `planning-contract`
- **Verification**:
  - `docs/orchestrator-task-map.md` states the node-work formula with the
    numeric defaults (bytes/4 × 1.3 + 2000/file, cap = 200,000 − head) and
    names `group --price`.
  - `SKILL.md` no longer instructs "Record exact existing symbols" without
    qualification; it states the dense-codebase trade-off and the
    `Summary:`/Verification-bullet conventions; the unit template shows
    `Summary:`.

### U6. error-accumulation — every validation phase reports all its failures (C1/R5)

- **Goal**: `_check_slice_overflow` collects every over-cap slice and raises
  once listing all of them; `plan_reader` accumulates shape errors and reports
  them together, then accumulates reference errors (unknown `depends_on`,
  bad `size_hints`, duplicate ids) and reports them together — phase order
  preserved, first failing phase reports its full set then stops. Error text
  keeps naming each problem exactly as today (one line per problem).
- **Files**: `orchestrator/grouping/pipeline.py`,
  `orchestrator/grouping/plan_reader.py`, `tests/test_plan_reader.py`,
  `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - A map with three over-cap slices fails with one `GrouperError` naming all
    three slices with their sums and overshoots.
  - A map with two unknown-key tasks and two bad `size_hints` entries reports
    both unknown-key problems in one error; fixing those then reports both
    `size_hints` problems in one error.
  - Single-error behavior is unchanged in wording for existing tests.

### U7. price-mode — `group --price` prints the budget arithmetic sub-second (C3/R6)

- **Goal**: `group --price <plan>` parses the task map (no graph build, no
  codegraph client, no quiescence), prices every task's node work from working
  tree byte counts + `size_hints`, and prints: per-task node work, per-slice
  sums against the cap with pass/fail, and the resolved budget parameters
  (token_budget, multipliers, per-file allowance, head, cap) — with the cap
  labeled approximate (compiled with an empty codegraph summary) and the
  approximation stated. Exits non-zero if any slice exceeds the cap. Runs
  sub-second.
- **Files**: `orchestrator/cli.py`, `orchestrator/grouping/estimator.py`,
  `tests/test_cli_price.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: consumes `planning-contract`
- **Verification**:
  - On a fixture plan with one over-cap slice, `--price` exits non-zero,
    prints that slice's member-by-member node work, sum, cap, and overshoot,
    and never invokes codegraph (asserted via a client stub that raises).
  - On this repo's 2026-08-26 plan document, `--price` completes in under one
    second and its per-task figures match `grouping-trace.json` `node_work`
    for the same map within the stated cap approximation.
  - The output names both `node work` and `coder work` scales consistently
    with U8.

### U8. budget-naming — one vocabulary for node work vs coder work (C2/R7)

- **Goal**: Everywhere an operator sees a budget figure — the slice-overflow
  error, the dry-run/group report listing, `--price` output — the two
  quantities are named distinctly: `node work` (unscaled) and `coder work`
  (× `coder_slack_multiplier`). The slice-overflow error prints both figures
  and states the multiplier. No arithmetic changes.
- **Files**: `orchestrator/grouping/pipeline.py`, `orchestrator/cli.py`,
  `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: u6-error-accumulation
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - The slice-overflow error message contains both a `node work` and a
    `coder work` figure for each offending slice and the literal multiplier
    value in use.
  - `group --dry-run`'s per-group listing labels its figure `node work`, and
    no user-facing output says bare "work" for either quantity.

### U9. partition-diagnostics — inferred/declared provenance and slice-re-entry named (C4.2+C5 / R8+R10)

- **Goal**: (a) The degenerate-partition error reports how many of the edges
  inside the offending SCC are inferred vs declared, with provenance — e.g.
  "103 of 127 dependency edges are inferred from `symbols`" — sourced from
  the same contribution data `edge_provenance_document` records. (b) A
  dependency path that leaves and re-enters a slice is detected on the
  contracted graph before partitioning and reported by name with the exact
  edit: "slice `resilience` contracts `u1` and `u3`, but `u1 → u2 → u3`
  leaves the slice and returns — bring `u2` into the slice or drop the
  label"; all such shapes are reported together (R5 discipline).
- **Files**: `orchestrator/grouping/pipeline.py`,
  `orchestrator/grouping/graphing.py`, `orchestrator/grouping/partition.py`,
  `tests/test_partition.py`, `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: u6-error-accumulation
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - A fixture reproducing the run-10 shape (u1→u2→u3 with u1/u3 slice-mates)
    fails naming the slice, the full path, and both remedies — not with the
    generic saturation message.
  - A degenerate-partition fixture with mixed inferred/declared edges reports
    the exact counts and the inferred edges' source kind.

### U10. cycle-repair-withdrawal — group-DAG repair withdraws inferred edges before merging (C4.1/R11)

- **Goal**: `repair_cycles` receives the declared-edge set (the mapper's
  `depends_on` pairs) and, when a group-SCC cycle forms, first withdraws
  inferred precedence edges (banking their weight as affinity, as
  `_drop_inferred_cycles` already does at task level) until the group DAG is
  acyclic — falling back to the existing merge+re-split only if withdrawal
  alone cannot break the cycle. Declared edges are never withdrawn.
  Withdrawals are recorded in the trace `repairs` with the edges named.
- **Files**: `orchestrator/grouping/partition.py`,
  `orchestrator/grouping/graphing.py`, `orchestrator/grouping/pipeline.py`,
  `tests/test_partition.py`
- **Symbols**: —
- **Depends-on**: u9-partition-diagnostics
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - A fixture whose group cycle is closed only by an inferred edge partitions
    cleanly with no merge, the withdrawal recorded in the trace, and the
    final group count unchanged from the pre-cycle expectation.
  - A fixture whose cycle consists solely of declared edges still takes the
    merge+re-split path, and declared edges never appear as withdrawn in any
    trace.

### U11. advisory-report — `group --advise`: one graph, all granularities, cohesion diagnostics (R12–R14)

- **Goal**: `group --advise <plan>` builds the task graph **once** (reusing
  the `compute_partition` prefix refactored into a graph-building seam), runs
  the partition at every `GRANULARITY_LEVELS` preset off that cached graph,
  and writes an Advisory Report — JSON artifact in the grouping's `preview/`
  dir plus a human-readable rendering — containing per preset: group count,
  per-group node work and budget utilization (mean/max), cross-group edge
  cut, group-DAG depth, simulated makespan (`_simulate_makespan`), modularity
  (`scorecard._modularity`), with Pareto-dominant presets flagged. Cohesion
  diagnostics, zero LLM: weakly-connected components computed before Louvain
  (a multi-component or near-disconnected plan is flagged "this reads as N
  separate plans", naming the task sets); a seriality signal (critical path /
  max wave width via `_compute_waves`) plus a topological-order cut sweep
  whose valleys name candidate phase boundaries ("this reads as serial
  phases"); and a "structurally monolithic" report when modularity is low and
  every cut has high conductance. Thresholds are named module constants.
  `--advise` follows preview semantics: never touches a persisted
  `groups.json`.
- **Files**: `orchestrator/grouping/advisory.py` *(new, large)*,
  `orchestrator/grouping/pipeline.py`, `orchestrator/grouping/partition.py`,
  `orchestrator/grouping/scorecard.py`, `orchestrator/cli.py`,
  `tests/test_advisory.py` *(new, large)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `advisory-report`
- **Verification**:
  - `group --advise` on a fixture plan performs exactly one graph build
    (asserted via a counting client stub) and emits a report with one entry
    per granularity preset, each carrying group count, utilization, edge cut,
    DAG depth, makespan, and modularity.
  - A fixture of two disjoint task sets is flagged "reads as 2 separate
    plans" naming both sets; a pure chain fixture is flagged serial with the
    boundary between its widest-gap waves; a clique fixture reports
    "structurally monolithic".
  - After `--advise`, a pre-existing `groups.json` in the same grouping dir
    is byte-identical, and `advisory.json` lives under `preview/`.
  - The report is byte-deterministic across two runs on an unchanged repo.

### U12. fill-penalty — merge key prefers balanced fills over cap-filling (R19b experiment)

- **Goal**: The merge key in `merge_small_groups` gains a fill/balance term
  ranked above `merged_work`'s current 5th place: candidate merges whose
  merged work lands inside a target band (config knob
  `[partition] target_fill_ratio`, default chosen and justified in-code) are
  preferred over ones landing at the cap, so one group no longer absorbs
  merges until it hits the cap. Determinism discipline preserved (sorted
  iteration, full ordering). The granularity-ladder and hub tests are updated
  where the expected partitions legitimately change; any change is explained
  in the test.
- **Files**: `orchestrator/grouping/partition.py`, `orchestrator/config.py`,
  `tests/test_partition.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - A synthetic fixture reproducing the greedy pathology (one group absorbing
    every merge to ~96% of cap while siblings starve) now yields a partition
    whose per-group fill variance is strictly lower, with no group over cap
    and the group DAG still acyclic.
  - Partitions remain byte-deterministic across runs, and slice must-link and
    budget-cap invariants hold on the whole existing test suite.

### U13. plan-skill-advise-phase — /orchestrator-plan consults the advisory and asks (R15)

- **Goal**: `skills/orchestrator-plan/SKILL.md`'s hand-off phase runs
  `smart-mcps-orchestrate group <plan> --advise` after the `--no-spec`
  validation, presents the Advisory Report's diagnostics (granularity
  comparison, "reads as N plans", "reads as serial phases"), and **asks the
  user** whether to split along the reported seams or proceed as one plan —
  never splitting silently, never rewriting the plan. The mechanical split and
  the deepen-command reminder are noted as arriving with wave 2 (R16/R17),
  not fabricated now.
- **Files**: `skills/orchestrator-plan/SKILL.md`
- **Symbols**: —
- **Depends-on**: u5-planning-contract-docs, u11-advisory-report
- **Slice**: —
- **Implements / Consumes**: consumes `advisory-report`, `planning-contract`
- **Verification**:
  - The skill's hand-off phase contains the `--advise` invocation, instructs
    presenting its diagnostics, and phrases the split strictly as a question
    to the user with proceed-as-one as an explicit option.
  - The skill contains no instruction to auto-split or regenerate plan prose,
    and no reference to a deepen command as currently runnable.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-plan-sections
    description: Deterministic plan parser producing unit sections, tagged summaries, and the shared digest
    slice: assembly
    files:
      - orchestrator/grouping/plan_sections.py
      - tests/test_plan_sections.py
    size_hints:
      orchestrator/grouping/plan_sections.py: large
      tests/test_plan_sections.py: medium
    symbols: []
    depends_on: []
    implements: ["plan-sections"]
    consumes: []
  - task_id: u2-spec-assembly
    description: Assemble group name/summary/spec/verification deterministically, replacing write_specs, with the verification-coverage lint
    slice: assembly
    files:
      - orchestrator/grouping/assembler.py
      - orchestrator/grouping/pipeline.py
      - tests/test_assembler.py
      - tests/test_grouper_pipeline.py
    size_hints:
      orchestrator/grouping/assembler.py: large
      tests/test_assembler.py: medium
    symbols: []
    depends_on: [u1-plan-sections]
    implements: ["assembled-specs"]
    consumes: ["plan-sections"]
  - task_id: u3-layered-context
    description: Base context carries the plan digest; specs carry full member sections plus contracts-only neighbor lines
    slice: null
    files:
      - orchestrator/grouping/base_context.py
      - orchestrator/grouping/assembler.py
      - orchestrator/grouping/pipeline.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: [u1-plan-sections, u2-spec-assembly]
    implements: []
    consumes: ["plan-sections", "assembled-specs"]
  - task_id: u4-speccer-removal
    description: Delete the grouping-time speccer and its grouping-form knob in one recoverable commit, keeping the rewrite speccer
    slice: null
    files:
      - orchestrator/grouping/speccer.py
      - orchestrator/prompts/speccer.md
      - orchestrator/cli.py
      - orchestrator/observatory/launch.py
      - ui/src/routes/Launch.tsx
      - ui/src/components/launch/ExecutionOptions.tsx
      - ui/src/types.ts
      - ui/src/api.ts
      - docs/orchestrator-grouping.md
      - tests/test_observatory_launch.py
      - ui/src/routes/launch.test.tsx
    symbols: []
    depends_on: [u2-spec-assembly]
    implements: []
    consumes: []
  - task_id: u5-planning-contract-docs
    description: Document Summary field, id and verification conventions, symbols trade-off, and the budget formula in skill and contract
    slice: null
    files:
      - skills/orchestrator-plan/SKILL.md
      - docs/orchestrator-task-map.md
    symbols: []
    depends_on: []
    implements: ["planning-contract"]
    consumes: []
  - task_id: u6-error-accumulation
    description: Slice-overflow and plan_reader validation accumulate and report all failures per phase
    slice: null
    files:
      - orchestrator/grouping/pipeline.py
      - orchestrator/grouping/plan_reader.py
      - tests/test_plan_reader.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u7-price-mode
    description: group --price prints per-task node work, per-slice sums vs cap, and budget parameters sub-second without codegraph
    slice: null
    files:
      - orchestrator/cli.py
      - orchestrator/grouping/estimator.py
      - tests/test_cli_price.py
    size_hints:
      tests/test_cli_price.py: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: ["planning-contract"]
  - task_id: u8-budget-naming
    description: Node work vs coder work named distinctly everywhere, slice error prints both with the multiplier
    slice: null
    files:
      - orchestrator/grouping/pipeline.py
      - orchestrator/cli.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: [u6-error-accumulation]
    implements: []
    consumes: []
  - task_id: u9-partition-diagnostics
    description: Degenerate-partition errors report inferred vs declared edge provenance; slice-re-entrant paths named with the exact edit
    slice: null
    files:
      - orchestrator/grouping/pipeline.py
      - orchestrator/grouping/graphing.py
      - orchestrator/grouping/partition.py
      - tests/test_partition.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: [u6-error-accumulation]
    implements: []
    consumes: []
  - task_id: u10-cycle-repair-withdrawal
    description: Group-DAG cycle repair withdraws inferred precedence edges before resorting to merges
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/graphing.py
      - orchestrator/grouping/pipeline.py
      - tests/test_partition.py
    symbols: []
    depends_on: [u9-partition-diagnostics]
    implements: []
    consumes: []
  - task_id: u11-advisory-report
    description: group --advise computes all granularities and cohesion diagnostics off one built graph into a preview Advisory Report
    slice: null
    files:
      - orchestrator/grouping/advisory.py
      - orchestrator/grouping/pipeline.py
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/scorecard.py
      - orchestrator/cli.py
      - tests/test_advisory.py
    size_hints:
      orchestrator/grouping/advisory.py: large
      tests/test_advisory.py: large
    symbols: []
    depends_on: []
    implements: ["advisory-report"]
    consumes: []
  - task_id: u12-fill-penalty
    description: Merge key gains a fill/balance term so one group stops absorbing merges until it hits the cap
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - orchestrator/config.py
      - tests/test_partition.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u13-plan-skill-advise-phase
    description: orchestrator-plan's hand-off runs --advise, presents diagnostics, and asks the user about splitting
    slice: null
    files:
      - skills/orchestrator-plan/SKILL.md
    symbols: []
    depends_on: [u5-planning-contract-docs, u11-advisory-report]
    implements: []
    consumes: ["advisory-report", "planning-contract"]
```

## Post-eval-harness backlog (do not implement now)

Recorded per the 2026-08-28 grill so nothing is lost when the R20 eval harness
lands (post-Infinity-Skills):

- **Hyperedge / hub-routing re-modeling of symbol-derived edges** (R19b):
  model dense shared symbols as affinity hyperedges or route them through the
  hub machinery at graphing time instead of pairwise precedence — one of the
  first experiments to run against the eval harness. Until then U10's
  withdrawal is the mitigation.
- **dagP-style acyclicity-safe refinement pass** (moving nodes back out of
  oversized groups) — the pipeline currently has no refinement stage; U12's
  fill penalty is the cheap precursor whose effect the harness should measure.
- **Cross-group read-overlap-aware merging** (brainstorm non-goal): pricing
  shared read context across candidate merges.
- **Fill-penalty calibration**: `target_fill_ratio`'s default is a judgment
  call in U12; the harness's estimator-calibration metric should tune it.
- **`--advise` cross-invocation graph cache** keyed by (plan sha, index
  fingerprint) — only if measured re-run latency violates R14 in practice.

## Requirement coverage

R1→U1+U2, R2→U2, R3→superseded (ADR 0006, U4), R4→U2+U4 (default path is
assembled; `preview/` semantics untouched), R5→U6, R6→U7+U5, R7→U8, R8→U9,
R9→U5, R10→U9, R11→U10, R12→U11, R13→U11, R14→U11, R15→U13, R21→U1+U5,
R22→U3, R23→U3 (byte-stable digest-first base context; fork architecture
already places it first). R16–R20 out of scope (waves 2–3).
