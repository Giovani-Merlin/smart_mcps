---
date: 2026-08-19
topic: orchestrator-crash-recovery
---

# Orchestrator Crash Recovery and Run Teardown — Requirements

## Summary

The orchestrator builds software correctly — run `r20260812-202855` merged seven of
eight groups with zero regressions across ~11,000 inserted lines — but it cannot
survive its own death, and it never cleans up after itself. This work closes both
gaps. It narrows terminal failure to a closed set of four genuine work failures so
that a git housekeeping error can no longer masquerade as "the coder failed"; it
puts one mechanical, LLM-free **Preflight** in front of every integration merge so
nothing unverified reaches the integration branch; it gives the resolve path the
same conflict ladder the approved path already has; it adds `retry` for the
failures that remain terminal by design; and it adds `finish`, which pushes the
integration branch, opens a draft PR, and removes the worktrees and branches a
completed run leaves behind.

## Problem Frame

`docs/2026-08-18-orchestrator-crash-recovery-findings.md` records what one live run
against `drummAI-practice-app` exposed. Every claim below was verified against the
source during this brainstorm; three of the doc's own claims turned out to be stale
or incomplete and are corrected here.

**A crash deterministically fails the next run.** The orchestrator process died
mid-round on 2026-08-14 leaving g6's worktree dirty. On resume,
`_refresh_onto_tip` (`worktrees.py:153`) ran a plain `git merge`, which refuses a
dirty tree, and raised. Nothing ever commits stranded work on the re-entry path,
so the group could never be re-entered again.

**And it fails it terminally, for the wrong reason.** This is the finding the
original doc did not reach. `_refresh_onto_tip` raises a bare `WorktreeError`
(`worktrees.py:178`). The envelope-failure tuple at `scheduler.py:482` catches
`WorktreeRefreshConflict` — its *subclass* — but not `WorktreeError` itself. So a
git housekeeping problem fell through the `except Exception` catch-all at
`scheduler.py:497` and was classified `FAILED`, which `TERMINAL_STATES` makes
unreachable by `resume`. g6's coder never failed at anything.

**The resolve path merges raw where the approved path has a whole ladder.**
`_merge` (`review.py:659`) handles a conflict by warm-resuming the group's own
coder with `conflict_resolve.md`, retrying the merge, then escalating, then
rewriting — a merge conflict does not fail a group. But `merge_for_resolve`
(`cli.py:1252`) calls `merger.merge_group` once and turns any `MergeConflict`
straight into `ResolveConflict`, which ends the run. The same trivial
router-registration conflict that `_merge` would have resolved killed
`r20260812-202855`.

**And it merges work nothing has judged.** `_resolve_autonomously`
(`scheduler.py:550`) commits stranded work and merges it to integration with no
build, no test, no review. g6's committed work carried four real test failures —
an `apply_edits` calling a signature its author had not written yet, code caught
mid-thought, which is exactly what an interrupted coder leaves behind. Had the
merge succeeded, g5 and g7 would have been built on a broken base. `SELF_VERIFY`
groups make this worse: `_review_round` returns immediately with no reviewer
session at all (`review.py:625`), so for those groups nothing mechanical exists
between a coder's last write and the integration branch.

**Nothing hands off, and nothing cleans up.** The run ends at `_print_outcomes`.
The integration branch is never pushed and no PR is opened. Group branches
accumulate one per group per run forever. Group worktrees *are* removed after a
clean merge (`merge.py:102`) — but without `force`, and `remove_worktree` refuses
a dirty worktree, so the reviewer's own `.review-scratch/` litter silently blocks
its own cleanup. `.worktrees/g1-note-values-and-chart-model` was dirty months
after g1 completed with nothing in it but that scratch.

**Worktree paths omit the run id.** `worktree_path` (`worktrees.py:66`) is
`.worktrees/<gid>-<slug>` while branches are run-scoped, so a stale worktree from
an earlier run blocks a later run's same-named group — which is what re-running a
validation looks like.

**And a run that dies while paused looks alive.** `status` reported `g6: running`
for 40 hours after the process died. `launch.py` grew a `RunLiveness` guard in
`092bf3f`, but it derives liveness from `state.live_pids`, which holds *worker
subprocess* pids (`scheduler.py:279`), not the orchestrator's own. It is wrong in
both directions: a run that crashed mid-worker leaves stale pids and reads live —
so `check_not_live` refuses to relaunch a dead run — while a healthy run between
workers reads dead.

### Corrections to the findings document

- §6 lists the §1 fixes as uncommitted. P-A (`_with_fresh_session_id`,
  `_adopt_actual_session_id`) is committed in `092bf3f`. Only P-B
  (`config.py::_with_path_qualified_forms`) and
  `tests/test_permission_patterns_live.py` remain uncommitted.
