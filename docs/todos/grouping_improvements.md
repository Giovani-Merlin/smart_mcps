# Grouping improvements — what made a pre-mapped plan take 15 validation runs

Written 2026-08-26, from the session that produced
`docs/plans/2026-08-26-001-fix-observatory-and-run-resilience-plan.md`
(36 units, 28 groups). The plan itself is fine. Getting its task map from
"written" to "parses and partitions cleanly" took **15 invocations** of
`smart-mcps-orchestrate group … --no-spec`, each of which surfaced exactly one
problem.

This document separates what the planner did wrong from what the tooling
*forced*, because the two have different fixes. Every number here was measured
in that session, not estimated.

## The sequence

| run   | what it reported                                             | class          |
| ----- | ------------------------------------------------------------ | -------------- |
| 1     | `size_hints` names an existing file (`tests/test_preflight.py`) | map validation |
| 2     | degenerate partition — cycle repair left a group 678k over cap | graph shape    |
| 3     | degenerate partition again, now **1.7M** over (symbol trim made it worse) | graph shape |
| 4     | slice `jobs` over cap                                          | slice sizing   |
| 5     | slice `legibility` over cap                                    | slice sizing   |
| 6     | slice `legibility` over cap (again, after a split)             | slice sizing   |
| 7     | slice `model-selection` over cap                               | slice sizing   |
| 8     | slice `repro` over cap                                         | slice sizing   |
| 9     | slice `resilience` over cap                                    | slice sizing   |
| 10    | degenerate partition — `u1 → u2 → u3` with `u1`/`u3` slice-mates | slice/dep interaction |
| 11    | slice `resume-truth` over cap                                  | slice sizing   |
| 12    | slice `surprises` over cap                                     | slice sizing   |
| 13    | slice `views` over cap                                         | slice sizing   |
| 14    | slice `views` over cap (again, 5k short)                       | slice sizing   |
| 15    | clean — 28 groups                                              | —              |

Ten of the fifteen runs were the same class of error, reported one at a time.

## Causes

### C1 — every validator fails on the first problem, never reports the set

`_check_slice_overflow` (`orchestrator/grouping/pipeline.py:133`) iterates over
**all** slices and raises `GrouperError` inside the loop on the first one over
cap. It has the full picture in hand — `work_by_member` is computed per slice —
and discards it.

`orchestrator/grouping/plan_reader.py` is the same shape throughout: 20 distinct
`raise TaskMapError(...)` sites, no error accumulator anywhere in the file
(`grep` for an errors list finds only `from collections import defaultdict`).
A map with eight bad `size_hints` entries needs eight runs to find them.

This single property accounts for **runs 4–9 and 11–14** — nine of the fifteen.
All eight oversized slices were knowable on run 4.

> **Fix.** Accumulate and report. `_check_slice_overflow` should collect every
> offending slice and raise once with all of them. `_validate_shape` and the
> `depends_on` checks should do the same per phase (shape errors together, then
> reference errors together). Report *all* problems at the phase that found
> them, then stop.

### C2 — two different quantities are both called "work", and they differ by 2.5x

The slice-overflow message and the group listing report numbers on different
scales with the same name:

| surface                           | figure for `u27` (5 files, 139,314 bytes) |
| --------------------------------- | ----------------------------------------- |
| `grouping-trace.json` `node_work` | 55,277                                    |
| per-group listing ("node work")   | 55,277                                    |
| slice-overflow error ("work")     | **138,193**                               |

The slice check prices against `coder_slack_multiplier` (2.5); the group listing
does not. `55,277 x 2.5 = 138,193` exactly.

The practical consequence: a planner who reads the group listing to budget the
next slice is reading a number 2.5x too small, and will size the next slice
wrong. It also produced a wrong statement in this session — the 138,193 figure
was misread as the cost of `orchestrator/cli.py` alone, when `cli.py` is
75,040 bytes → 18,760 tokens raw → 26,388 node work. The user caught it.

> **Fix.** Name them differently in output — `node work` vs `coder work` — and
> print both in the slice error, with the multiplier stated. Or report the slice
> error in node-work units against a node-work cap, so one scale is used
> everywhere an operator sees a budget.

### C3 — there is no way to price a task map without running the partitioner

The work formula is `source_bytes / bytes_per_token * slack_multiplier +
file_count * per_file_tool_allowance`, then `* coder_slack_multiplier` for the
slice check, against `budget_cap = token_budget - head`. Every input is
knowable at plan time from `wc -c` and the config.

Nothing exposes it. `docs/orchestrator-task-map.md` documents `size_hints`
prices (500/2000/5000) but not the formula, the multipliers, or the cap. The
planner's only way to learn a slice is 5k over is to run the whole pipeline and
be told.

> **Fix.** A `group --price <plan>` (or `--explain-budget`) that parses the map
> and prints per-task node work, per-slice sums against the cap, and the
> resolved budget parameters — no graph build, no codegraph, sub-second. That
> one command replaces runs 4–14. Document the formula in the task-map contract
> alongside `size_hints`.

### C4 — `symbols` silently saturates the dependency graph, and the skill tells you to fill it in

Measured on this plan, same tasks, same files, symbols the only variable:

