---
title: Orchestrator grouping — correctness, explainability, and addressable groupings
type: feat
date: 2026-07-25
origin: docs/brainstorms/2026-07-22-orchestrator-grouping-improvement-requirements.md
---

# Orchestrator grouping — correctness, explainability, and addressable groupings

## Objective

Make the grouper produce the groups the planner declared, explain every decision
it makes, and let a grouping be named and selected instead of being the single
anonymous slot in the repo.

Measured outcome, asserted through the RH-R20 fixture harness and the two real
plans in this repo:

- every declared slice lands whole in exactly one group, or grouping fails
  naming the slice (R4–R6, R21);
- no fixture reaches the user as `GroupCycleError` (R22, R25) — today four of
  six cycle;
- every `group` invocation writes a versioned `grouping-trace.json` from which
  "why is this node in this group" is answerable without a debugging session
  (R11–R18, R26);
- `group --name <tag>` / `run --grouping <tag>` replace the overwrite-the-single-
  slot model that cost the Observatory its DAG (new scope, this plan);
- `group` works against the real codegraph CLI at all — a P0 found while
  planning and **already fixed on this branch**, so the plan builds on it rather
  than scheduling it.

Origin requirements R1–R26 are covered; the mapping is in **Requirement
coverage** below. The origin's causal model was falsified during exploration —
see **What we already know**.

## What we already know (resolved context)

### The P0: `group` is broken against the real codegraph CLI

`CodegraphClient._run` (`orchestrator/grouping/graphing.py:163-181`) appends
`-p <repo_root>` to **every** invocation, but `codegraph sync` takes a
*positional* path and rejects `-p`:

```
$ uv run smart-mcps-orchestrate group docs/plans/2026-07-22-001-…-plan.md --no-spec
error: codegraph sync failed (1): error: unknown option '-p'

$ codegraph sync --help
Usage: codegraph sync [options] [path]
```

Every real `group` invocation died at `client.sync()`
(`orchestrator/grouping/pipeline.py:118`, the RH-R13 gate). The whole test suite
passed because every grouping test injects a stub runner
(`tests/test_grouping_fixtures.py:26`), so the sync gate had never executed
against the real CLI — the injected-runner seam hid it completely.

**Fixed on this branch before the plan shipped**, because nothing else here is
verifiable on a real plan without it: `CodegraphClient._argv` now places the
path positionally for the index-maintenance commands
(`POSITIONAL_PATH_COMMANDS`) and keeps `-p, --path` for the query commands,
covered by two argv tests in `tests/test_graphing.py`. Suite: 342 passing.
Implementers should treat this as existing behaviour, not as work to redo.

### The origin's causal model is wrong, and the measurements say why

The brainstorm attributes slice dissolution to the budget splitter (D2) fed by
file-count-inflated greenfield work (D4), and prescribes "the estimator fix
carrying the load". Sweeping `per_file_tool_allowance` over `[2000 … 100]`
through the partition-only path falsifies that:

| plan                                            | 2000                        | 1000              | 600           | 400                 |
| ----------------------------------------------- | --------------------------- | ----------------- | ------------- | ------------------- |
| observatory (greenfield, ~50 prospective files) | 2 of 3 slices SPLIT, cycles | SPLIT, cycles     | SPLIT, cycles | SPLIT, cycles       |
| run-hardening (brownfield, real files)          | both slices intact          | 1 slice **SPLIT** | SPLIT         | SPLIT               |
| the four cycling fixtures                       | cycle                       | cycle             | cycle         | cycle (also at 100) |

Pricing never restored a slice, and *lowering* it dissolved one that had
survived — cheaper nodes let `merge_small_groups` build larger clusters, which
then exceed the cap and get cut. There are three independent mechanisms, and
the origin named only the third:

**M1 — hub-role exclusion deletes the slice before contraction runs.**
`slice_atoms` (`orchestrator/grouping/partition.py:260-274`) skips every node
whose role is not `core`, then keeps only labels with ≥2 survivors. On the real
Observatory plan:

```
slice ATOMS actually contracted: {}

u4-sse-endpoints:    live-board  (role=aggregator_hub)
u5-board-and-log-ui: live-board  (role=core)
u6-escalation-api:   hitl        (role=aggregator_hub)
u7-escalation-ui:    hitl        (role=core)
u8-transcript-api:   drill-in    (role=aggregator_hub)
u9-drill-in-ui:      drill-in    (role=core)
```

