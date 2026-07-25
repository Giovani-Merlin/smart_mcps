---
date: 2026-07-25
topic: multiagent-orchestrator
phase: run-hardening executed + merged to feat → next: fix the waste paths the execution exposed
plan: docs/plans/2026-07-22-001-feat-orchestrator-run-hardening-plan.md
branch: feat/multiagent-orchestrator
---

# Handoff — run-hardening is landed; the to-do it exposed

The run-hardening plan was **executed as its own orchestrator run** (`r20260722-221452`)
and is now **merged into `feat/multiagent-orchestrator`** (merge commit `d2d9b35`,
integration branch `orchestrator/run-r20260722-221452` @ `b203579`). Full suite
**340 passed**. Nothing pushed, no PR yet.

The run was the plan's own first customer — and because the orchestrator *running*
it was still pre-hardening code, it hit the exact failures the plan fixes (the R15
warning predicted this). That made the cost pathologically high (~10 resume cycles
over ~2.7 days, mostly rate-limit-reset waits; g5 alone burned 11 worker sessions
and still failed → resolved by hand). This handoff records what's fixed and the
concrete waste paths still open.

## Already landed on feat (don't re-fix)

- **INTERRUPTED state + resume-first warm re-entry** (g1/g4) — envelope failures
  (`SessionError`/usage-limit) are now non-terminal; `resume` re-enters warm
  instead of the old fork-first wipeout. This alone removes most of the pain.
- **Per-round timeout removed** (g5/U3) — no more 30-min kills of legitimate work.
- **Per-worktree venv** (g5/U6); **grouping gates + task-map strip** (g3);
  **partition `--no-spec` harness** (g6); **docs-register** (g2).
- **Serial by default** — `ExecutionConfig.concurrency = 1` (commit `0d19b17`).
  Groups stack conflict-free on the integration tip; a usage-limit hit costs at
  most one in-flight group. `--concurrency N` restores parallel throughput.
- **Commit-early coder prompt** (`orchestrator/prompts/coder.md`) — uncommitted
  work is lost when an interrupted group restarts, so the prompt now mandates
  incremental commits.

## To-do — the waste paths the run exposed

### 1. [HIGH] A merge conflict triggers a blind rewrite, which can never resolve it

On a `git merge` conflict, `IntegrationMerger.merge_group` (merge.py) does
`merge --abort` and raises `MergeConflict`; the review loop (review.py) routes the
group to the **rewrite loop** — re-running the coder from scratch. Re-doing the work
cannot resolve a merge conflict, so it burns `max_rewrites` (2) full coder+reviewer
generations and then fails the group. g5 died exactly this way (4 wasted sessions),
and the conflict was trivially resolvable by hand (one obsolete test + one keep-both).
**Fix options:** (a) attempt an automatic rebase / 3-way resolution before giving up;
(b) escalate the conflict to the operator via the existing HITL channel instead of
rewriting; (c) at minimum, do **not** spend rewrite budget on a conflict — surface a
distinct terminal state ("needs manual merge", naming the files) so the operator
resolves it directly. Files: `orchestrator/execution/merge.py`, `orchestrator/execution/review.py`.

### 2. [MED] Uncommitted progress is still only advisory-durable

The prompt now tells coders to commit often, but nothing enforces it. A checkpoint
safety-net would make progress crash-proof regardless: on interruption / round end,
auto-commit any uncommitted worktree changes as a WIP commit. Also verify the
INTERRUPTED re-entry path never cleans the worktree (preserve partial work — this
was the g6 spiral, and an operator-side mistake compounded it: see
`docs/handoffs` sibling note / memory `orchestrator-recovery-and-cleaning-lesson`).
Files: `orchestrator/execution/review.py` (re-entry), `orchestrator/execution/worktrees.py`.

### 3. [MED] Confirm envelope re-entry doesn't burn the generation/rewrite budget

g5 reached **generation 4** in the pre-hardening run purely from crash-respawns,
not real failures — inflating it toward `max_generations` (3) unfairly. With
INTERRUPTED now landing envelope failures as non-terminal, verify that warm
re-entry **continues the same generation** rather than incrementing it. If a
usage-limit crash still advances the counter, envelope pressure silently erodes the
rewrite budget of long groups. Files: `orchestrator/execution/scheduler.py`,
`orchestrator/execution/review.py`.

### 4. [LOW] Flag/config cleanups

- `--sequential` is `store_true` (can only turn serial *on*). With `concurrency=1`
  the default, parallel is `--concurrency N` and the `sequential` flag is largely
  redundant — either document the relationship or deprecate the flag.
- `[session] timeout_s` is now a **deprecated** key (warns) since the round timeout
  was removed — drop it from any local `.orchestrator/config.toml`.

## Operational notes for the next big run

- **Run on this hardened code** (now on feat) — the D9 wipeout + timeout kills are
  gone; recovery should rarely need hand-holding.
- **sonnet workers + serial default** stretch the rate-limit budget and bank each
  group as it completes. Set both in `.orchestrator/config.toml` (`[session] model`,
  `[execution] sequential` — or just rely on the new `concurrency=1` default).
- If a run still stalls on an envelope failure, the manual recovery recipe (flip
  only envelope-failed groups `failed→ready` in `state.json`, resume; never touch a
  real work failure or a crashed group's uncommitted worktree) is in memory
  `orchestrator-recovery-and-cleaning-lesson`.

## Reproduce / inspect

Run artifacts: `.orchestrator/runs/r20260722-221452/` (`state.json` shows all 6
groups completed; `state.json.bak-*` are the recovery snapshots). Integration branch
`orchestrator/run-r20260722-221452` (`b203579`) merged into feat at `d2d9b35`; g5's
merge (`b203579`) was resolved by hand — g2's superseded register annotations remain
reachable on that branch.