| map                          | tasks | **dependency edges** | affinity edges | cycle repairs | result                        |
| ---------------------------- | ----- | -------------------- | -------------- | ------------- | ----------------------------- |
| with `symbols` populated     | 34    | **127**              | 254            | 1             | degenerate, collapsed to 6 groups |
| with `symbols: []` everywhere | 36   | **24**               | 116            | 0             | clean, 28 groups              |

24 is exactly the declared `depends_on` count. So populating `symbols` added
**103 directed precedence edges** — a graph 5.3x denser than what was declared —
and that is what produced the 25-task SCC in runs 2 and 3.

`docs/orchestrator-grouping.md:461` records this failure mode as *"Derived
precedence saturated the graph — ✅ FIXED 2026-07-29"* via
`_drop_inferred_cycles`, which withdraws inferred precedence until the **task**
graph is a DAG. That fix held: the task graph was acyclic. The cycle in runs 2
and 3 was in the **group** DAG after Louvain — `repairs[].cyclic_groups` holds
group ids, not task ids. Task-level acyclicity does not imply group-level
acyclicity, and when `repair_cycles` merges the cyclic SCC the result blew the
cap.

Meanwhile `skills/orchestrator-plan/SKILL.md:37` instructs the planner to
*"Record **exact existing symbols and file paths**"*, and the map contract says
symbols must exist in the index. Nothing anywhere warns that in a codebase with
a dense call graph, populating `symbols` can make the partition degenerate — nor
that dropping it is the remedy. The planner follows the instruction and gets a
worse partition for it.

> **Fix, in order of value.**
> 1. Extend `_drop_inferred_cycles`' guarantee to the group DAG, or have
>    `repair_cycles` withdraw *inferred* edges (never declared ones) before
>    resorting to a merge that can exceed budget. Withdrawal is already free —
>    the weight is banked in affinity.
> 2. When a degenerate partition is reported, say how many of the offending
>    edges were **inferred vs declared**, and name their provenance
>    (`edge-provenance.json` already records it but the error does not surface
>    it). "103 of 127 dependency edges are inferred from `symbols`" is the
>    sentence that would have ended runs 2 and 3 immediately.
> 3. Update `SKILL.md` and the task-map contract: `symbols` is optional, it
>    contributes derived precedence, and on a dense codebase omitting it may
>    give a better partition. State the trade-off instead of an unconditional
>    "record exact symbols".

### C5 — a dependency path that leaves and re-enters a slice is reported as a generic cycle

Run 10 failed with a degenerate partition whose evidence was:

```
u1-preflight-classification -> u2-preflight-baseline
u2-preflight-baseline       -> u3-merge-gate-triage
```

That relation is acyclic. The cycle exists only because `u1` and `u3` were
slice-mates in `resilience` and slices contract to a single node before Louvain,
so the path out to `u2` and back closes a loop.

The error reported it as "the task dependency graph is saturated", which is the
wrong diagnosis and points at the wrong remedy (the suggested
`--allow-degenerate-partition` would have shipped a bad partition).

> **Fix.** Detect this specific shape and name it: *"slice `resilience` contracts
> `u1` and `u3`, but `u1 → u2 → u3` leaves the slice and returns — either bring
> `u2` into the slice or drop the slice label."* It is a cheap check on the
> contracted graph and it names the exact edit needed.

### C6 — the skill has the planner write everything before validating anything

`SKILL.md` Phase 5 writes the full plan — prose units *and* task map, slices
included — and Phase 7 runs the deterministic validation. Sizing is guided only
by *"a slice's summed content must plausibly fit one worker's token budget"*
(line 136): the `≤ 5 tasks` half is checkable by inspection, the token half is
unquantified, so "plausibly" is doing all the work and it guessed wrong eight
times.

Every slice edit then costs *two* synchronised edits (prose + map) because the
skill's own 1:1 rule requires it, which is why the fifteen runs each carried a
scripted two-sided patch.

> **Fix.** Move a sizing pass into Phase 2/5: with C3's `--price` command
> available, price the tasks *before* assigning slices. And state the real rule
> in place of "plausibly" — a slice's summed node work x 2.5 must be under
> `token_budget - head`.

## What was the planner's fault, not the tool's

Stated plainly, so the tool-side items above are not read as excuses:

- Eight slices were declared without pricing any of them, when every input to
  that arithmetic (`wc -c`, the multipliers, the cap) was available before a line
  of the map was written. That is one sizing pass, done badly, not fifteen
  tool failures.
- `symbols` was populated, then trimmed to "distinctive names only" (run 3),
  which made saturation **worse** (678k → 1.7M over cap) — a change made on a
  guess about the mechanism rather than on the trace, which already held the
  edge list that would have settled it.
- The 138,193 figure was quoted to the user as `cli.py`'s cost without checking
  the arithmetic. C2 makes that error easy; it does not make it correct.

## Priority

1. **C3** (`--price`) — removes nine of the fifteen runs on its own, and is the
   smallest change here.
2. **C1** (report all failures per phase) — removes most of the rest.
3. **C4.2** (say how many edges are inferred, and from what) — turns the two
   worst runs from guesswork into a one-line diagnosis.
4. **C2** (two names for two quantities) — cheap, and it is already responsible
   for one wrong statement to a user.
5. **C5**, **C4.1**, **C6** — real but larger, and mostly redundant once 1–3 land.