All three slices dropped to one member and **none was contracted**. The
splitter never touched them; `last_stage` was `merge`, not `split`. The backend
half of a vertical slice naturally has many upstreams, and `detect_hub_roles`
(`partition.py:230-257`) thresholds on a *fraction of all tasks*
(`deps/(n-1) > 0.4`), so in a 10-unit plan four upstreams is a hub. The
"horizontal backend layer" everyone observed is literally
`_hub_isolated_clustering`'s single trailing aggregator bucket
(`partition.py:355-385`).

**M2 — `merge_small_groups` creates the cycles.** Its guards
(`partition.py:589-676`) are budget, `chain_compatible`, and makespan — none of
them acyclicity. Merging an upstream hub with a downstream aggregator *across*
an intermediate group inverts an edge in the quotient graph. On
`greenfield-cross-stack` the merged group is `{auth-api, auth-ui, scaffold, verify}`: `scaffold → items-*` runs one way, `items-* → verify` the other.

**M3 — `split_over_budget` cuts inside an expanded slice.** Real, and the origin
describes it correctly, but it is the *secondary* mechanism: it explains the
brownfield run-hardening regression at ≤1000 and the `slice-over-budget`
fixture, not the Observatory dissolution.

### What the fixes measure out to

Prototyped against both real plans and all six fixtures (partition-only path,
cycle detection neutralized so the partition is observable):

| variant                                | observatory slices | fixtures cycling | run-hardening @1000 |
| -------------------------------------- | ------------------ | ---------------- | ------------------- |
| baseline                               | 1 of 3 intact      | 4 of 6           | slice SPLIT         |
| slice-wins atoms                       | **3 of 3**         | 4 of 6           | slice SPLIT         |
| + also splitting the aggregator bucket | 3 of 3             | 4 of 6           | slice SPLIT         |
| + acyclic merge guard                  | 3 of 3             | **0 of 6**       | slice SPLIT         |
| + slice-aware splitter (**the plan**)  | 3 of 3             | **0 of 6**       | **intact**          |

Splitting the aggregator bucket changes nothing, so `_hub_isolated_clustering`
stays as it is. With all three fixes the six fixtures are acyclic with every
slice intact, `slice-over-budget` keeps its slice whole at 4,000 work against a
3,939 cap (exactly the R5 overflow case), and run-hardening groups cleanly at
both 1000 and 2000.

One cycle survives: the Observatory plan, where the cycle originates at
Louvain/lift/split rather than at merge, so the guard cannot prevent it. Both
candidate repairs — collapsing the cyclic SCC, and ejecting the cheapest
cycle-closing node — converge on the same 2-group result with a
**100,678-work group against an 83,602 cap**. That is 20% over, and above the
breaker's 120k context limit once the base head is added. So R9's
dependency-safe re-split is not an optional refinement; it is the difference
between a usable partition and one that respawns workers immediately.

### The single-slot artifact problem

`_cmd_group` writes `.orchestrator/groups.json` and `.orchestrator/base-context.md`
(`orchestrator/cli.py:294-298`); `_cmd_run` reads exactly those two paths
(`cli.py:379-389`). Grouping a second plan overwrites the first with no record.
ADR 0002 records the loss this caused the Observatory and prescribes a per-run
snapshot — **which is implemented only on `feat/observatory`** (its `cli.py:420`);
`_cmd_run` on this branch never writes it, so `CONTEXT.md`'s "Run Directory"
entry currently describes a file that does not exist here.

Readers of the shared file are few and known: `_cmd_run` (`cli.py:380`),
`tests/test_cli.py:56`, `tests/test_e2e_stub.py:166,267`,
`tests/test_grouper_pipeline.py:506,621`, and the Observatory's `load_dag`
fallback on its own branch.

### Landed harness this plan builds on

`compute_partition` / `PartitionOutcome` (`pipeline.py:100-171`) is the
sub-second, zero-LLM prefix; `--no-spec` renders it (`cli.py:310-348`); six
fixture plans plus determinism and cap properties exist
(`tests/test_grouping_fixtures.py`). `DefaultPartitionStrategy.last_stage`
already records which stage last changed membership.

