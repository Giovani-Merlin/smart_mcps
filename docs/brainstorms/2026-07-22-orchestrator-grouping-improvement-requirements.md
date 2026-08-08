---
date: 2026-07-22
topic: orchestrator-grouping-improvement
---

# Orchestrator Grouping Improvement — Requirements

## Summary

Make the grouper produce the groups the planner intended and explain every
decision it makes. Three behaviour changes: greenfield work estimation stops
being pure file count (D4 — task-map size hints plus a lower prospective-file
default), slice integrity becomes an invariant of the pipeline output rather
than a preference that survives only through Louvain (D2/H1 — with a loud,
named error and an explicit override when a slice alone cannot fit the
budget), and group-DAG cycles are repaired by merging quotient SCCs instead of
failing the run (D3/H5). One observation change: every `group` invocation
writes a versioned, stage-by-stage **grouping trace** sidecar artifact —
purely observational, changing no partition behaviour — that both the
`--no-spec` CLI printout and a future Observatory view render. The task-map
contract and the `/orchestrator-plan` skill are updated in lockstep so plans
actually carry the new signals.

## Problem Frame

Writing one greenfield plan took five `group --dry-run` rounds, three failing
with `dependency cycle across groups`; in both successful rounds **all three
vertical slices dissolved** into horizontal layers. `obs1` reproduced the
dissolution exactly — it is the grouper's stable behaviour on cross-stack
plans, not a fluke. The mechanism is fully understood and verified
(`docs/orchestrators_improvements.md`, D2/D3/D4): greenfield `node_work` is
`2000 × file_count`, inflated work triggers `split_over_budget`, the splitter
runs on the expanded node set with no slice knowledge, cross-stack slices have
no other cohesion force, and the cut direction occasionally inverts a
dependency edge in the quotient graph — which `build_group_dag` reports as a
cycle, blaming the plan for a problem the partitioner created. Meanwhile
nothing in `groups.json` explains *why* a node landed where it did
(`GroupingResult` carries only `flags: list[str]`), so every "why did it group
that way" question costs a debugging session. The operator's front-end branch
needs a machine-readable explanation artifact to visualize.

Prior art: the register's slice-dissolution study (H1–H5),
`docs/brainstorms/2026-07-22-orchestrator-run-hardening-requirements.md`
(whose D5 harness this scope builds on), ADR 0002 (per-run snapshots),
`docs/orchestrator-task-map.md` (the contract this scope amends).

## Dependencies

This scope starts **after** run-hardening's partition harness lands:
`--no-spec` (RH-R18/R19), the deterministic fixture plans and recorded
baseline (RH-R20), and the property tests (RH-R21). "RH-Rn" below refers to
that document's requirement IDs. The H2 sweep and every acceptance criterion
here are asserted through that harness.

## Key Decisions

- **H3 is rejected; verticals are the goal.** Accepting layer-shaped groups
  would contradict the operator's token-optimality strategy (few large
  vertical task groups) and would delete the only channel by which planner
  intent reaches the grouper. obs1 shipping with layers proves layers are
  *survivable*, not that they are *desired*.
- **H2 and H1 together, with the estimator fix carrying the load.** The
  estimator is fixed first (size hints + lower prospective default) so slices
  fit and the splitter rarely fires; slice integrity is then stated as an
  output **invariant** — no group may contain a strict subset of a slice —
  enforced by whatever stages need it. If the estimator fix alone satisfies
  the invariant on the fixtures, the splitter needs only the assertion; if
  not, the splitter/merger become slice-aware. The requirement is the
  invariant, not the mechanism.
- **Slice overflow is a loud, named error with an explicit override.**
  Mirrors RH-R14's `--allow-unknown-symbols` pattern: hard error by default
  (naming the slice, members, per-node work, cap, overshoot), an override
  that keeps the slice intact as one flagged over-budget group. Never a
  silent split. *Rejected:* warn-only (silently trades away warm-context
  economy); no-override (greenfield estimates are guesses; a 5% overshoot
  should not force plan surgery).
