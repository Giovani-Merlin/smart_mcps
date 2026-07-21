# Slice dissolution and group-DAG cycles — findings, and a study plan

Written 2026-07-21, from the session that produced
`docs/plans/2026-07-21-001-feat-orchestrator-observatory-plan.md`. That plan shipped and
groups cleanly; **this document is about what went wrong on the way there**, and is the
brief for a following session that improves the grouper. Nothing here proposes changing
the Observatory plan.

## TL;DR

Writing one greenfield plan took **five `group --dry-run` rounds**, three of which failed
with `dependency cycle across groups`. Each round costs ~5 minutes, almost all of it the
speccer LLM, which runs *after* every deterministic stage that could have failed.

The loop I fell into was **editing the plan document to steer the partitioner** — moving a
unit's `depends_on`, retagging `implements`/`consumes`, adding and removing shared stub
files — and re-running to see which shape the partitioner tolerated. That is tuning the
input to fit the tool, and it is the wrong loop. It also produced a lot of low-value diff
churn (`events.py` → `events.py *(new)*` and back) as I flip-flopped between two designs.

Two findings matter more than the cycles:

1. **In both runs that succeeded, every slice was dissolved anyway.** All three vertical
   slices were split across groups. The `slice:` labels — the whole vertical-slice
   feature — contributed nothing to the final grouping.
2. **For greenfield plans, a task's estimated work is almost entirely file *count*.**
   Prospective files contribute zero source bytes but a full `per_file_tool_allowance`, so
   `node_work ≈ 2000 × len(files)`. Listing `tsconfig.json` costs the same 2,000 tokens as
   listing a 400-line module. Group sizing on a greenfield plan is therefore driven by how
   granularly the planner enumerated files, not by how much work there is.

## What happened, round by round

Plan: 10 units, 3 slices of 2, ~50 files, nearly all prospective (greenfield front-end +
new backend package).

| #   | Plan shape                                                                                             | Result                             |
| --- | ------------------------------------------------------------------------------------------------------ | ---------------------------------- |
| A   | U3 (SPA hub) `depends_on` U2 (backend hub); U10 (verification) `depends_on` all three slices           | `cycle across groups [0,1,2,3]`    |
| B   | U3 made a root; `consumes` moved so only U3 (the API-client owner) consumes routes                     | `cycle across groups [0,1]`        |
| C   | U10 made fully isolated (no edges in or out); its tests distributed into the units owning each surface | **OK** — 4 groups, g1 = 86,577 tok |
| D   | Removed U2's stub router modules in favour of `pkgutil` auto-discovery in `app.py`                     | `cycle across groups [0,1,2]`      |
| E   | Reverted D                                                                                             | **OK** — 4 groups, g1 = 87,699 tok |

Final grouping (E), against the intended 3 vertical slices:

```
g1  u1, u2, u4, u6, u8, u10   87,699 tok  paired_plus  30 verification items
g2  u3, u5                    51,324 tok  self_verify
g3  u7                        18,990 tok  self_verify
g4  u9                        18,976 tok  self_verify
```

Slice `live-board` = {u4, u5} → split g1/g2. Slice `hitl` = {u6, u7} → split g1/g3. Slice
`drill-in` = {u8, u9} → split g1/g4. **Three for three dissolved.** What came out is four
horizontal layers: one backend group and three front-end groups.

## Why — the mechanism

All citations are current as of this commit; re-verify before acting on them.

### The expensive stage runs last, and the failing stage runs first

`run_grouping` (`orchestrator/grouping/pipeline.py:47`) is:

```
parse_task_map  →  build_task_graph  →  partition  →  build_group_dag  →  write_specs
   (no LLM)         (codegraph)        (deterministic)   (RAISES HERE)      (~5 min LLM)
```

`DefaultPartitionStrategy.partition` (`partition.py:166`) ends with
`build_group_dag(graph, partition)  # cycles must fail loudly`. So **every cycle failure
is fully deterministic and reachable in about a second**, and every *success* pays five
minutes for specs I did not need while iterating on structure. This is the single biggest
lever: there is no way today to ask "how would this plan group?" without paying for specs.

### Slice contraction is soft, and the softening is what breaks it

`DefaultPartitionStrategy.partition` (`partition.py:166-186`):

```python
roles = detect_hub_roles(graph, threshold=self.hub_threshold)
atoms = _slice_atoms(graph, roles)          # slice -> core members, 2+
if atoms:
    unit_graph, self_loops, unit_of = _contract_slices(graph, atoms)
    unit_partition = _hub_isolated_clustering(...)
    unit_partition = lift_independent(unit_graph, unit_partition)
    partition = {node: unit_partition[unit_of[node]] for node in graph.nodes}   # EXPAND
...
if self.budget_cap is not None:
    partition = split_over_budget(graph, partition, self.work_fn, self.budget_cap)
partition = merge_small_groups(graph, partition, self.work_fn, self.budget_cap)
```