### Plan-shape constraints learned the hard way

Listing map `symbols` on a plan whose units co-edit hub files (here
`partition.py`, `cli.py`) turns every symbol's callers/callees into directed
call and impact edges (`graphing.py:256-289`), forming a bidirectional web no
partition can quotient acyclically. This plan therefore ships `symbols: []`
throughout — `depends_on` ordering and shared-file affinity are unaffected.
Doc-only units carry no edges in or out.

## Decisions

- **A declared slice outranks an inferred hub role.** `slice_atoms` stops
  filtering on role: every declared member joins its atom. The planner bound
  those tasks explicitly; hub classification is an inference from degree ratios
  that, at plan scale, fires on four upstreams. Hub isolation continues to apply
  to every task carrying no slice label. *Alternatives rejected:* excluding only
  `utility_hub` (fixes the observed case but leaves a silent deletion path for
  any slice whose member happens to be a source); retuning `detect_hub_roles`
  thresholds (indirect — slices survive only when the new threshold happens to
  spare them, and it perturbs every plan's grouping).

- **Cycles are prevented at merge *and* repaired at DAG build.** Prevention
  alone leaves the Louvain/split-origin cycle on the real Observatory plan;
  repair alone lets the merger build sandwiches that then collapse into
  over-cap groups. Measured: prevention takes the fixtures from 4-of-6 cycling
  to 0-of-6; repair covers what is left. *Alternative rejected:* repair only
  (the origin's D3/H5 reading) — it converts a preventable cycle into a
  100,678-work group.

- **Repair is SCC-merge followed by a mandatory dependency-safe re-split.**
  Merging the cyclic SCC is deterministic and provably terminating; the re-split
  is what keeps the result inside the cap. Both candidate repairs measured
  identically on the real plan, so the simpler one wins. A group that still
  exceeds the cap after re-split ships with a `flags[]` entry rather than
  failing — greenfield estimates are guesses and a hard failure here would be
  unactionable.

- **Slice integrity is an output invariant, enforced at the stage that breaks
  it.** `split_over_budget` treats a slice as one indivisible block (cut
  candidates are computed between blocks, never inside one); the pipeline
  asserts the invariant on its final partition. A slice whose own work exceeds
  the cap is a loud, named error with an explicit override, never a silent
  split. *Alternative rejected:* output assertion only — it would turn every
  splitter cut into a crash instead of a correct partition.

- **The partition-core units carry no slice label, because they cannot fit one
  group.** U2–U6 all edit `partition.py` and would ideally be one worker, but
  their summed `node_work` is ~146,000 against a measured cap of ~84,000 — under
  this plan's own R5 rule that is a slice-overflow error, so declaring it would
  be incoherent. Their ordering is already fully expressed by `depends_on`
  (u2 → u3 → u5, u4 → u5, u3 → u6), and their shared `partition.py` gives them
  the strongest affinity signal in the plan, so they cluster and schedule
  serially without the label. `grouping-trace` (u8 + u9, ~51,000) stays a slice:
  it fits.

- **Greenfield pricing gains precision, not a lower rate.** Size hints price
  `small`/`medium`/`large` at 500/2000/5000, with unhinted prospective files
  staying at today's 2000 — i.e. today's rate *is* medium. The sweep showed
  pricing never restored a slice and that a blanket reduction dissolved one, so
  the estimator is not load-bearing for slice integrity; it is load-bearing for
  honesty, and a flat rate that charges `tsconfig.json` like a 400-line module
  is dishonest in both directions. The sweep table ships as evidence in
  `docs/orchestrator-grouping.md`, not as a default-setting mechanism.
  *Alternative rejected:* the origin's R2 lower `prospective_file_allowance` —
  measured to *cause* a splitter cut on the brownfield plan.

- **Size hints ride a `size_hints` map key, not the `*(new)*` prose marker.**
  The origin's R1 phrasing ("the task map's `*(new)*` marker accepts an optional
  size hint") does not typecheck: `*(new)*` is a plan-prose convention, while the
  map is YAML whose `files:` is a plain path list. The additive, v1-compatible
  carrier is a sibling `size_hints: {path: class}` mapping, which the prose
  marker `*(new, large)*` renders 1:1. **This plan's own map cannot use it** —
  the parser gains the key in U7 — so its prose marks prospective files with a
  bare `*(new)*`.

- **A grouping is a named, self-contained directory.**
  `.orchestrator/groupings/<name>/` holds `groups.json`, `base-context.md` and
  `grouping-trace.json`; `group --name` writes one, `run --grouping` selects
  one. `run` with no flag auto-selects only when exactly one exists and
  otherwise errors listing every candidate — never "the newest", which is the
  implicitness that cost the Observatory its DAG. Nothing writes the top-level
  `.orchestrator/groups.json` any more; a stale one is reported, never silently
  consumed. (→ ADR 0003)

- **The run snapshots the whole grouping directory.** ADR 0002's premise
  survives naming — `group --name <same>` still rewrites a finished run's
  history — so `run` copies the directory's files into `runs/<run_id>/` and
  `resume` reads the snapshot. Copying the directory rather than an enumerated
  list means the trace is covered without U10 depending on U9.

- **The trace carries no timestamp.** R18 demands byte-stability across
  identical runs, which a `created_at` field would break. File mtime and the
  run snapshot supply time; the trace supplies content.

- **The trace is written in every mode, including `--dry-run` and `--no-spec`.**
  Explaining a partition you chose not to materialize is the main iteration
  loop, and explaining a *failure* is a primary use (R16). Those modes write
  `grouping-trace.json` and nothing else, leaving today's "no groups.json from
  a dry run" contract intact.

- **Per-group difficulty (R14) is recorded only on the full path.**
  `DifficultySignals` needs `verification_items`, which exist only after the
  speccer. The partition-only trace carries `groups: []`; `run_grouping` fills
  it. The schema is one shape with an optionally-empty section, not two schemas.

## Units

### U2. slice-atoms-hub-independence — a declared slice survives hub classification

- **Goal**: `slice_atoms` returns every declared slice with ≥2 members
  regardless of the members' hub roles; hub isolation still applies to
  slice-less tasks.
- **Files**: `orchestrator/grouping/partition.py`, `tests/test_partition.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: — *(see Decisions: the set cannot fit one group)*
- **Implements / Consumes**: —
- **Verification**:
  - Given a graph where one member of a two-task slice is classified
    `aggregator_hub`, `slice_atoms` returns that slice with both members.
  - Given a graph where a slice-less task is classified `utility_hub`, it still
    lands in its own group in the partition output.
  - Partitioning a graph whose slice members are all hub-classified places every
    member of each slice in one group.

### U3. slice-aware-splitter — the budget splitter cannot cut inside a slice

- **Goal**: `split_over_budget` computes cut candidates between indivisible
  blocks, where a block is a slice atom or a lone node, so an over-budget group
  is cut around slices instead of through them.
- **Files**: `orchestrator/grouping/partition.py`, `tests/test_partition.py`,
  `tests/test_grouping_fixtures.py`
- **Symbols**: —
- **Depends-on**: U2
- **Slice**: — *(see Decisions: the set cannot fit one group)*
- **Implements / Consumes**: —
- **Verification**:
  - An over-budget group containing a two-task slice plus two loose tasks splits
    into groups where the slice's two tasks are still together.
  - A group that is over budget and contains exactly one block stays whole (the
    single-node passthrough behaviour is preserved).
  - The `slice-over-budget` fixture yields a group containing both
    `reports-api` and `reports-ui`.
  - Re-partitioning the same fixture twice produces byte-identical partitions.

### U4. acyclic-merge-guard — no merge may create a group-level cycle

- **Goal**: `merge_small_groups` rejects any candidate merge whose resulting
  quotient graph contains a cycle, alongside its existing budget,
  chain-compatibility and makespan guards.
- **Files**: `orchestrator/grouping/partition.py`, `tests/test_partition.py`,
  `tests/test_grouping_fixtures.py`
- **Symbols**: —
- **Depends-on**: U2
- **Slice**: — *(see Decisions: the set cannot fit one group)*
- **Implements / Consumes**: —
- **Verification**:
  - On a graph shaped as source-hub → feature → sink-aggregator, merging the
    source with the sink is rejected and the three groups remain distinct.
  - `greenfield-cross-stack`, `brownfield-cross-stack`, `hub-in-the-middle` and
    `no-affinity-sink` each partition without raising `GroupCycleError`.
  - `pure-backend` still produces two groups with both slices intact (no
    control regression).
  - A merge that is acyclic, in budget and makespan-neutral is still accepted
    (the guard does not disable merging).

### U5. scc-repair-and-resplit — a surviving cycle is repaired, then re-fitted

- **Goal**: `build_group_dag` merges each cyclic SCC deterministically, then a
  dependency-safe re-split brings the merged group back inside the cap without
  reintroducing a cycle; acyclicity becomes an internal invariant and a
  surviving `GroupCycleError` is reported as an orchestrator bug.
- **Files**: `orchestrator/grouping/partition.py`,
  `orchestrator/grouping/pipeline.py`, `tests/test_partition.py`,
  `tests/test_grouping_fixtures.py`,
  `tests/fixtures/grouping/observatory-round-a.md` *(new)*
- **Symbols**: —
- **Depends-on**: U3, U4
- **Slice**: — *(see Decisions: the set cannot fit one group)*
- **Implements / Consumes**: —
- **Verification**:
  - A partition whose quotient graph cycles is returned acyclic, with the
    formerly-cyclic groups' tasks all present exactly once.
  - After repair of an SCC whose merged work exceeds the cap, the resulting
    groups are each within the cap and the quotient graph is still acyclic.
  - When no within-budget acyclic re-split exists, the group is returned over
    budget and a `flags[]` entry names it and states the overshoot.
  - The new `observatory-round-a` fixture — an SPA hub depending on a backend
    hub, three two-task cross-stack slices, and a verification task depending on
    all three — partitions without raising, with all three slices intact.
  - Repairing the same graph twice produces byte-identical partitions.
  - Every group produced from every fixture is within the cap except those
    carrying an explicit `flags[]` entry.

### U6. slice-overflow-gate — a slice that cannot fit fails loudly, or is kept whole on request

- **Goal**: the pipeline asserts that no group contains a strict subset of a
  slice; a slice whose own summed work exceeds the cap raises a `GrouperError`
  naming the slice, its members, per-member work, the cap and the overshoot,
  with `--allow-oversized-slice` / `[partition] allow_oversized_slice` keeping
  it whole as one flagged group instead.
- **Files**: `orchestrator/grouping/partition.py`,
  `orchestrator/grouping/pipeline.py`, `orchestrator/config.py`,
  `orchestrator/cli.py`, `tests/test_grouping_fixtures.py`,
  `tests/test_cli.py`
- **Symbols**: —
- **Depends-on**: U3
- **Slice**: — *(see Decisions: the set cannot fit one group)*
- **Implements / Consumes**: —
- **Verification**:
  - Grouping a plan whose slice alone exceeds the cap exits non-zero with a
    message containing the slice label, every member id, each member's work, the
    cap, and the overshoot amount.
  - The same plan with `--allow-oversized-slice` exits 0, places the whole slice
    in one group, and records a `flags[]` entry naming the accepted overshoot.
  - No configuration produces a partition in which a slice's members are spread
    across more than one group.
  - `[partition] allow_oversized_slice = true` in `.orchestrator/config.toml`
    has the same effect as the flag.

### U7. size-hints — prospective files can be priced by declared size

- **Goal**: the task map accepts `size_hints: {path: small|medium|large}`,
  priced 500/2000/5000 by `EstimatorConfig`, with unhinted prospective files
  unchanged at `per_file_tool_allowance`; existing-file pricing untouched.
- **Files**: `orchestrator/grouping/plan_reader.py`,
  `orchestrator/grouping/estimator.py`, `orchestrator/config.py`,
  `tests/test_plan_reader.py`, `tests/test_estimator.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `size_hints`
- **Verification**:
  - A map declaring `size_hints` for a prospective file yields a `node_work`
    equal to that class's price plus the task's other file allowances.
  - A map with no `size_hints` key produces exactly today's `node_work` values
    for the same plan (existing plans do not change shape).
  - `size_hints` naming a path absent from that task's `files` is a
    `TaskMapError`; an unknown size class is a `TaskMapError`.
  - A hint on an existing (non-prospective) file is a `TaskMapError` naming the
    file — hints price unwritten work only.
  - `estimate_group_tokens` output is unchanged for a group with no hinted files.

### U8. trace-model-and-recorder — every stage and decision is recorded

- **Goal**: a versioned pydantic trace model plus a recorder threaded through
  hub roles, slice atoms, contraction, Louvain, lift, expansion, split, merge,
  SCC repair and renumber — capturing the input graph, per-node work with its
  components, the budget arithmetic, the effective config, each stage's
  resulting partition, and each decision with its quantitative context
  (hub scores vs threshold, slice members, communities and resolution, every cut
  edge with its weight and the compared alternatives, every merge candidate
  accepted or rejected with the reason, every repair with its evidence edges).
- **Files**: `orchestrator/grouping/trace.py` *(new)*,
  `orchestrator/grouping/partition.py`, `orchestrator/grouping/pipeline.py`,
  `tests/test_grouping_trace.py` *(new)*
- **Symbols**: —
- **Depends-on**: U5
- **Slice**: grouping-trace
- **Implements / Consumes**: implements `GroupingTrace`
- **Verification**:
  - Partitioning every fixture with a recorder attached and without one produces
    byte-identical partitions.
  - The recorded trace for `greenfield-cross-stack` contains one stage entry per
    executed stage, each with the partition after it.
  - For every node in every fixture, the node's group can be reconstructed by
    replaying the trace's stage partitions in order, and the reconstruction
    matches the final partition.
  - Every rejected merge candidate appears with a reason string drawn from a
    closed set (`over_budget`, `not_chain_compatible`, `makespan_regression`,
    `would_create_cycle`).
  - The model exposes `schema_version` and round-trips through
    `model_validate_json(model_dump_json())` unchanged.
  - The trace contains no timestamp field, and serializing the same fixture's
    trace twice yields identical bytes.

### U9. trace-artifact-and-cli — the trace is written, survives failure, and renders the report

- **Goal**: every `group` invocation writes `grouping-trace.json` into the
  grouping directory — including `--dry-run`, `--no-spec`, and failed runs,
  where the partial trace records the failure — and `--no-spec` renders its
  report from the trace structure rather than from `PartitionOutcome` fields.
- **Files**: `orchestrator/grouping/trace.py`,
  `orchestrator/grouping/pipeline.py`, `orchestrator/cli.py`,
  `tests/test_grouping_trace.py`, `tests/test_cli.py`
- **Symbols**: —
- **Depends-on**: U8, U10
- **Slice**: grouping-trace
- **Implements / Consumes**: consumes `GroupingTrace`, consumes `grouping-directory`
- **Verification**:
  - `group <plan>` writes `.orchestrator/groupings/<name>/grouping-trace.json`
    alongside `groups.json`.
  - `group <plan> --dry-run` and `group <plan> --no-spec` each write
    `grouping-trace.json` and neither writes `groups.json` nor `base-context.md`.
  - A grouping that fails on slice overflow still leaves a
    `grouping-trace.json` whose failure section names the slice, and the CLI
    message points at the file.
  - The `--no-spec` printout after this change contains the same group listing,
    node work, budget cap, hub roles, slice atoms and last-stage lines as before.
  - Running `group --no-spec` twice on the same plan produces byte-identical
    trace files.
  - Grouping an over-budget slice with `--allow-oversized-slice` records the
    accepted overshoot in the trace as well as in `flags[]`.
  - `run_grouping` fills the trace's per-group section with each group's
    difficulty signals, score, and the thresholds that selected its intensity.

### U10. named-groupings — groupings are addressable, and runs snapshot the one they used

- **Goal**: `group --name <tag>` writes `.orchestrator/groupings/<tag>/`;
  `run --grouping <tag>` selects one, auto-selecting only when exactly one
  exists; a new `groupings` subcommand lists them; `run` snapshots the chosen
  directory into `runs/<run_id>/` and records the name in `RunManifest`;
  `resume` reads the snapshot.
- **Files**: `orchestrator/cli.py`, `orchestrator/execution/manifest.py`,
  `orchestrator/model.py`, `tests/test_cli.py`, `tests/test_e2e_stub.py`,
  `tests/test_grouper_pipeline.py`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `grouping-directory`
- **Verification**:
  - `group <plan> --name alpha` creates `.orchestrator/groupings/alpha/groups.json`
    and `.../base-context.md`, and writes no top-level `.orchestrator/groups.json`.
  - `group <plan>` with no `--name` uses the plan filename stem as the name.
  - A `--name` containing a path separator or `..` is rejected before anything
    is written.
  - With two groupings present, `run` exits non-zero and its message lists both
    names with their plan paths; with one present, `run` uses it.
  - With none present but a legacy top-level `groups.json` on disk, `run` exits
    non-zero naming that file and the command to re-group, and does not consume it.
  - `run --grouping alpha` copies `alpha/`'s files into
    `.orchestrator/runs/<run_id>/` and records `grouping: "alpha"` in
    `manifest.json`.
  - After `group --name alpha` is re-run against a different plan, `resume`
    of the earlier run still schedules the groups the run started with.
  - `groupings` lists each name with its plan path and group count.
  - Every existing assertion against the old top-level artifact paths — in
    `tests/test_grouper_pipeline.py` (the artifacts-written and dry-run-writes-
    nothing cases), `tests/test_cli.py` and `tests/test_e2e_stub.py` — is
    updated to the grouping directory and passes; the full suite is green.

### U11. docs-and-skill-lockstep — the contract, the guide, and the plan skill state the new semantics

- **Goal**: `docs/orchestrator-task-map.md` documents `size_hints` and the
  hard-invariant slice semantics with the overflow error and override;
  `docs/orchestrator-grouping.md` documents the named-grouping layout, the trace
  artifact, and carries the pricing sweep table; `skills/orchestrator-plan/SKILL.md`
  emits size hints and validates plans through `--no-spec`; the register records
  D2/D3/D4 as resolved with the corrected mechanisms. (`CONTEXT.md`'s glossary
  entries and ADR 0003 landed with this plan, not with its implementation.)
- **Files**: `docs/orchestrator-task-map.md`, `docs/orchestrator-grouping.md`,
  `skills/orchestrator-plan/SKILL.md`, `docs/orchestrators_improvements.md`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - `docs/orchestrator-task-map.md` describes `size_hints` with its three
    classes and their prices, and states the slice must-link as an output
    invariant with the overflow error and the override flag.
  - The task-map document no longer claims enforcement "holds only through
    Louvain".
  - `docs/orchestrator-grouping.md` shows the `.orchestrator/groupings/<name>/`
    layout, names the three artifacts, and contains the sweep table with a row
    per swept allowance for both real plans.
  - `skills/orchestrator-plan/SKILL.md` instructs plans to mark prospective
    files `*(new, small|medium|large)*` and to validate with
    `group <plan> --no-spec` before finalizing.
  - `docs/orchestrators_improvements.md` records D2/D3/D4 as resolved, naming
    the three measured mechanisms rather than the superseded splitter-only
    account.

## Requirement coverage

| Origin      | Where                                                                                                           |
| ----------- | --------------------------------------------------------------------------------------------------------------- |
| R1, R2 | U7 — R1's carrier corrected to `size_hints`; R2's lower `prospective_file_allowance` **rejected on evidence** (unhinted files keep today's rate, see Decisions)                                                           |
| R3          | U11 — the sweep ships as a recorded table; defaults are pinned by this plan, not derived at implementation time |
| R4          | U3 (enforcement), U6 (assertion)                                                                                |
| R5, R6      | U6                                                                                                              |
| R7          | U11                                                                                                             |
| R8, R9 | U5 |
| R10 | U5 (invariant + `flags[]`), U8 (repair decisions in the trace)                                                                                                              |
| R11         | U9 (write), U10 (snapshot)                                                                                      |
| R12, R13    | U8                                                                                                              |
| R14         | U8 (model), U9 (population on the full path)                                                                    |
| R15         | U8                                                                                                              |
| R16, R17    | U9                                                                                                              |
| R18         | U8 (byte-stable model), U9 (byte-stable file)                                                                   |
| R19, R20    | U11                                                                                                             |
| R21         | U2, U3, U5                                                                                                      |
| R22, R25    | U4, U5                                                                                                          |
| R23         | U5, U6                                                                                                          |
| R24         | U4                                                                                                              |
| R26         | U8                                                                                                              |

Scope added beyond the origin: U4 (cycle prevention) and U10 (named groupings);
the sync P0 was fixed outright rather than scheduled. Origin non-goals are unchanged — no H4, no front-end work, no
mapper-LLM changes, no counterfactual re-runs, no speccer or breaker changes.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u2-slice-atoms-hub-independence
    description: Slice atoms include every declared member regardless of hub role
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - tests/test_partition.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u3-slice-aware-splitter
    description: The budget splitter cuts between indivisible blocks, never inside a slice
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - tests/test_partition.py
      - tests/test_grouping_fixtures.py
    symbols: []
    depends_on: [u2-slice-atoms-hub-independence]
    implements: []
    consumes: []
  - task_id: u4-acyclic-merge-guard
    description: merge_small_groups rejects any candidate that would create a group-level cycle
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - tests/test_partition.py
      - tests/test_grouping_fixtures.py
    symbols: []
    depends_on: [u2-slice-atoms-hub-independence]
    implements: []
    consumes: []
  - task_id: u5-scc-repair-and-resplit
    description: Cyclic SCCs are merged and re-split dependency-safely; acyclicity becomes an internal invariant
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/pipeline.py
      - tests/test_partition.py
      - tests/test_grouping_fixtures.py
      - tests/fixtures/grouping/observatory-round-a.md
    symbols: []
    depends_on: [u3-slice-aware-splitter, u4-acyclic-merge-guard]
    implements: []
    consumes: []
  - task_id: u6-slice-overflow-gate
    description: A slice that exceeds the cap fails loudly naming it, with an explicit keep-whole override
    slice: null
    files:
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/pipeline.py
      - orchestrator/config.py
      - orchestrator/cli.py
      - tests/test_grouping_fixtures.py
      - tests/test_cli.py
    symbols: []
    depends_on: [u3-slice-aware-splitter]
    implements: []
    consumes: []
  - task_id: u7-size-hints
    description: Task maps may price prospective files by declared size class
    slice: null
    files:
      - orchestrator/grouping/plan_reader.py
      - orchestrator/grouping/estimator.py
      - orchestrator/config.py
      - tests/test_plan_reader.py
      - tests/test_estimator.py
    symbols: []
    depends_on: []
    implements: ["size_hints"]
    consumes: []
  - task_id: u8-trace-model-and-recorder
    description: A versioned trace model and a recorder capturing every stage partition and decision
    slice: grouping-trace
    files:
      - orchestrator/grouping/trace.py
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/pipeline.py
      - tests/test_grouping_trace.py
    symbols: []
    depends_on: [u5-scc-repair-and-resplit]
    implements: ["GroupingTrace"]
    consumes: []
  - task_id: u9-trace-artifact-and-cli
    description: Every group invocation writes grouping-trace.json, failures included, and no-spec renders from it
    slice: grouping-trace
    files:
      - orchestrator/grouping/trace.py
      - orchestrator/grouping/pipeline.py
      - orchestrator/cli.py
      - tests/test_grouping_trace.py
      - tests/test_cli.py
    symbols: []
    depends_on: [u8-trace-model-and-recorder, u10-named-groupings]
    implements: []
    consumes: ["GroupingTrace", "grouping-directory"]
  - task_id: u10-named-groupings
    description: Groupings are named directories selected at run time and snapshotted into the run
    slice: null
    files:
      - orchestrator/cli.py
      - orchestrator/execution/manifest.py
      - orchestrator/model.py
      - tests/test_cli.py
      - tests/test_e2e_stub.py
      - tests/test_grouper_pipeline.py
    symbols: []
    depends_on: []
    implements: ["grouping-directory"]
    consumes: []
  - task_id: u11-docs-and-skill-lockstep
    description: Task-map contract, grouping guide, plan skill, glossary and register state the new semantics
    slice: null
    files:
      - docs/orchestrator-task-map.md
      - docs/orchestrator-grouping.md
      - skills/orchestrator-plan/SKILL.md
      - docs/orchestrators_improvements.md
    symbols: []
    depends_on: []
    implements: []
    consumes: []
```
