# The KPI harness contract

The contract a harness unit implements so an `evaluate` or `optimize` unit
downstream of it can score against it deterministically. Read this before
writing the harness script that either recipe's `recipe_args.kpi` names, and
before writing a judge script the harness calls.

Writers: whichever unit's spec says "write the harness" (typically a plain
`code` unit landing `scripts/<name>_harness.py` or similar). Readers: the
`evaluate` executor ([`orchestrator/execution/evaluate_executor.py`](../orchestrator/execution/evaluate_executor.py))
and the `optimize` loop ([`orchestrator/execution/optimize_executor.py`](../orchestrator/execution/optimize_executor.py)),
both built on the pure decision function in
[`orchestrator/execution/kpi.py`](../orchestrator/execution/kpi.py). See
[`docs/orchestrator-task-map.md`](orchestrator-task-map.md) for the
`recipe_args` shape both recipes take.

## What the harness must do

1. Run the scoring pass named by the unit's `commands` (an `evaluate` unit)
   or by the group's own `commands` (an `optimize` unit).
2. Write a JSON file at the path named by `recipe_args.measurements`,
   containing at minimum the KPI key named by `recipe_args.kpi.key` as a
   top-level scalar, plus one top-level scalar per `recipe_args.kpi.guards[].key`.
   Only top-level scalar keys are read — a nested object or array under a
   declared key is ignored, not an error.
3. Exit non-zero on a harness crash (a real failure, not a bad score) so the
   executor records `crashed=True` and `decide()` returns `"crash"` rather
   than scoring a partial or missing measurements file.

## Held-out and frozen inputs

The instance set a harness scores against is **fixed for the life of one
optimize loop or evaluate unit** — every round scores the same inputs, so a
delta between rounds reflects the candidate, not sampling noise. Two rules
follow from that:

- **Held-out, not the codegraph index.** A judge or scoring harness reads its
  fixture inputs from a committed file or the run's shared data directory,
  content-hashed alongside the harness itself — never from a live codegraph
  query, which can drift mid-loop as the candidate's own commits land.
- **The harness paths are hashed once, at first use.** `recipe_args.kpi.harness_paths`
  names every file the hash covers. The evaluate child (or the optimize
  loop's settle step) computes the hash in the worktree at the first scoring
  run — the integration tip the group launched from — and every later
  scoring run in that same group compares against it. A mismatch is a Work
  Failure naming the path: the harness itself changed mid-loop, which
  invalidates every earlier delta, so the loop stops rather than silently
  rescaling.

## The KPI Contract fields

`KpiContract` (`orchestrator/execution/kpi.py`):

| Field           | Required | Semantics                                                                                                                                                                                       |
| --------------- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `key`           | yes      | The measurements JSON key scored as the KPI.                                                                                                                                                    |
| `direction`     | yes      | `"min"` or `"max"` — which way is better.                                                                                                                                                       |
| `min_effect`    | no       | The smallest delta (in `direction`'s sense) worth calling a real gain rather than noise.                                                                                                        |
| `guards`        | no       | `[{key, direction, max_regression}]` — metrics that must not regress past their own threshold, independent of the KPI's gain.                                                                   |
| `harness_paths` | yes      | Non-empty list of paths hashed at first use (above). For `optimize`, an `optimize` task's own `files` may not overlap these globs — checked at `group` time (`pipeline._check_optimize_files`). |
| `smoke`         | no       | An optional cheap command run before the real scoring pass, to catch a broken harness before spending a full evaluation.                                                                        |
| `good_enough`   | no       | An optional early-stop threshold; informational only — it never gates the unit or the loop on its own.                                                                                          |

## Keep-or-revert

`decide(delta, guard_deltas, floor, contract, crashed=)` returns one of
`keep | promising | inconclusive | discard | crash`. `delta` and every entry
of `guard_deltas` are signed so a positive value is always "better" —
`signed_delta(candidate, champion, direction)` does that flip once so the
harness itself never has to. `floor` is `noise_floor(...)`, the median
absolute deviation over the last five deltas (`0.0` with fewer than two
points — nothing to measure a spread from yet). A `promising` result runs a
confirmation evaluation before it is trusted as `keep`; a `discard` never
merges to the Champion. `optimize` renders the full history as an
append-only Attempt Ledger (`ledger.json`), and every prompt in the loop
carries a capped table of it.

## Judge-script patterns

A judge is a harness script, not a special case — a script that happens to
call an LLM to produce one of the measurements JSON's scalar keys, held to
the same hashing and fixed-instance rules above. The patterns below are
lifted, by name only, from `infinity-skills/src/infinity_skills/eval/summary_prompt_tuning.py`
— that module is the origin of this section, not a dependency:

- **Pairwise, position-swapped comparisons against the live baseline.** Score
  a candidate by asking the judge to compare it against the current
  Champion's output on the same instance, not by scoring it in isolation —
  and run the comparison twice with the two outputs swapped, since an LLM
  judge has a measurable position bias. A tie only if both orderings agree.
- **A rubric with a reason per field**, not a single number: the judge names
  which dimension moved and why, so a `discard` or `promising` verdict is
  legible in the ledger, not just a delta.
- **Disk-cached judge calls**, keyed on the instance and the two outputs
  being compared, so a re-run (a confirmation evaluation, a re-scored
  earlier candidate) never re-spends a judge call for output it has already
  seen.
- **A fixed instance set**, sized to keep the judge's own cost bounded per
  round — the same held-out set named above, not resampled between rounds.

## Not covered here

Writing the harness's own domain logic (what a "summary" or "render" harness
actually measures) is the harness unit's spec, not this document. This
document is the shape every harness must expose to the recipes that call
it — the KPI Contract, the hash, and the judge patterns — not the metric
itself.