- §5.2 is half-fixed. `RoundHeartbeat` has `push_phase`/`pop_phase` and
  `RateLimitGate.watch()` is wired at `review.py:221`, so `heartbeat.json` does
  show a pause. The remaining bug is narrower than described: `_due_log_line`
  (`heartbeat.py:196`) reads `self._phase` and ignores `self._overlay`, so only
  the run-log line still lies.
- §5.1's fix is partially applied. The `coder launching, forking base session`
  line was moved before the fork; `round N: started` (`review.py:300`) was not.

## Key Decisions

- **FAILED becomes a closed set; unknown classifies INTERRUPTED.** Only four
  routes reach terminal failure: rewrite cap exhausted (`review.py:740`),
  generation cap exhausted (`review.py:778`), operator skip (`review.py:918`), and
  `ReportError` (`scheduler.py:481`). Every other exception — including any
  `WorktreeError` — becomes INTERRUPTED. Rationale: an exception the orchestrator
  does not recognise is by definition not a judgement about the work. *Rejected:*
  widening the envelope tuple to include `WorktreeError` only, which fixes g6's
  exact case and leaves the next unclassified envelope error to repeat the bug.
  *Rejected:* adding a consecutive-re-entry bound, because the loop risk it guards
  is not real — `run()` only flips non-terminal states back to READY when
  `self._resume` is set (`scheduler.py:306`), a fresh process, so the inversion
  yields one re-attempt per operator-invoked `resume`, never a hot loop.

- **The cost of the inversion is a silent stall, so it is paid with loud
  reporting.** An INTERRUPTED group holds every file-overlapping group via
  `_held_by` (`scheduler.py:625`) with no `resolve_settled` escape. Under the
  inversion a permanently-broken group stops failing loudly and starts stalling
  its neighbours instead. The run therefore reports the stall at the end rather
  than changing state. *Rejected:* a verb to release a hold, which would let a
  successor build over files a stalled group may still change — the exact
  collision `_held_by` exists to prevent. *Rejected:* escalating in-flight, which
  would fire on every ordinary crash-and-resume.

- **One mechanical Preflight in front of every merge, and no LLM in it.** Two
  checks: the worktree is clean, and a configured check command exits zero in it.
  It runs before the approved merge and before the resolve merge alike, so nothing
  reaches the integration branch unverified and `self_verify` groups stop being
  ungated. An LLM is invoked only when a *concrete identified* problem exists — a
  real git conflict, stranded uncommitted work — never as a general "check if this
  is good" pass. *Rejected:* gating on `Group.verification`. Those items are
  `{id, description, required}` (`model.py`) — prose with no executable field,
  interpretable only by the reviewer LLM, which contradicts the no-LLM-gate rule.
  They stay the reviewer's contract, untouched. *Rejected:* a fresh reviewer
  session on the branch, which spends a full LLM session on a group that already
  failed.

- **The resolve path gets the approved path's conflict ladder.** The asymmetry
  between `_merge` and `merge_for_resolve` has no design justification; it is an
  omission. *Rejected:* giving resolve the ladder without the Preflight gate,
  which fixes the run-killing conflict and keeps the danger that poisons
  successors.

- **`retry` is a deliberate operator override, not the crash-recovery path.** The
  inversion means INTERRUPTED groups re-enter automatically on `resume`, so
  `retry` narrows to the four genuine work failures, where automatic re-entry
  would be wrong by design. *Rejected:* documenting the manual `state.json` edit,
  which keeps hand-editing run state as the supported recovery route.

- **`finish` refuses to touch anything not provably merged, and runs only once the
  plan is complete.** This repo's CLAUDE.md carries the lesson *never clean a
  crashed group's uncommitted worktree progress*. The rule is about completeness,
  not about worktrees: while the plan is unfinished, a crashed group's worktree
  may still be the only copy of work a retry will build on. Once every group has
  reached a terminal state and every branch has merged — however that happened,
  including via retries and re-entries — that work is banked on the integration
  branch and the worktree is safe to remove. `finish` gates on exactly that, and
  on any other outcome prints its own command and stops.

- **Cleanup keeps the integration worktree.** It is where a human inspects the
  result. Only merged group worktrees and merged group branches are removed.

## Requirements

### Classification and recovery

- R1. `_run_group` classifies terminal `FAILED` only for the four named work-failure
  routes (rewrite cap, generation cap, operator skip, `ReportError`). The
  `except Exception` catch-all at `scheduler.py:497` classifies `INTERRUPTED`
  instead, recording the exception type and message as the failure string.