- **Cycles are repaired by merging quotient SCCs at `build_group_dag` time.**
  Deterministic, provably terminating, and covers cycles from *any* stage —
  round A's cycle came from Louvain parking a no-affinity node, not from the
  splitter, so cut-level fixes are insufficient. After repair, acyclicity is
  an internal invariant: a surviving cycle is an orchestrator bug, never a
  plan-shape error. *Rejected:* back-off-the-offending-cut (splitter-only
  coverage); convex-cut prevention only (leaves Louvain-caused cycles
  raising).
- **Explainability is purely observational.** The algorithm is not changed to
  be explainable; it is instrumented. A versioned sidecar artifact
  (`grouping-trace.json`) records each stage's output partition and each
  decision with its quantitative context. The RH-R18 `--no-spec` printout
  renders from this same structure — one data model, two consumers (CLI now,
  Observatory branch later). *Rejected:* embedding in `groups.json` (couples
  run schema to explain schema); log lines (front-end would parse text).
- **The plan skill and task-map contract move in lockstep.** Size hints are
  useless if `/orchestrator-plan` never emits them; the skill doc and
  `docs/orchestrator-task-map.md` are updated in the same scope, and the
  skill's validation step switches to the sub-second partition-only path.

## Requirements

### Estimator — greenfield pricing (D4/H2)

- R1. The task map's `*(new)*` marker accepts an optional size hint —
  `*(new, small)*`, `*(new, medium)*`, `*(new, large)*` — parsed by
  `parse_task_map`; hintless `*(new)*` remains valid, so existing plans parse
  unchanged.
- R2. `EstimatorConfig` gains token prices for the three hint classes and a
  `prospective_file_allowance` default (lower than `per_file_tool_allowance`)
  for unhinted prospective files. Existing-file pricing is unchanged.
- R3. The H2 sweep runs before defaults are frozen: the greenfield fixtures
  and the real Observatory plan are re-partitioned across a sweep of
  prospective allowances via the partition-only path; results (which slices
  survive at which values) are recorded in the baseline table, and the
  shipped defaults are chosen from that evidence.

### Slice integrity (D2/H1)

- R4. **Invariant:** no group in the pipeline output contains a strict subset
  of a slice. Slices land whole, wherever they land. Enforced by whatever
  stages require it (at minimum an output assertion; slice-aware
  `split_over_budget`/`merge_small_groups` if the estimator fix alone does
  not satisfy the fixtures).
- R5. A slice whose own summed `node_work` exceeds the partition budget cap
  fails `group` with an error naming the slice, its members, the per-node
  work breakdown, the cap, and the overshoot — the planner's fix (split the
  slice or raise the budget) must be readable from the error alone.
- R6. An explicit override (CLI flag + config key) keeps an over-budget slice
  intact as one group instead of erroring; the acceptance is recorded in
  `flags[]` and the trace. There is no silent-split path under any setting.