Its own docstring is explicit: *"Softness comes after expansion: `split_over_budget` may
still break an oversized slice at its weakest internal edges and `merge_small_groups` may
combine small ones."*

`split_over_budget` (`partition.py:414`) runs on the **expanded** node set — the
supernodes are already gone — and does reverse-Kruskal: drop the weakest internal affinity
edges until the group falls apart. It has no knowledge that `u4` and `u5` were must-linked.
So the contract in `docs/orchestrator-task-map.md` — *"Slice-mates are **contracted into
one node** before Louvain — a hard must-link"* — is accurate only through Louvain. It is
**not** an invariant of the pipeline output. That gap is arguably a documentation bug on
its own.

### Why a split slice becomes a cycle

`depends_on` edges are directed and preserved. Once `u4` lands in g1 and `u5` in g2, the
edge `u4 → u5` becomes `g1 → g2`. Cycles appear when a *different* split puts an edge back
the other way — e.g. `u3 → u7` giving `g2 → g1`. The task DAG is still perfectly acyclic;
only the quotient graph cycles. `build_group_dag` then fails the whole run.

So the cycles were never a defect in the plans I wrote. They were the budget splitter
cutting slice supernodes in a direction that happened to invert an edge.

### Greenfield work estimation is file-count-driven

`node_work` (`estimator.py:36`):

```python
source_bytes = int(metadata.get("source_bytes", 0) or 0)
file_count = len(metadata.get("files", ()) or ()) + len(metadata.get("prospective_files", ()) or ())
tokens = source_bytes / config.bytes_per_token * config.slack_multiplier
return tokens + file_count * config.per_file_tool_allowance
```

On a greenfield plan `source_bytes == 0` for essentially every task, so
`node_work == 2000 × file_count` (`per_file_tool_allowance` default 2,000). U3 listed 16
files — several of them `tsconfig.json`-class boilerplate — and was priced at ~32k of
"work". The partitioner then split the plan to respect a cap derived from that number.

The effective cap is also lower than `token_budget` suggests
(`estimator.py:52`):

```python
head = (base_tokens + config.spec_tokens_allowance) * config.slack_multiplier
return max(config.token_budget - head, 0.0)
```

With `token_budget=100_000`, `spec_tokens_allowance=3_000`, `slack_multiplier=1.3`, the cap
on summed node work is `100_000 - (base_tokens + 3_000) × 1.3` — meaningfully below 100k
once the base context is counted. *(Worth verifying whether the per-group `est. tokens`
printed in the report includes that head; I did not confirm this and did not rely on it.)*

### `depends_on` carries no affinity — by design, but it has a consequence

`docs/orchestrator-task-map.md`: *"Directed dependency edges only — never affinity."* True
in the code. The consequence is that **a unit whose only relationships are dependencies has
no gravity at all**. My original U10 (verification) depended on all three slices and shared
files with nothing; Louvain parked it in the hub cluster, and the slices then pointed back
into that cluster. That is round A's cycle.

Corollary that shaped rounds C/D/E: **shared files are the only real grouping lever a plan
author has.** Round D removed U2's stub router files to decouple the backend units; that
removed their only anchor and immediately re-cycled. Round C/E kept the shared stubs, which
anchored u4/u6/u8 to u2 — and that is precisely *why* the whole backend collapsed into one
87.7k group. The thing that made the plan groupable is the same thing that destroyed the
verticals.

### Cross-stack verticals are structurally impossible right now

A `live-board` slice is meant to hold `events.py` (Python) and `GroupBoard.tsx` (TS). There
is no codegraph edge between them, they share no files, and `depends_on` contributes no
affinity. The only cross-stack signal is the `implements`/`consumes` route tag. So the
*only* force holding a cross-stack slice together is slice contraction — the one thing
`split_over_budget` is free to undo. When it does, nothing is left.

## The real question to study

I do not think this is settled, and I would not want the next session to start from my
guess. The plausible positions, with what each would cost:

**H1 — Slices should be a hard constraint.** Make `split_over_budget` and
`merge_small_groups` slice-aware: never cut inside a slice; if a slice alone exceeds
budget, fail loudly and tell the planner to split it. Pro: `slice:` finally means what the
contract says. Con: greenfield file-count inflation means slices will blow the budget
often, so this trades cycles for a different loud failure — possibly a better one, since
the error would name the actual problem.

**H2 — The estimator is the bug, not the partitioner.** If prospective files were priced
by something other than a flat 2,000 (declared size hint? a smaller greenfield rate?), the
Observatory plan might fit in fewer, larger groups and never trigger the splitter at all.
Cheapest to test: re-run the existing plan with `per_file_tool_allowance` at 500/1000 and
see whether slices survive. **Do this first — it is one config change and one dry run.**