- R2. `WorktreeError` and every other unrecognised exception therefore classify
  `INTERRUPTED` and are re-entered by a plain `resume`. The existing named
  envelope types keep their current behaviour.
- R3. `create_worktree`'s re-entry path commits any stranded work *before* calling
  `_refresh_onto_tip`, using the commit subject
  `recover(<run_id>): <gid> work stranded by an interrupted run` — deliberately
  distinct from `resolve(...)` so the two paths stay distinguishable in `git log`.
- R4. The dirty-tree raise at `worktrees.py:178` stays in place as the backstop for
  any path R3 does not cover, and its message names the branch and the `retry`
  command.
- R5. When a run ends with groups still `INTERRUPTED`, it exits non-zero and prints,
  for each one: its failure string, the groups it is holding and on which files,
  its branch name, and the command to act on it. No state is changed.

### Preflight

- R6. A `Preflight` runs immediately before every merge into the integration branch,
  on the approved path and the resolve path alike. It performs exactly two checks
  and invokes no LLM: (a) the group's worktree has no uncommitted or untracked
  changes, evaluated after scratch archival (R17); (b) the repo's configured check
  command exits zero when run in that worktree.
- R7. A new config field carries the check command. When it is unset, the
  orchestrator detects a default once at run start — `uv run pytest` for a checkout
  with `pyproject.toml` or `uv.lock`, `npm test` for one with `package.json`, none
  otherwise — and records the resolved value in the run log.
- R8. When no check command is configured and none is detected, Preflight runs the
  clean-tree check alone and the run log states that no check command was applied.
- R9. A Preflight failure on the approved path produces a `Surprise` and feeds the
  existing ladder — escalate, then rewrite — exactly as a merge conflict does today.
- R10. A Preflight failure on the resolve path leaves the group `FAILED` with its work
  committed on its own branch and **not** merged, and logs the branch name, the
  reason, the path to the check output, and the `retry` command.
- R11. `merge_for_resolve` gains the approved path's conflict handling: on
  `MergeConflict` it makes up to `max_conflict_resolve_attempts` in-place resolve
  attempts by warm-resuming the group's coder session, retrying the merge after
  each, before raising `ResolveConflict`. A group with no reachable warm session
  skips straight to raising.
- R12. A `group_resolve` escalation that times out commits the stranded work and stops.
  It never merges, whatever Preflight would have said — an unanswered escalation is
  not consent.

### Retry

- R13. A `retry` command — `smart-mcps-orchestrate retry --repo <r> <run-id> <gid>` —
  resets a terminally `FAILED` group to `pending`, clears its `failure`, and keeps
  its branch, its worktree, and its warm session so the next `resume` re-enters it
  normally.
- R14. `retry` first refreshes the group's branch onto the current integration tip. On
  a clean refresh it proceeds; on a conflict it changes no state, reports the
  conflicting files, and stops, leaving the operator to resolve and re-run it.
- R15. `retry` backs up `state.json` to `.orchestrator/backups/` before writing.
- R16. When a group reaches terminal `FAILED`, the run log records the recovery route:
  its branch name, its worktree path, and the `retry` command.

### Reviewer scratch

- R17. The reviewer prompt names one scratch directory inside the group worktree. That
  path is added to the worktree's own `.git/info/exclude` — never to the target
  repo's tracked `.gitignore` — and the review loop archives it to
  `.orchestrator/runs/<run_id>/groups/<gid>/review-scratch/` and removes it from
  the worktree when the round ends.
- R18. Archival is capped at a configurable maximum per group, defaulting to 100 MB.
  Files beyond the cap are not archived; the archive carries a `skipped.txt`
  naming each one with its size, and a run-log line records the truncation.
  Nothing is dropped silently.

### Worktree layout

- R19. `worktree_path` becomes `<repo>/.worktrees/<run_id>/<gid>-<slug(name)>`, and the
  integration worktree becomes `<repo>/.worktrees/<run_id>/integration`. Both keep
  the repo directory name as a path substring, which the infinity-skills ingest
  allowlist requires.
- R20. On re-entry, when the run-scoped path does not exist but a legacy
  `.worktrees/<gid>-<slug>` directory is registered on the same branch, the
  orchestrator adopts it by moving it to the new path rather than creating a
  second worktree — so an in-flight run's uncommitted work is not stranded by the
  layout change.

### Reporting and liveness

- R21. `round N: started` is logged before the `start_fork` call in `_run_generation`,
  so a round's logged start precedes the work it covers and a healthy group stops
  producing the same-second start/end signature that reads as an empty-merge P0.
- R22. `RoundHeartbeat._due_log_line` honours `self._overlay` when one is set, so the
  periodic run-log line reports the rate-limit pause instead of the phase the
  group was in when the pause began.
