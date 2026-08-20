---
title: Why the grouper's token estimate under-predicts coder context by ~3x
type: findings
date: 2026-08-20
evidence: run r20260819-crashrec (4 groups, 9 sessions, all measured)
---

# Why the estimate under-predicts coder context ~3x

## The measurement

Estimates from `.orchestrator/groupings/crash-recovery/groups.json`; actuals from
`last_context_tokens` in the run manifest, cross-checked against the peak
per-turn `input+cache_read+cache_creation` in each session's transcript jsonl
(the two agree within rounding).

| grp | est     | coder actual | overshoot | reviewer actual | reviewer ratio |
| --- | ------- | ------------ | --------- | --------------- | -------------- |
| g1  | 118,613 | 386,938      | **3.26x** | 118,527         | 1.00x          |
| g2  | 93,591  | 146,397      | **1.56x** | 100,093         | 1.07x          |
| g3  | 117,999 | 384,678      | **3.26x** | 105,695         | 0.90x          |
| g4  | 84,557  | 323,864      | **3.83x** | 113,861         | 1.35x          |

## The finding: the estimator is a *read-cost* model, and only reviewers read once

**The estimate is not broken. It is accurate for what it models.** Reviewers land
at 0.90x-1.35x of estimate — a reviewer reads the group's material roughly once,
which is exactly what `estimate_group_tokens` computes (base head + spec +
source_bytes/4, times 1.3 slack, plus a flat per-file allowance).

Coders overshoot 1.56x-3.83x because their context is read-cost **plus**
iteration cost, and iteration dominates. Measured cost per assistant turn:

| session       | turns | context | ctx/turn |
| ------------- | ----- | ------- | -------- |
| g2 coder      | 146   | 146,397 | 1,003    |
| g3 coder      | 399   | 384,678 | 964      |
| g4 coder gen1 | 284   | 323,864 | 1,140    |
| g4 coder gen2 | 108   | 108,054 | 1,000    |

**coder peak context ~= 1,000 tokens x turns**, tight across every measured
session (964-1,140). Turn count is the whole story, and the estimator has no term
for it. g2 was not better estimated than g3 — it simply took 146 turns instead of
399\.

### A hypothesis the data refutes

Greenfield share looked like the driver (g4: 92% of insertions in new files, the
lowest estimate and the worst overshoot). **g1 kills it**: 0 new files, 1,048
insertions into existing files, and still 3.26x. New-file volume correlates with
turns but does not cause the overshoot — iteration does, whoever it is aimed at.

## The values that are actually wrong

1. **`slack_multiplier = 1.3` (`EstimatorConfig`)** — the single worst value. It
   is the only fudge factor standing in for all iteration, and it is ~2.5x too
   small for a coder. Measured coder need: ~3.3x (median), 3.8x worst. It is
   simultaneously *correct* for reviewers, which is why it cannot simply be
   raised: one knob is serving two consumers with very different behaviour.

2. **`token_budget == context_token_limit` (both 200,000 in `config.toml`)** —
   structurally guarantees retirement. Sizing a group to *fill* a 200k budget
   means its coder lands near 200k x 3.3 = ~660k, against a 200k breaker. g1, g3
   and g4 all exceeded it; g4 was actually retired mid-review-cycle and lost its
   warm context. Any group sized to the budget is a group the breaker will kill.
   Note the resolution runs the other way from the obvious one: the coupling was
   broken by making the budget mean *coder* tokens (so groups shrink), not by
   lifting the breaker to accommodate them — see the decisions below.

3. **`per_file_tool_allowance = 2,000` / `size_hint_large = 5,000`** — flat
   per-file costs that price *touching* a file, not *iterating* on it. Secondary
   to (1) and (2), but they are why authoring-heavy groups skew worst.

## What was decided and done (2026-08-20)

**Rejected: raising the breaker to fit the groups.** The first draft of this
document proposed lifting `context_token_limit` to ~650-700k so a budget-filling
group would stop being retired. That is the wrong trade. Past roughly 200k of
context the model degrades noticeably, so retiring g4's coder at 323k was the
breaker *protecting output quality*, not obstructing throughput. Raising it buys
more work done worse. The overshoot is an upstream sizing bug and belongs fixed
upstream.

**Done — `EstimatorConfig.coder_slack_multiplier = 2.5`.** The estimator now
scales its read-cost figure into a predicted coder peak. Applied to the group
estimate only; the reviewer figure needs no correction and gets none. Applied
consistently across `estimate_group_tokens`, `node_work` and
`partition_budget_cap` (and the recorded `BudgetArithmetic`, or the trace would
not explain the cap printed beside it) so partition-time sizing and the final
estimate agree.

Direction, since it is easy to get backwards: raising this makes groups
**smaller and more numerous**. Against a 200k budget, 2.5 leaves an effective
read-cost cap of ~80k per group. That is the intent — a group sized to fill 200k
of read cost costs its coder ~500k and gets retired mid-work, losing the warm
context that made it cheap. More groups is the price of every group finishing.

2.5 rather than the measured 3.26 median: the breaker sits at 250k with the
budget at 200k, so a modest under-correction still lands inside the guard rail,
and the multiplier is meant to be tuned from accumulated runs rather than set
once from a four-group sample.

**Done — `context_token_limit` 200k -> 250k.** Just above the sizing budget:
~25% headroom for a correctly sized group, while a group that blows well past it
is genuinely misbehaving rather than merely under-estimated.

**Done — `smart-mcps-orchestrate calibrate <run-id>`.** Both halves of the
comparison were already on disk (`estimated_tokens` per group in the run's
`groups.json`, `last_context_tokens` per session in `manifest.json`) and nothing
compared them, so the multiplier could only ever be set by hand-measuring. The
command reports per-group ratios and the multiplier they imply, and never edits
config: one run is a small sample.

One subtlety it has to get right, because getting it wrong inverts the advice:
the multiplier that explains a run's estimates is the one in effect when that run
was **grouped**, read from `grouping-trace.json`, not the one configured now. A
run grouped before the knob existed carries raw read-cost estimates; scaling its
3.26x by today's 2.5 would compound two eras and recommend 8.15.

**Not done: shrinking `token_budget`.** It would bring coders under the breaker
but fragments work into many small groups, against the standing preference for
few large vertical groups and warm-context reuse. The multiplier achieves the
same safety while leaving the budget legible as "what a coder may cost".

## Caveat on the sample

Four groups, one run, one repo, one model (sonnet), and g1's coder transcript was
absent (its actual came from the manifest, so turn count is unknown for it). The
~1,000/turn constant is consistent enough to act on, but F3 exists precisely so
the multiplier stops depending on this sample.