- R7. `docs/orchestrator-task-map.md` is updated: the must-link is an output
  invariant (superseding RH-R22's softness correction), and the overflow
  error + override are documented.

### Cycle repair (D3/H5)

- R8. `build_group_dag`-time repair: groups forming a cyclic SCC in the
  quotient graph are merged into one group, deterministically.
- R9. A repair-merged group exceeding the cap is re-split in a
  dependency-safe mode that cannot introduce new quotient cycles; if no
  within-budget acyclic split exists, the group stays over budget with a
  `flags[]` warning.
- R10. After repair, acyclicity is an internal invariant: `GroupCycleError`
  surviving repair is reported as an orchestrator bug, never as a plan-shape
  error. Every repair (merged groups, evidence edges) is recorded in the
  trace and `flags[]`.

### Grouping trace — explainability

- R11. Every `group` invocation (with or without `--no-spec`) writes
  `grouping-trace.json` next to `groups.json`; the per-run snapshot copies it
  into the run directory alongside `groups.json` (ADR 0002 parity).
- R12. The trace is pydantic-modeled and carries a `schema_version`; the
  schema is the published contract the Observatory branch renders.
- R13. Trace contents, stage by stage — inputs first: the task graph (nodes,
  affinity edges with weights, dependency edges), per-node `node_work` with
  its components (source bytes, file counts, hint prices), the budget-cap
  arithmetic, and the effective config. Then, for each stage (hub roles,
  slice atoms, contraction, Louvain, lift, expansion, split, merge, SCC
  repair, renumber): the partition after the stage, plus each decision with
  its quantitative context — hub scores vs threshold, slice members and hub
  exclusions, communities and resolution, every cut edge with its weight and
  the compared alternatives and the budget arithmetic that triggered it,
  every merge candidate accepted or rejected with the reason, every repair
  with its evidence edges.
- R14. Per-group difficulty is explained: each group's `DifficultySignals`,
  the resulting score, and the thresholds that selected its review intensity
  are recorded in the trace.
- R15. Recording is purely observational: no partition code path reads trace
  state, and the partition output is byte-identical with and without a
  recorder attached (asserted by test).
- R16. On grouping failure (slice overflow, any hard error after graph
  construction) the trace is still written up to and including the failure,
  with the failure recorded — explaining failures is a primary use.
- R17. The RH-R18 `--no-spec` printout is rendered from the trace structure —
  one source of truth for CLI and front-end.
- R18. The trace is byte-stable across identical runs (extends RH-R21's
  determinism property).

### Plan-side lockstep

- R19. The `/orchestrator-plan` skill doc is updated to emit size hints on
  prospective files and to plan slices under the hard-invariant semantics
  (including the overflow error's meaning and remedies).
- R20. The skill's grouping-validation step uses the partition-only path
  (`--no-spec`) so "will this plan group?" is answered sub-second in-skill
  before the plan is finalized; the speccer is paid only on the final
  accepted shape.

### Acceptance — asserted through the RH-R20 fixtures

- R21. On `greenfield-cross-stack` and the re-grouped real Observatory plan:
  every slice lands intact in one group.
- R22. Every fixture shape that cycles at baseline groups cleanly via repair;
  no `GroupCycleError` reaches the user on any fixture.
- R23. Every group is within the cap except explicit-override and
  repair-flagged cases, which carry `flags[]` entries.
- R24. Control fixtures (`pure-backend`, brownfield variants) do not regress
  against the recorded baseline.
- R25. The original five-round loop is closed: the Observatory plan's round-A
  shape (the one that cycled) groups successfully on the first attempt.
- R26. For every node in every fixture, "why is this node in this group" is
  answerable from the trace alone (stage snapshots + decisions), verified by
  a test that reconstructs each node's stage-by-stage path.

## Non-Goals

- **No H4.** No task-map syntax for declaring group membership directly and
  no grouper-as-validator mode. Revisit only if verticals still dissolve
  after this scope lands.
- **No front-end work.** The Observatory visualization of the trace lives on
  its own branch; this scope ends at the versioned artifact and the CLI
  renderer.
- **No mapper-LLM fallback changes.** The LLM mapper path keeps its current
  behaviour, covered only by RH-R26's opt-in test.
- **No counterfactual re-runs.** The trace records what happened, not
  hypothetical alternate partitions; stage snapshots supply the useful diffs.
- **No speccer or breaker changes.**

## Open Questions

None — every decision above was resolved with the operator on 2026-07-22.
The dependency-safe re-split algorithm (R9) is an implementation choice for
planning time, bounded by its requirement (no new cycles, deterministic), not
an open decision.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-07-22-orchestrator-grouping-improvement-requirements.md`
— after the run-hardening plan's D5 harness units have landed.