**H3 — Vertical slices are the wrong abstraction for this pipeline.** Louvain optimizes
modularity over affinity; verticals are by definition low-affinity across the stack
boundary. Forcing them may be fighting the algorithm. The honest alternative is to accept
layer-shaped groups (which is what actually came out, and which is not obviously worse —
one worker owning a coherent backend API is defensible) and drop `slice:` from the
contract rather than keep a feature that measurably does nothing.

**H4 — The grouper becomes advisory.** The planning session already decides slices; let
the task map declare group membership directly and have the grouper validate (budget,
acyclicity, hub ordering) rather than cluster. Biggest change, but it removes a whole class
of "why did it group that way" debugging.

**H5 — Cycles should be repaired, not raised.** A group-DAG cycle is always caused by a
cut the partitioner itself chose. It could back off the offending cut and re-split
elsewhere instead of failing the run. This is orthogonal to H1–H4 and may be worth doing
regardless.

My weak lean is H2 first (cheapest, and file-count inflation is clearly *a* real bug),
then H1, with H5 as an independent robustness fix. But H3 deserves a genuine hearing —
"the feature did nothing in 2/2 successful runs" is evidence, and I would rather delete a
feature than keep one that only appears to work.

## Proposed harness — stop paying 5 minutes per question

### 1. A partition-only entry point (prerequisite for everything else)

Everything deterministic already precedes `write_specs`. Expose that:

- `smart-mcps-orchestrate group <plan> --no-spec` (or `--explain`), printing the partition,
  the group DAG, per-node `node_work`, the budget cap, detected hub roles, slice atoms, and
  which stage last modified the partition.
- Refactor `run_grouping` so the pre-speccer half is callable on its own — the speccer call
  at `pipeline.py:111` is the natural seam.

This turns a 5-minute question into a sub-second one and is what makes the rest cheap. It
is also directly useful to planning sessions: "will this plan group?" without burning
tokens.

### 2. Deterministic fixture plans (no LLM)

`tests/fixtures/grouping/*.md` — small plans, each isolating one behaviour, asserted
through the partition-only path:

- `greenfield-cross-stack.md` — the Observatory shape in miniature: 2 hubs, 3 two-task
  cross-stack slices, 1 isolated doc task. **Regression for this session.** Assert: no
  cycle, and record whether slices survive.
- `slice-over-budget.md` — one slice whose files alone exceed the cap. Asserts what we
  *decide* should happen (hard error under H1, silent split today).
- `hub-in-the-middle.md` — hub B depends on hub A, feature units depend on B. Round A's
  cycle, minimized.
- `no-affinity-sink.md` — a task depending on everything, sharing files with nothing. Round
  A's other cycle, minimized.
- `pure-backend.md` — same-stack plan where affinity is real; control case showing slices
  behave when the stack boundary is absent.
- Existing-code variants of the above, so greenfield vs brownfield estimation is compared
  on the same shapes.

Property assertions worth encoding regardless of which hypothesis wins:

- The group DAG is acyclic for every fixture (today: 3 of 5 shapes fail).
- No group's summed `node_work` exceeds the cap.
- Partitioning is byte-stable across runs (the memory notes a prior dry-run 3 vs real 2
  discrepancy — worth re-testing now that the mapper LLM is skipped for task-map plans).
- Under H1: no group contains a strict subset of a slice.

### 3. LLM-in-the-loop scenarios (few, explicit, opt-in)

Marked `@pytest.mark.llm` and excluded from the default run, since the suite is currently
zero-token:

- One end-to-end `group` on a fixture plan asserting the `task map: parsed from plan — mapper LLM skipped` flag and non-empty specs.
- One *without* a task map, exercising the mapper fallback on a greenfield plan — that path
  has the known "drops nonexistent-file mappings" behaviour and is not covered by the
  deterministic fixtures.

### 4. Suggested order for the next session

1. Build `--no-spec`. Everything else is gated on it.
2. Port the five fixture plans; record current behaviour as a baseline (expect failures —
   that is the point).
3. Test H2 by sweeping `per_file_tool_allowance`. One config knob, immediate signal.
4. Decide H1 vs H3 with the baseline table in hand, not from first principles.
5. If H1: make `split_over_budget` / `merge_small_groups` slice-aware, and fix
   `docs/orchestrator-task-map.md`, which currently overstates the must-link guarantee.

## Process note for planning sessions, until this is fixed

Do not iterate on a plan document to satisfy the partitioner. If `group` reports a cycle,
the useful response is to record the shape that caused it (here) and pick a plan structure
on its own merits — not to keep permuting `depends_on` and file lists until the tool stops
complaining. Three of my five rounds produced no improvement to the plan as a document.

Related: `docs/orchestrator-task-map.md` (the contract), `orchestrator/grouping/partition.py`
(hub roles, contraction, splitting), `orchestrator/grouping/estimator.py` (work and cap),
`docs/handoffs/2026-07-16-multiagent-orchestrator-phase-d-and-grouping-next.md` (the
grouping-quality items already queued).