- R23. The heartbeat reports paused time separately from round elapsed time — e.g.
  `4h36m elapsed, 4h16m paused` — so neither a human nor a breaker reads a pause
  as a catastrophically slow round.
- R24. `_refresh_transcript` runs after a successful warm resume in `_reenter`, so a
  session that died before its transcript existed stops reporting
  `transcript_path: NO` for the rest of the run.
- R25. The orchestrator records its own process id and a run-scoped heartbeat whose
  mtime advances while it is alive. `status` reports whether a process is actually
  driving the run, derived from that pid and that mtime.
- R26. `launch.py::run_liveness` is rebuilt on R25's signal instead of
  `state.live_pids`, so `check_not_live` no longer refuses to relaunch a run that
  crashed mid-worker, and no longer reports a healthy run as dead between workers.

### Finish: PR and teardown

- R27. A `finish` command — `smart-mcps-orchestrate finish --repo <r> <run-id>` —
  pushes the integration branch, opens a draft pull request, and cleans up.
- R28. The run invokes `finish` automatically only when every group has reached
  `completed` or `resolved` and every group branch is an ancestor of the
  integration tip. On any other outcome it prints the `finish` command and exits
  without touching worktrees or branches.
- R29. `finish` pushes `orchestrator/run-<run_id>` to `origin` and opens a **draft**
  PR whose base is the *branch* the run was launched from, resolved at run start and
  recorded in the manifest — `launch_ref` defaults to `HEAD`, which is a commit and
  not a valid PR base. When the run was launched from a detached HEAD with no
  branch, the push still happens and the PR is skipped per R30. The body is
  generated from the run:
  each group with its summary, its final state, its reviewer verdict where one
  exists, its session count, and an explicit list of any group left unmerged.
- R30. When `gh` is absent, unauthenticated, or the remote is not GitHub, `finish`
  prints `integration branch orchestrator/run-<run_id> is ready at <sha>; could
  not open a PR (<reason>)` and continues to cleanup. A missing PR never blocks
  cleanup.
- R31. `finish` removes each group worktree with `force=True`, guarded on the group's
  branch being an ancestor of the integration tip. A group failing that guard is
  skipped and named in the output.
- R32. Before force-removing a worktree, `finish` writes any remaining uncommitted diff
  to `.orchestrator/runs/<run_id>/groups/<gid>/leftover.patch`, so the
  never-lose-work guarantee survives the force.
- R33. `finish` deletes each merged group branch with `git branch -d` — never `-D` — so
  git's own merged-branch check is a second guard behind R31's.
- R34. `finish` archives any remaining review scratch and the run's heartbeat files
  into the run directory before removing worktrees.
- R35. `finish` keeps the integration branch and the integration worktree. Neither is
  ever removed.

### Housekeeping

- R36. The uncommitted P-B fix (`config.py::_with_path_qualified_forms`) and
  `tests/test_permission_patterns_live.py` are committed.

### Tests

- R37. A live-tier test kills the orchestrator mid-round with SIGKILL so no cleanup
  runs, asserts the group's worktree is dirty, then resumes and asserts the group
  reaches RUNNING. Nothing in the current suite kills a run mid-round; that is the
  coverage hole that let §2 ship.
- R38. A test asserts that a `WorktreeError` raised during re-entry classifies
  `INTERRUPTED`, not `FAILED`.
- R39. A test asserts that a group whose Preflight fails is left `FAILED` with its work
  committed on its branch and the integration branch unchanged.
- R40. A test asserts that `finish` refuses to remove a worktree whose branch is not an
  ancestor of the integration tip.

## Non-Goals

- Changing `Group.verification` or the reviewer's contract. Verification items stay
  prose the reviewer judges; Preflight is a separate, mechanical gate.
- Adding any LLM call to the merge path beyond the in-place conflict resolution that
  already exists. No general "is this good?" pass before a merge, ever.
- Releasing a stalled group's hold on its file-overlapping neighbours. The run
  reports the stall; it does not work around `_held_by`.
- Rebasing group branches, or any history rewriting. `_refresh_onto_tip` stays a
  plain merge for the reasons its docstring already gives.
- Retrying `INTERRUPTED` groups automatically within a single run process. Re-entry
  remains one attempt per operator-invoked `resume`.
- Recovering run `r20260812-202855`'s g6 as part of this work. Its recovery kit is in
  the findings doc; `retry` is what makes it reachable, and finishing its work is a
  separate task.
- Migrating existing stale worktrees from earlier runs. R20 covers the in-flight
  case; historical litter is removed by hand.

## Open Questions

None.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-08-19-orchestrator-crash-recovery-requirements.md`.
