---
title: Orchestrator Crash Recovery, Preflight, and Run Teardown
type: fix
date: 2026-08-19
origin: docs/brainstorms/2026-08-19-orchestrator-crash-recovery-requirements.md
---

# Orchestrator Crash Recovery, Preflight, and Run Teardown

## Objective

The orchestrator builds software well and cannot survive its own death. This plan
closes that, and the teardown gap behind it, against the origin brainstorm's
R1–R40.

Done means: an exception the orchestrator does not recognise classifies
`INTERRUPTED` and a plain `resume` re-enters it (R1, R2, R38); stranded work is
committed before any refresh touches it (R3, R4) and a SIGKILL mid-round is a
covered test case (R37); one mechanical, LLM-free `Preflight` gates every merge
into the integration branch on both the approved and the resolve path (R6–R10,
R39); the resolve path carries the approved path's conflict ladder (R11, R12); a
stalled run reports itself loudly rather than silently holding its neighbours
(R5); `retry` re-enters the four genuine work failures (R13–R16); reviewer
scratch is archived out of the worktree instead of blocking its own cleanup
(R17, R18); worktree paths are run-scoped with legacy adoption (R19, R20); the
run log and heartbeat stop lying about pauses, rounds, and transcripts
(R21–R24); liveness is derived from the orchestrator's own process rather than
worker pids (R25, R26); and `finish` pushes the integration branch, opens a
ready-for-review PR, and removes exactly the worktrees and branches that are
provably merged (R27–R35, R40).

**R41 is new, and did not come from the brainstorm.** It was raised during this
planning session from an observed symptom: merges going wrong on *serial* runs
after an earlier group had failed. It is specified in the Decisions below and
implemented by U3. Every other requirement traces to the origin doc.

**R36 is already done and is not a unit.** The P-B fix
(`orchestrator/config.py::_with_path_qualified_forms`),
`tests/test_permission_patterns_live.py`, the findings and requirements docs,
and the `CONTEXT.md` glossary entries were committed at plan time as `5c468c4`.
Any run of this plan launches from a base that already carries them. This was
necessary rather than optional: those changes lived uncommitted in the operator's
working tree, so a group worktree branched off the integration tip would never
have seen them, and a worker asked to "commit the P-B fix" would have re-derived
a different one.

## What we already know (resolved context)

Everything below was read from the source during planning. A worker should not
need to re-derive any of it.

### Where terminal failure is decided

`Scheduler._run_group` (`orchestrator/execution/scheduler.py:462`) is the single
classification point. Its current shape:

- `except RunAbort` — re-raised, stops the whole run.
- `except ReportError` — `FAILED` (a work failure despite its `SessionError` type).
- `except (SessionError, LlmProcessError, PermissionDenied, WorktreeRefreshConflict)`
  — `INTERRUPTED` via `self._classify`, and **returns immediately**, skipping
  `_resolve_failure`.
- `except Exception` (line 497) — `FAILED`. This is the bug: `WorktreeError`
  lands here, and `TERMINAL_STATES = {COMPLETED, FAILED, RESOLVED}` makes it
  unreachable by `resume`.

The four genuine work-failure routes all raise **`GroupFailure`**
(`orchestrator/execution/review.py:83`): rewrite cap (`_rewrite`), generation cap
(`_retire`), operator skip (`_escalate`, on `HumanAction.SKIP`). The fourth is
`ReportError`, from `orchestrator/execution/sessions.py:78`, already caught by
name.

**Import direction is load-bearing.** `review.py:54` does
`from orchestrator.execution.scheduler import Executor, GroupContext, GroupState, RunAbort`.
So `scheduler.py` cannot import `GroupFailure` from `review.py` — that cycles.
`GroupFailure` moves *to* `scheduler.py` and `review.py` imports it from there,
extending the import it already has.

### The refresh that raises

`_refresh_onto_tip` (`worktrees.py:153`) runs a plain `git merge`. On a real
content conflict it aborts and raises `WorktreeRefreshConflict` (which *is*
classified correctly today). On git refusing before the merge starts — the dirty
worktree case — it raises a bare `WorktreeError` (line 178). Only
`create_worktree`'s two re-entry branches call it, and neither commits first.

`commit_all(worktree, message)` (`worktrees.py:244`) already does `git add -A` +
`commit` and returns `False` on a clean or missing worktree. `is_dirty`
(line 239) is `git status --porcelain`, untracked included.

### Worktree paths

`worktree_path(repo_root, group_id, name)` (`worktrees.py:66`) returns
`.worktrees/<gid>-<slug(name)>` — no run id, while `group_branch` and
`integration_branch` are both run-scoped. Its three callers:

- `cli.py::_workspace_seams` → `workspace_for(group)` (line 1210)
- `cli.py::_resolve_deps` → `worktree_for(group)` (line 1243)
- `merge.py::IntegrationMerger.ensure` (line 56), which calls `create_worktree`
  with `group_id=f"run-{run_id}"`, `name="integration"` — so the integration
  worktree is `.worktrees/run-<run_id>-integration` today.

The infinity-skills ingest allowlist substring-matches the encoded cwd, which is
why every worktree nests under the repo root; `<repo>/.worktrees/<run_id>/…`
preserves that.

`_registered_branch(repo_root, path)` (`worktrees.py:98`) parses
`git worktree list --porcelain` and is the existing way to find what a directory
is checked out on — the raw material for R20's legacy adoption. Moving a
registered worktree needs `git worktree move <old> <new>`, not `mv`.

### The two merge paths

**Approved path.** `_GroupExecution._merge` (`review.py:659`) loops:
`set_state(MERGING)` → `deps.merge_group(group, workspace)` → on `MergeConflict`,
spread a `Surprise`, spend one of `execution.max_conflict_resolve_attempts` on
`_resolve_conflict_in_place` (a warm resume of `self.coder_sid` with
`render_conflict_resolve_prompt`), retry, then escalate `MERGE_CONFLICT`, then
`_rewrite`. A conflict never fails the group.

**Resolve path.** `cli.py::_resolve_deps` (line 1233) builds `ResolveDeps` with
`merge_for_resolve` (line 1252), which calls `merger.merge_group` **once** and
turns `MergeConflict` straight into `ResolveConflict` — which propagates out of
`Scheduler.run()` and ends the run. `ResolveDeps`
(`scheduler.py:69`) is three plain callables: `commit_stranded`,
`commits_ahead`, `merge_group`.

`Scheduler._resolve_autonomously` (line 550) is **synchronous** and is called
directly from `async _resolve_failure` (line 522). Adding an LLM warm-resume
under it would block the event loop for every other group.

`IntegrationMerger.merge_group` (`merge.py:73`) holds `self._lock` for the whole
merge and, after a clean merge, calls `remove_worktree(repo_root, worktree)`
without `force` inside a `try/except WorktreeError: pass` — which is exactly how
reviewer scratch silently blocks cleanup.

`_review_round` (`review.py:593`) returns `(None, None)` immediately for
`ReviewIntensity.SELF_VERIFY`, so no reviewer session exists and nothing
mechanical stands between such a group's last write and the integration branch.

### Holds and reporting

`_held_by(gid)` (`scheduler.py:614`) and `_blocked_by_failure()` (line 643) hold
every file-overlapping group against a `FAILED`-unsettled or `INTERRUPTED`
neighbour; `_overlap_report(gid)` (line 631) already returns
`[(other_gid, shared_files)]` and is what `_resolve_prompt` formats.
`GroupHold`/`HoldReason` (lines 114–131) are persisted on `GroupRunState.holds`
and printed verbatim by `_cmd_status`.

`_print_outcomes` (`cli.py:1296`) already special-cases `INTERRUPTED` and returns
2 — but prints only the state line and a `resume` hint, not the stall.

**The failure gate is much weaker than it looks.** `_held_by` holds a successor
only when `_files_overlap` is true, and that compares *declared* file sets —
which is exactly the quantity the grouper minimizes between groups. So across
groups of one partition the gate is close to a no-op: a group with no shared
declared file and no DAG edge is admitted normally while an earlier group sits
`FAILED` or `INTERRUPTED`. It then forks from `merger.tip()`, which by then either
carries a **hole** where the failed group's work should be, or carries
resolve-merged work nothing verified. This is independent of concurrency — it is
about what is on the tip — so it reproduces on a strictly serial run. The
cross-group call/impact coupling that would predict the breakage is deliberately
withdrawn as ordering by the grouper (`a reference is coupling, not ordering`),
so nothing else catches it either.

`Scheduler.run()`'s idle branch is where a halt has to be handled carefully: when
nothing is in flight it computes `blocked`, and raises `NoProgressError` unless
every blocked group is in `_blocked_by_failure()` — a set defined by DAG and
file-overlap reachability. A halt stops admission for groups outside that set, so
returning cleanly has to be explicit or the run would report a wedge it does not
have.

### Heartbeat and liveness

`RoundHeartbeat` (`heartbeat.py:68`) keeps `_phase`/`_phase_since` and an
independent `_overlay`/`_overlay_since` set by `push_phase`/`pop_phase` (wired to
the rate-limit gate at `review.py:221`). `snapshot()` (line 171) honours the
overlay; `_due_log_line()` (line 191) reads `self._phase` only — that asymmetry
is the whole of R22. `write_once` writes `heartbeat.json` under
`heartbeat_path(paths, group_id)` with atomic write-then-rename, from a daemon
thread on a 15s tick, and swallows every exception by contract.

`_run_generation` (`review.py:236`) logs `round N: started` and calls
`mark_round` **after** the `start_fork` call returns, guarded by
`if not is_reentry`. `_reenter` (line 410) already logs it *before* its resume,
which is the pattern R21 wants on the fork path. `_reenter` calls
`_refresh_transcript` nowhere; `_run_generation` calls it only after `start_fork`
(line 291).

`launch.py::run_liveness` (line 346) reads `state.json`'s `live_pids` and
`interrupted_at`; `RunLiveness.live` is `exists and bool(live_pids) and
interrupted_at is None`. `live_pids` is written by `Scheduler._record_pid`
(line 277) with **worker subprocess** pids. `check_not_live` (line 360) raises
`ConflictError` → 409. The module's own docstring already concedes that
`os.kill(pid, 0)` alone is an approximation because pids recycle.

### Run wiring

`_cmd_run` (`cli.py:834`) builds `merger = IntegrationMerger(repo_root, run_id)`
at line 994 with the default `launch_ref="HEAD"` — a commit, not a branch, which
is why R29 needs a branch resolved and persisted separately. `RunManifest`
(`model.py:106`) already carries the precedent for persisting run-launch config:
`escalation` and `usage_limit` are stored so `resume` restores them.
`RunPaths` (`manifest.py:112`) is the one place the run-directory layout is
spelled out; `group_dir(gid)` is `run_dir/groups/<gid>`. `atomic_write_text` and
`log_event(paths, text)` are the existing write/log primitives.

`OrchestratorConfig` (`config.py:371`) composes eight sub-models; `apply_overrides`
(`cli.py:349`) maps CLI flags onto them and `_add_execution_args` (line 294) is
where execution flags are registered. Subcommands are registered inline in
`main` (`cli.py:130`) and dispatched by an `if args.command == …` chain at the
bottom of it.

## Decisions

- **`GroupFailure` moves to `scheduler.py`; `review.py` imports it from there.**
  Classification must name the work-failure exception, and `review.py` already
  imports `scheduler.py`, so the reverse import would cycle. *Rejected:* a new
  `failures.py` module — a third module for one exception class, when the
  classifier's own module is where the concept belongs. *Rejected:* catching by
  name string, which is untypeable and silently breaks on rename.

- **The catch-all inverts to `INTERRUPTED`; only `GroupFailure` and `ReportError`
  reach terminal `FAILED`.** An exception the orchestrator does not recognise is
  by definition not a judgement about the work. *Rejected:* adding `WorktreeError`
  to the envelope tuple, which fixes g6's exact case and leaves the next
  unclassified error to repeat it.

- **The stall is reported, never worked around.** An `INTERRUPTED` group holds its
  file-overlapping neighbours with no `resolve_settled` escape, so the inversion
  trades a loud failure for a silent stall. It is paid for with a loud end-of-run
  report and a non-zero exit, not with a hold-release verb — releasing a hold
  would let a successor build over files the stalled group may still change,
  which is the exact collision `_held_by` exists to prevent.

- **`Preflight` is its own module, not a method on `IntegrationMerger`.** It runs
  on two callers that share no class (`_GroupExecution._merge` and
  `ResolveDeps.merge_group`), it needs a config object neither merger nor
  scheduler holds, and keeping it standalone is what makes it testable without a
  session, a merger, or a run. *Rejected:* folding it into `merge_group`, which
  would make the resolve path inherit it silently and leave no seam to assert the
  two checks independently.

- **Preflight checks the tree that will actually ship: refresh, check, merge — all
  under one lock hold, in the group's own worktree.** A green group branch plus a
  green integration branch does not imply a green merge; that is the semantic
  merge conflict, and it is why merge queues (Bors-NG, GitHub merge queue,
  Mergify, GitLab merge trains) test a speculative merge candidate rather than
  the topic branch. Our exposure is narrower than theirs but real in two places.
  With `concurrency > 1`, two groups fork from the same tip and the second ships
  a tree nobody tested — bounded, because `_excluded_by` already keeps
  file-overlapping groups from running concurrently, so the residual case is
  groups with disjoint declared files and genuine semantic coupling (the
  cross-file rename). And on the **resolve path this bites even at
  `concurrency = 1`**: `_resolve_autonomously` merges a failed group's branch with
  no refresh at all, so a branch forked many merges ago lands untested against
  what it lands on. Therefore `merge_group` acquires its lock, refreshes the group
  worktree onto the current integration tip, runs Preflight on *that* tree, and
  only then merges — a merge which is by then content-free on the integration
  side. *Rejected:* testing in the integration worktree post-merge, which is the
  textbook merge-queue shape but worse here on three counts — a failure would land
  where the group's warm coder session cannot reach it (the existing
  escalate/rewrite/in-place-resolve ladder operates on the group worktree), it
  needs a `git reset --hard` unwind path on the shared branch, and the
  integration worktree is never `provision_env`'d so `uv run pytest` would not
  run there at all. *Rejected:* speculative batching with bisection, which pays
  for itself at high merge volume with slow parallel CI and not at four to eight
  serialized branches.

- **The envelope side gets a bounded re-entry budget, released only by `retry`.**
  Step Functions, Temporal and Argo all pair a retriable/terminal classification
  with an explicit bound; our four `GroupFailure` routes are that bound for the
  work-judgement side, and before this the envelope side had no counter at all —
  nothing in the system could say a group had died the same way five times.
  `GroupRunState` gains `reentry_count`, and a group that exceeds
  `breaker.max_reentries` is **quarantined**: `resume` stops re-entering it
  automatically and reports it, and `retry` is what releases it. *Rejected:* a new
  `QUARANTINED` member of `GroupState` — quarantine is orthogonal to lifecycle
  (a quarantined group is still `INTERRUPTED`), and every `GroupState` addition
  has to be mirrored in the Observatory's own enums, which has drifted before. A
  boolean flag beside `resolve_settled` carries it with no enum surface.
  *Rejected:* a second override verb; `retry` already is the deliberate operator
  override and extending it to quarantine keeps one verb for one concept.

- **Liveness is an advisory `flock`, with the heartbeat as evidence rather than
  authority.** The kernel releases an `flock` on any process death, SIGKILL
  included, so it has no staleness window and no pid-recycling hazard by
  construction — where `os.kill(pid, 0)` proves only that *some* process holds
  that pid. It also makes `check_not_live` atomic: today it reads, decides, then
  launches, and two simultaneous launches can both pass that window. The
  heartbeat stays, because a lock answers "is a driver alive" and cannot answer
  "alive but wedged, or merely paused on a rate limit" — which is the question the
  run log and the Observatory actually ask. The lock fd **must** be opened
  `O_CLOEXEC` and kept out of `pass_fds`: this process spawns worker subprocesses
  continuously, and an inherited fd shares the lock's open file description.
  *Rejected:* a `psutil.Process.create_time()` start token instead of the lock —
  it closes the recycling hole but not the check-then-act race, and adds a
  dependency the project does not have. The 120s-over-15s freshness bound is kept
  for the wedged-versus-working question, where at eight missed ticks on a
  same-machine daemon thread it is conservative rather than under-tuned.

- **Preflight's check command is bounded by a configurable timeout, default 900s,
  and a timeout is a failure.** A hung `uv run pytest` on the approved path holds
  `IntegrationMerger._lock` and stalls every other group's merge — the same
  silent-stall class this work exists to close. *Rejected:* degrading a timeout to
  R8's "no check applied", which would let a genuinely wedged suite push work
  through unverified. *Rejected:* no timeout at all.

- **No LLM in Preflight, ever.** An LLM is invoked only when a *concrete
  identified* problem exists — a real git conflict, stranded uncommitted work.
  `Group.verification` items are `{id, description, required}` prose with no
  executable field; they stay the reviewer's contract and Preflight never reads
  them.

- **`_resolve_autonomously` becomes `async` and awaits its merge in a thread.**
  R11's in-place resolve is a blocking LLM resume; called from today's
  synchronous method it would freeze the event loop and every concurrent group
  with it. `ResolveDeps.merge_group` stays a plain sync callable invoked via
  `asyncio.to_thread`, so `scheduler.py` still imports no session machinery.

- **New behaviour gets new test modules.** `tests/test_review_loop.py` is 72 KB and
  `tests/test_cli.py` 56 KB; naming either in a group's files spends most of a
  worker's budget on reading. New modules (`test_preflight.py`, `test_retry.py`,
  `test_finish.py`, `test_review_scratch.py`, `test_worktrees.py`,
  `test_driver_liveness.py`, `test_failure_policy.py`) keep each group's read set
  proportional to its work.

- **`cli.py` is touched by four of the five groups, and that is accepted.** Both new
  verbs, the Preflight config wiring, the stall report, and the status liveness
  line all surface there, so those groups serialize on `_held_by`'s file-overlap
  hold. The alternative — one hub unit owning every `cli.py` edit — would leave
  every slice unverifiable end-to-end until that hub landed, which is a worse
  trade than a serialized merge order. Logic lives in `retry.py`, `finish.py`,
  and `preflight.py`; `cli.py` gains only registration and dispatch.

- **A failed group halts admission by default (R41).** `ExecutionConfig` gains
  `on_group_failure`, defaulting to `halt`: once any group has ended `FAILED` or
  `INTERRUPTED`, no further group is admitted. Both trigger it, because both leave
  the same thing behind — work that is not on the integration branch — and a
  successor forking from that tip is equally exposed either way. Stopping early is
  recoverable (`resume`, or `retry` then `resume`); building on a bad tip is not,
  which is why the safe outcome is the default rather than the opt-in. `overlap`
  keeps today's behaviour for unattended runs that should get as far as they can.
  In-flight groups are **never cancelled** to effect a halt — they run to their own
  outcome first; cancelling would strand exactly the work the rest of this plan
  exists to protect. *Rejected:* making `halt` the only policy, which removes the
  run-as-far-as-possible mode that a long unattended run wants. *Rejected:*
  halting on `FAILED` alone — under R1's inversion `INTERRUPTED` is now the common
  non-success outcome, so that reading would leave the original bug almost
  entirely unfixed.

- **`finish` gates on completeness, not on worktree state.** The CLAUDE.md rule
  *never clean a crashed group's uncommitted worktree progress* is about
  completeness: while the plan is unfinished, a crashed group's worktree may be
  the only copy of work a `retry` will build on. Once every group is terminal and
  every branch is an ancestor of the integration tip, that work is banked and the
  worktree is safe. `git branch -d` (never `-D`) is the second, independent guard
  behind the ancestry check, and a leftover diff is written to `leftover.patch`
  before any force-removal.

No decision here clears the ADR bar — each is either a bug fix with one sensible
shape or a mechanism the origin brainstorm already argued through.

## Units

### U1. failure-classification — an unrecognised exception is interrupted, not failed

- **Goal**: `_run_group` classifies terminal `FAILED` for exactly two exception
  types — `GroupFailure` (the rewrite-cap, generation-cap and operator-skip
  routes) and `ReportError` — and `INTERRUPTED` for everything else, including
  `WorktreeError`. `GroupFailure` lives in `scheduler.py`; `review.py` imports it
  from there. The inversion is bounded: `GroupRunState` counts re-entries and a
  group past `breaker.max_reentries` is quarantined rather than re-entered
  silently for ever. `RunState` gains a schema version so a `resume` across
  orchestrator versions fails loudly instead of coercing.
- **Files**: `orchestrator/execution/scheduler.py`,
  `orchestrator/execution/review.py`, `orchestrator/config.py`,
  `tests/test_scheduler.py`
- **Symbols**: `Scheduler`, `GroupState`, `TERMINAL_STATES`, `GroupRunState`,
  `RunState`, `BreakerConfig`, `GroupFailure`,
  `ReportError`, `WorktreeError`, `WorktreeRefreshConflict`, `RunAbort`
- **Depends-on**: —
- **Slice**: recovery
- **Implements / Consumes**: implements `GroupFailure`
- **Verification**:
  - A group executor raising `WorktreeError` leaves that group's `state.json`
    entry at `interrupted`, with `failure` reading `WorktreeError: <message>`.
  - A group executor raising a `RuntimeError` the orchestrator has never seen
    leaves that group `interrupted`, not `failed`. (R2, R38)
  - A group executor raising `GroupFailure` leaves that group `failed` and its
    `resolve_settled` flag set once resolve has run. (R1)
  - A group executor raising `ReportError` leaves that group `failed`. (R1)
  - `RunAbort` still propagates out of `_run_group` and is not reclassified.
  - Re-running the run with `resume=True` moves an `interrupted` group back to
    `ready` and re-enters it; a `failed` group is not re-entered. (R2)
  - `from orchestrator.execution.review import GroupFailure` still resolves, and
    `orchestrator.execution.review.GroupFailure is
    orchestrator.execution.scheduler.GroupFailure`.
  - Each re-entry of an `INTERRUPTED` group increments its persisted
    `reentry_count` by exactly one; a group that has never been re-entered reads
    zero. (Decisions)
  - A group whose `reentry_count` reaches `breaker.max_reentries` is marked
    quarantined and is **not** returned by the scheduler's admission pass on the
    next `resume`, while its `state` remains `interrupted`. (Decisions)
  - Quarantine is carried by a flag, not by a `GroupState` member: the set of
    `GroupState` values is unchanged, so no Observatory enum has to be updated.
    (Decisions)
  - `breaker.max_reentries` defaults to 3 and is settable from
    `.orchestrator/config.toml`.
  - `RunState` round-trips a `schema_version`; loading a `state.json` whose
    version the running orchestrator does not support raises a named error
    identifying both versions, rather than silently coercing or dropping fields.

### U2. worktree-lifecycle — commit stranded work, and scope worktrees to the run

- **Goal**: `create_worktree`'s re-entry paths commit any stranded work before
  refreshing; worktree paths carry the run id; a legacy path in flight is adopted
  rather than duplicated; and a SIGKILL mid-round becomes a covered live test.
- **Files**: `orchestrator/execution/worktrees.py`,
  `orchestrator/execution/merge.py`, `orchestrator/cli.py`,
  `tests/test_worktrees.py` *(new, medium)*, `tests/test_e2e_live.py`
- **Symbols**: `create_worktree`, `_refresh_onto_tip`, `worktree_path`,
  `_registered_branch`, `commit_all`, `is_dirty`, `remove_worktree`,
  `group_branch`, `integration_branch`, `IntegrationMerger`, `_workspace_seams`,
  `_resolve_deps`
- **Depends-on**: —
- **Slice**: recovery
- **Implements / Consumes**: implements `worktree_path`
- **Verification**:
  - Re-entering a group whose worktree has uncommitted and untracked changes
    leaves the worktree clean and adds one commit whose subject is
    `recover(<run_id>): <gid> work stranded by an interrupted run`; the
    previously-uncommitted file contents are present in that commit. (R3)
  - That subject never begins with `resolve(`, so `git log --grep` separates the
    two recovery paths. (R3)
  - `_refresh_onto_tip` still raises `WorktreeError` when git refuses a merge for
    a reason R3 did not clear, and the message names both the branch and the
    literal string `retry`. (R4)
  - `worktree_path(repo, run_id, gid, name)` returns a path under
    `<repo>/.worktrees/<run_id>/`, and the integration worktree resolves to
    `<repo>/.worktrees/<run_id>/integration`. (R19)
  - Every returned worktree path contains the repo directory name as a substring.
    (R19)
  - Given a registered worktree at the legacy `.worktrees/<gid>-<slug>` on the
    group's branch and no run-scoped path, re-entry leaves exactly one registered
    worktree for that branch, at the run-scoped path, with its uncommitted
    changes intact. (R20)
  - A live-tier test SIGKILLs the orchestrator mid-round so no cleanup runs,
    asserts `git status --porcelain` in the group worktree is non-empty, then
    resumes and asserts the group reaches `running`. (R37)

### U3. failure-policy-and-reporting — what the run does when a group fails

- **Goal**: two halves of one behaviour. **Policy**:
  `ExecutionConfig.on_group_failure` (`halt` | `overlap`, default `halt`) governs
  admission after a group ends unsuccessfully — under `halt` no new group is
  admitted once any group is `FAILED` or `INTERRUPTED`, in-flight groups finish,
  and `Scheduler.run()` returns without raising `NoProgressError`. **Reporting**: a
  run ending with any group `INTERRUPTED` exits non-zero and prints, per group,
  its failure string, the groups it holds and on which files, its branch, its
  re-entry count, and the command to act on it. No state changes.
- **Files**: `orchestrator/cli.py`, `orchestrator/execution/scheduler.py`,
  `orchestrator/config.py`, `tests/test_scheduler.py`,
  `tests/test_failure_policy.py` *(new, medium)*
- **Symbols**: `_print_outcomes`, `Scheduler`, `_admissible`, `_holds_on`,
  `_overlap_report`, `_held_by`, `_blocked_by_failure`, `NoProgressError`,
  `GroupHold`, `HoldReason`, `GroupRunState`, `RunState`, `GroupState`,
  `TERMINAL_STATES`, `ExecutionConfig`, `apply_overrides`, `_add_execution_args`,
  `group_branch`
- **Depends-on**: u1-failure-classification
- **Slice**: recovery
- **Implements / Consumes**: consumes `GroupFailure`
- **Verification**:
  - A finished run with one `interrupted` group exits non-zero. (R5)
  - Its output names that group's failure string verbatim. (R5)
  - Its output names each group held by it together with the shared file paths,
    for a state in which an overlap exists. (R5)
  - Its output names the group's branch `orchestrator/<run_id>-<gid>` and the
    `smart-mcps-orchestrate resume <run_id>` command. (R5)
  - Its output states each interrupted group's `reentry_count`, so repetition is
    visible without diffing run logs. (Decisions)
  - A quarantined group is reported as quarantined, and the command printed for
    it is `retry`, not `resume` — `resume` will not re-enter it. (Decisions)
  - `state.json` is byte-identical before and after the report is printed. (R5)
  - A run with no `interrupted` group prints no stall section.
  - With the default policy and a group that ends `FAILED`, no further group is
    admitted — including one sharing no declared file and having no DAG edge with
    it. That group's state stays `pending`. (R41)
  - The same holds for a group that ends `INTERRUPTED`. (R41)
  - Groups already in flight when the halt triggers run to their own terminal
    state and are not cancelled; their work reaches their branches. (R41)
  - A halted run returns from `Scheduler.run()` normally — it does not raise
    `NoProgressError` — even when the un-admitted groups are outside
    `_blocked_by_failure()`. (R41)
  - A halted run exits non-zero and its output names the group whose outcome
    triggered the halt, the groups it did not admit, and both ways forward:
    fix and `resume`, or re-run with `--on-failure overlap`. (R41)
  - Resuming a run whose only unsuccessful group is terminally `FAILED` halts
    again immediately, admitting nothing, and says that `retry` is what clears it.
    (R41)
  - With `on_group_failure = "overlap"`, a group sharing no declared file with a
    failed group is admitted and runs — today's behaviour, unchanged. (R41)
  - `--on-failure` overrides the config file value, and the resolved policy is
    recorded once in `logs/run.log`. (R41)

### U4. preflight-gate — one mechanical, LLM-free gate in front of every merge

- **Goal**: a `Preflight` that runs two checks — clean worktree, configured check
  command exits zero — on the tree that will actually ship, invoking no LLM.
  `IntegrationMerger.merge_group` takes its lock, refreshes the group worktree
  onto the current integration tip, runs Preflight on that refreshed tree, and
  only then merges. On the approved path a failure produces a `Surprise` and feeds
  the existing escalate-then-rewrite ladder.
- **Files**: `orchestrator/execution/preflight.py` *(new, medium)*,
  `orchestrator/config.py`, `orchestrator/execution/merge.py`,
  `orchestrator/execution/review.py`, `orchestrator/cli.py`,
  `tests/test_preflight.py` *(new, medium)*
- **Symbols**: `IntegrationMerger`, `merge_group`, `commits_ahead`,
  `_refresh_onto_tip`, `WorktreeRefreshConflict`, `is_dirty`, `Surprise`,
  `MergeConflict`, `OrchestratorConfig`, `ExecutionConfig`, `ReviewDeps`,
  `apply_overrides`, `log_event`, `_cmd_run`, `_add_execution_args`
- **Depends-on**: u2-worktree-lifecycle, u6-reviewer-scratch
- **Slice**: preflight
- **Implements / Consumes**: implements `Preflight`
- **Verification**:
  - Preflight against a worktree with uncommitted or untracked changes fails, and
    its reason names the dirty paths. (R6a)
  - Preflight evaluates cleanliness *after* scratch archival, so a worktree whose
    only dirt was the reviewer scratch directory passes. (R6a, R17)
  - Preflight against a clean worktree whose check command exits non-zero fails,
    and the check's combined output is written to a file whose path the failure
    carries. (R6b)
  - Preflight against a clean worktree whose check command exits zero passes.
  - Preflight makes zero LLM calls: with a session runner that raises on any
    call, both outcomes above still complete. (R6)
  - With no check command configured, in a checkout carrying `pyproject.toml` or
    `uv.lock`, the resolved command is `uv run pytest`; with only
    `package.json`, `npm test`; with neither, none. The resolved value appears
    once in `logs/run.log`. (R7)
  - With no check command configured and none detectable, Preflight runs the
    clean-tree check alone and `logs/run.log` states that no check command was
    applied. (R8)
  - A check command still running after `preflight.check_timeout_s` is killed and
    the result is a failure whose reason names the timeout. (Decisions)
  - An approved group whose Preflight fails does not merge; a `Surprise` of kind
    `other` carrying the Preflight reason is spread, and the group proceeds
    through escalation and then a rewrite, exactly as it does for a merge
    conflict. (R9)
  - A `self_verify` group's merge is gated by Preflight even though no reviewer
    session was ever created. (R6)
  - Preflight runs on the refreshed tree, not the pre-refresh one: given a group
    branched from an older tip and an integration branch that has moved since,
    the check command observes the integration branch's newer content in the
    worktree it runs in. (Decisions)
  - The refresh, the check, and the merge all happen within one acquisition of
    `IntegrationMerger`'s lock: a second `merge_group` call for another group
    cannot interleave between them. (Decisions)
  - A group whose check command fails *only* after the refresh — passing on its
    own branch in isolation, failing once the integration tip is merged in —
    does not reach the integration branch, and the integration tip is unchanged.
    This is the semantic-merge-conflict case. (Decisions)
  - A refresh that conflicts textually during a merge attempt surfaces as the
    existing `MergeConflict` path and feeds the conflict ladder, not as a
    Preflight failure. (Decisions)

### U5. resolve-ladder — the resolve path gets the approved path's conflict handling

- **Goal**: `merge_for_resolve` makes up to `max_conflict_resolve_attempts`
  in-place conflict-resolution attempts by warm-resuming the group's coder before
  raising `ResolveConflict`; a Preflight failure on this path leaves the group
  `FAILED` with its work committed and unmerged; a timed-out `group_resolve`
  escalation commits and stops without merging.
- **Files**: `orchestrator/cli.py`, `orchestrator/execution/scheduler.py`,
  `orchestrator/execution/review.py`, `tests/test_scheduler.py`
- **Symbols**: `ResolveDeps`, `ResolveConflict`, `_resolve_failure`,
  `_resolve_autonomously`, `_resolve_via_escalation`, `_resolve_deps`,
  `merge_for_resolve`, `MergeConflict`, `render_conflict_resolve_prompt`,
  `ExecutionConfig`, `SessionEntry`, `ManifestStore`, `EscalationKind`,
  `HumanAction`, `commit_all`
- **Depends-on**: u4-preflight-gate
- **Slice**: preflight
- **Implements / Consumes**: consumes `Preflight`
- **Verification**:
  - A resolve merge that conflicts triggers a warm resume of the group's recorded
    coder session with the conflict-resolve prompt, then a second merge attempt;
    when that succeeds the group reaches `resolved` and no `ResolveConflict` is
    raised. (R11)
  - The number of resume attempts never exceeds
    `execution.max_conflict_resolve_attempts`; with the attempts exhausted,
    `ResolveConflict` is raised. (R11)
  - A group with no reachable warm coder session raises `ResolveConflict` on the
    first conflict with zero resume attempts. (R11)
  - A group whose Preflight fails on the resolve path ends `failed`, the
    integration branch tip is unchanged, and the group's own branch carries the
    committed work. `logs/run.log` names the branch, the reason, the check-output
    path, and the `smart-mcps-orchestrate retry` command. (R10, R39)
  - A `group_resolve` escalation that times out leaves the group's worktree clean
    with its work committed on its branch, and the integration tip unchanged.
    (R12)
  - Resolving a group does not block other groups' progress: with two groups in
    flight, the second continues while the first is inside its resolve resume.
    (Decisions — `_resolve_autonomously` awaited off the event loop)

### U6. reviewer-scratch — the reviewer's litter stops blocking its own cleanup

- **Goal**: the reviewer prompt names one scratch directory inside the group
  worktree; that path is excluded via the worktree's own `.git/info/exclude`; and
  the review loop archives it into the run directory and removes it from the
  worktree at round end, with a configurable size cap and no silent drops.
- **Files**: `orchestrator/prompts/reviewer.md`,
  `orchestrator/execution/prompting.py`, `orchestrator/execution/review.py`,
  `orchestrator/execution/manifest.py`, `orchestrator/config.py`,
  `tests/test_review_scratch.py` *(new, medium)*
- **Symbols**: `render_reviewer_prompt`, `_GroupExecution`, `_review_round`,
  `RunPaths`, `group_dir`, `log_event`, `is_dirty`, `OrchestratorConfig`
- **Depends-on**: —
- **Slice**: preflight
- **Implements / Consumes**: implements `review-scratch`
- **Verification**:
  - The rendered reviewer prompt names exactly one scratch directory path, and
    that path is inside the group worktree. (R17)
  - After a round, the worktree's `.git/info/exclude` contains that path and the
    target repo's tracked `.gitignore` is unmodified. (R17)
  - After a round ends, the scratch directory no longer exists in the worktree
    and its files are present under
    `.orchestrator/runs/<run_id>/groups/<gid>/review-scratch/`. (R17)
  - `git status --porcelain` in the worktree is empty after archival, for a
    worktree whose only untracked content was the scratch directory. (R17)
  - With the cap set below the scratch total, files beyond the cap are absent
    from the archive, `skipped.txt` in the archive names each skipped file with
    its size, and `logs/run.log` records the truncation. (R18)
  - The cap defaults to 100 MB. (R18)

### U7. retry-command — a deliberate operator override for the four work failures

- **Goal**: `smart-mcps-orchestrate retry --repo <r> <run-id> <gid>` resets a
  terminally `FAILED` group to `pending`, clears its failure, keeps branch,
  worktree and warm session, refreshes the branch onto the integration tip first,
  and backs up `state.json` before writing. It is also what releases a quarantined
  group, and it refuses to mutate run state while a driver process holds the run's
  lock. Reaching terminal `FAILED` logs the recovery route.
- **Files**: `orchestrator/execution/retry.py` *(new, medium)*,
  `orchestrator/cli.py`, `orchestrator/execution/review.py`,
  `tests/test_retry.py` *(new, medium)*
- **Symbols**: `RunState`, `GroupRunState`, `GroupState`, `TERMINAL_STATES`,
  `RunPaths`, `atomic_write_text`, `log_event`, `_refresh_onto_tip`,
  `worktree_path`, `group_branch`, `integration_branch`, `ManifestStore`,
  `SessionEntry`, `main`, `_add_common_args`
- **Depends-on**: u1-failure-classification, u2-worktree-lifecycle,
  u11-run-driver-liveness
- **Slice**: operator-verbs
- **Implements / Consumes**: consumes `worktree_path`, `GroupFailure`,
  `run-driver-lock`
- **Verification**:
  - `retry <run-id> <gid>` on a `failed` group exits zero and leaves that group's
    `state.json` entry at `pending` with `failure` null. (R13)
  - The group's branch still exists, its worktree still exists, and its manifest
    session entries are unchanged with no new `retirement_reason`. (R13)
  - A following `resume` re-enters that group and it reaches `running`. (R13)
  - `retry` on a group that is not terminally `failed` exits non-zero and changes
    no state. (R13)
  - `retry` refreshes the group branch onto the integration tip: after a clean
    refresh the integration tip is an ancestor of the group branch. (R14)
  - When that refresh conflicts, `retry` exits non-zero, prints the conflicting
    file paths, leaves `state.json` byte-identical, and leaves the group branch at
    its pre-refresh commit. (R14)
  - A file named `state.json` appears under `.orchestrator/backups/` after a
    successful `retry`, and its content equals the pre-retry `state.json`. (R15)
  - When a group reaches terminal `failed`, `logs/run.log` contains its branch
    name, its worktree path, and the literal `retry` command line to run. (R16)
  - `retry` on a quarantined `interrupted` group clears the quarantine flag,
    resets `reentry_count` to zero, and a following `resume` re-enters it.
    (Decisions)
  - `retry` invoked while a driver process holds the run's lock exits non-zero,
    names the live run, and leaves `state.json` byte-identical. (Decisions)

### U8. finish-pr — push the integration branch and open a PR

- **Goal**: `smart-mcps-orchestrate finish --repo <r> <run-id>` pushes
  `orchestrator/run-<run_id>` and opens a ready-for-review PR against the branch the run was
  launched from, resolved at run start and persisted in the manifest; the run
  invokes it automatically only when every group is terminal-successful and every
  branch is merged, and prints the command otherwise. A missing or unusable `gh`
  never blocks.
- **Files**: `orchestrator/execution/finish.py` *(new, large)*,
  `orchestrator/cli.py`, `orchestrator/model.py`,
  `orchestrator/execution/manifest.py`, `tests/test_finish.py` *(new, large)*
- **Symbols**: `RunManifest`, `GroupManifestEntry`, `RunState`, `GroupRunState`,
  `GroupState`, `RunPaths`, `ManifestStore`, `log_event`, `integration_branch`,
  `group_branch`, `IntegrationMerger`, `_cmd_run`, `_print_outcomes`, `main`
- **Depends-on**: u2-worktree-lifecycle
- **Slice**: operator-verbs
- **Implements / Consumes**: implements `finish`; consumes `worktree_path`
- **Verification**:
  - `RunManifest` carries the branch name the run was launched from, written at
    run start; a run launched from a detached HEAD records it as null. (R29)
  - `finish` pushes `orchestrator/run-<run_id>` to `origin` and the remote ref
    exists at the integration tip afterwards. (R27, R29)
  - The opened PR's base is the recorded launch branch — not `HEAD` and not a
    commit sha. (R29)
  - ~~The opened PR is a draft.~~ **Superseded by operator decision
    (2026-08-20)**, after the run this plan shipped. R29's draft clause is
    withdrawn: `finish` passes no `--draft`, so the PR opens ready for review.
    `tests/test_finish.py` captures `gh pr create`'s argv and asserts `--draft`
    is absent — the fake `gh` succeeds either way, so only the argv pins it.
  - The PR body lists every group with its summary, its final state, its reviewer
    verdict where one exists, and its session count, plus an explicit list of any
    group left unmerged. (R29)
  - With a run launched from a detached HEAD, `finish` still pushes and skips the
    PR. (R29)
  - With `gh` absent, unauthenticated, or a non-GitHub remote, `finish` prints
    `integration branch orchestrator/run-<run_id> is ready at <sha>; could not
    open a PR (<reason>)`, continues into cleanup, and exits zero. (R30)
  - A run in which every group is `completed` or `resolved` and every group branch
    is an ancestor of the integration tip invokes `finish` itself. (R28)
  - A run with any other outcome prints the `finish` command, removes no worktree
    and deletes no branch. (R28)

### U9. finish-teardown — remove exactly what is provably merged

- **Goal**: `finish` archives remaining scratch and heartbeat files, writes any
  leftover diff to a patch, force-removes each group worktree guarded on branch
  ancestry, deletes each merged branch with `git branch -d`, and never touches
  the integration branch or worktree.
- **Files**: `orchestrator/execution/finish.py`,
  `orchestrator/execution/worktrees.py`, `tests/test_finish.py`
- **Symbols**: `remove_worktree`, `is_dirty`, `worktree_path`, `group_branch`,
  `integration_branch`, `RunPaths`, `group_dir`, `log_event`, `heartbeat_path`
- **Depends-on**: u8-finish-pr
- **Slice**: operator-verbs
- **Implements / Consumes**: consumes `finish`, `worktree_path`, `review-scratch`
- **Verification**:
  - After `finish`, each group worktree directory whose branch is an ancestor of
    the integration tip is gone and is absent from `git worktree list`. (R31)
  - A group whose branch is *not* an ancestor of the integration tip keeps its
    worktree and its branch, and is named in `finish`'s output. (R31, R40)
  - For a group worktree carrying uncommitted changes at teardown,
    `.orchestrator/runs/<run_id>/groups/<gid>/leftover.patch` exists, is
    non-empty, and applies cleanly to that group's branch tip. (R32)
  - Every deleted branch is deleted with `git branch -d`; a branch git itself
    considers unmerged survives and is reported. (R33)
  - Remaining review scratch and the run's heartbeat files exist under the run
    directory after `finish`, and are gone from the worktrees. (R34)
  - `orchestrator/run-<run_id>` still exists as a branch and its worktree
    directory still exists after `finish` completes. (R35)

### U10. heartbeat-and-log-truth — the run log stops lying about pauses and rounds

- **Goal**: a round's start is logged before the work it covers; the periodic
  heartbeat line reports an active overlay rather than the shadowed phase; paused
  time is reported separately from round elapsed time; and a warm resume refreshes
  its transcript path.
- **Files**: `orchestrator/execution/heartbeat.py`,
  `orchestrator/execution/review.py`, `tests/test_heartbeat.py`
- **Symbols**: `RoundHeartbeat`, `_due_log_line`, `mark_phase`, `mark_round`,
  `push_phase`, `pop_phase`, `snapshot`, `write_once`, `_humanize`,
  `_run_generation`, `_reenter`, `_refresh_transcript`, `_round_tag`,
  `SessionEntry`, `UsageLimitGate`, `PhaseOverlay`
- **Depends-on**: —
- **Slice**: observability
- **Implements / Consumes**: implements `run-driver-heartbeat`
- **Verification**:
  - In `_run_generation`, the `round N: started` line is appended to `run.log`
    before `start_fork` is invoked — asserted with a `start_fork` stub that reads
    the log at call time. (R21)
  - A healthy group's `round N: started` and the round's end line no longer carry
    the same timestamp to the second. (R21)
  - With an overlay pushed, the periodic log line names the overlay phase, and
    after `pop_phase` it names the phase underneath again. (R22)
  - The heartbeat snapshot exposes paused seconds separately from round elapsed
    seconds, and the periodic line renders both — e.g. `4h36m elapsed, 4h16m
    paused`. (R23)
  - Accumulated paused time survives more than one push/pop cycle in a round.
    (R23)
  - After a successful warm resume in `_reenter`, the session's manifest entry
    carries a non-null transcript path, for a session whose transcript appeared
    only after the resume. (R24)

### U11. run-driver-liveness — status reports whether a process is actually driving

- **Goal**: the driver process holds an exclusive advisory lock for its lifetime —
  the authority on whether a process is driving the run — and records its own pid
  and a freshening heartbeat as human-readable evidence. `status` reports both;
  `launch.py::run_liveness` and `check_not_live` are rebuilt on the lock instead
  of `state.live_pids`; and the Observatory's own detached-job liveness, which has
  the same pid-recycling hole with no heartbeat at all, is brought up to the same
  standard.
- **Files**: `orchestrator/execution/driver.py` *(new, medium)*,
  `orchestrator/execution/heartbeat.py`, `orchestrator/execution/manifest.py`,
  `orchestrator/cli.py`, `orchestrator/observatory/launch.py`,
  `tests/test_driver_liveness.py` *(new, medium)*,
  `tests/test_observatory_launch.py`
- **Symbols**: `RunPaths`, `RoundHeartbeat`, `heartbeat_path`,
  `atomic_write_text`, `run_liveness`, `RunLiveness`, `check_not_live`,
  `ConflictError`, `read_job`, `_cmd_status`, `_cmd_run`, `RunState`
- **Depends-on**: u10-heartbeat-and-log-truth
- **Slice**: observability
- **Implements / Consumes**: implements `run-driver-lock`; consumes
  `run-driver-heartbeat`
- **Verification**:
  - A running orchestrator writes a run-scoped driver record carrying its own
    process id — not a worker's — plus `started_at` and an `updated_at` that
    advances at least once within 30s. (R25)
  - A second process attempting to drive the same run is refused while the first
    holds the lock, and is admitted once the first exits. (R26)
  - A driver killed with SIGKILL — no cleanup, the record left on disk claiming
    it is alive — releases the lock, and the run reads as not live. (R25, R26)
  - A worker subprocess spawned by the driver does not inherit the lock: with a
    worker running, killing the driver still frees the lock immediately. This is
    the `O_CLOEXEC` requirement, asserted rather than assumed. (Decisions)
  - `status <run-id>` prints that a process is driving the run while the lock is
    held, and that none is once it is released. (R25)
  - `status <run-id>` prints that no process is driving the run when no driver
    record exists at all.
  - `status <run-id>` separately reports whether the run is *progressing*: with
    the lock held but the heartbeat's file mtime older than 120s, it says a driver
    is alive but its heartbeat is stale. The staleness decision reads the file's
    mtime, not the wall-clock string inside it. (R25)
  - `run_liveness` reports a run that crashed mid-worker — stale entries in
    `state.live_pids`, no held lock — as not live, and `check_not_live` permits
    relaunching it. (R26)
  - `run_liveness` reports a healthy run with an empty `state.live_pids` (between
    workers) as live, and `check_not_live` refuses a second launch with
    `ConflictError`. (R26)
  - The Observatory's detached-job liveness no longer reports a job as running
    purely because its recorded pid is reusable: a job whose process is gone reads
    as not running even when some other process now holds that pid. (Decisions)

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-failure-classification
    description: Classify terminal FAILED only for GroupFailure and ReportError; everything else becomes INTERRUPTED under a bounded re-entry budget
    slice: recovery
    files:
      - orchestrator/execution/scheduler.py
      - orchestrator/execution/review.py
      - orchestrator/config.py
      - tests/test_scheduler.py
    symbols:
      - Scheduler
      - GroupState
      - TERMINAL_STATES
      - GroupRunState
      - RunState
      - BreakerConfig
      - GroupFailure
      - ReportError
      - WorktreeError
      - WorktreeRefreshConflict
      - RunAbort
    depends_on: []
    implements: ["GroupFailure"]
    consumes: []
  - task_id: u2-worktree-lifecycle
    description: Commit stranded work before refreshing a re-entered worktree, and scope worktree paths to the run id with legacy adoption
    slice: recovery
    files:
      - orchestrator/execution/worktrees.py
      - orchestrator/execution/merge.py
      - orchestrator/cli.py
      - tests/test_worktrees.py
      - tests/test_e2e_live.py
    size_hints:
      tests/test_worktrees.py: medium
    symbols:
      - create_worktree
      - _refresh_onto_tip
      - worktree_path
      - _registered_branch
      - commit_all
      - is_dirty
      - remove_worktree
      - group_branch
      - integration_branch
      - IntegrationMerger
      - _workspace_seams
      - _resolve_deps
    depends_on: []
    implements: ["worktree_path"]
    consumes: []
  - task_id: u3-failure-policy-and-reporting
    description: Halt admission once any group ends failed or interrupted, and report every stalled group's failure, holds, branch and recovery command
    slice: recovery
    files:
      - orchestrator/cli.py
      - orchestrator/execution/scheduler.py
      - orchestrator/config.py
      - tests/test_scheduler.py
      - tests/test_failure_policy.py
    size_hints:
      tests/test_failure_policy.py: medium
    symbols:
      - _print_outcomes
      - Scheduler
      - _admissible
      - _holds_on
      - _overlap_report
      - _held_by
      - _blocked_by_failure
      - NoProgressError
      - GroupHold
      - HoldReason
      - GroupRunState
      - RunState
      - GroupState
      - TERMINAL_STATES
      - ExecutionConfig
      - apply_overrides
      - _add_execution_args
      - group_branch
    depends_on: [u1-failure-classification]
    implements: []
    consumes: ["GroupFailure"]
  - task_id: u4-preflight-gate
    description: One mechanical LLM-free Preflight on the refreshed group worktree, run under the merge lock so the tree checked is the tree that ships
    slice: preflight
    files:
      - orchestrator/execution/preflight.py
      - orchestrator/config.py
      - orchestrator/execution/merge.py
      - orchestrator/execution/review.py
      - orchestrator/cli.py
      - tests/test_preflight.py
    size_hints:
      orchestrator/execution/preflight.py: medium
      tests/test_preflight.py: medium
    symbols:
      - IntegrationMerger
      - merge_group
      - commits_ahead
      - _refresh_onto_tip
      - WorktreeRefreshConflict
      - is_dirty
      - Surprise
      - MergeConflict
      - OrchestratorConfig
      - ExecutionConfig
      - ReviewDeps
      - apply_overrides
      - log_event
      - _cmd_run
      - _add_execution_args
    depends_on: [u2-worktree-lifecycle, u6-reviewer-scratch]
    implements: ["Preflight"]
    consumes: []
  - task_id: u5-resolve-ladder
    description: Give the resolve path the approved path's conflict ladder and the Preflight gate, and never merge on a timed-out escalation
    slice: preflight
    files:
      - orchestrator/cli.py
      - orchestrator/execution/scheduler.py
      - orchestrator/execution/review.py
      - tests/test_scheduler.py
    symbols:
      - ResolveDeps
      - ResolveConflict
      - _resolve_failure
      - _resolve_autonomously
      - _resolve_via_escalation
      - _resolve_deps
      - merge_for_resolve
      - MergeConflict
      - render_conflict_resolve_prompt
      - ExecutionConfig
      - SessionEntry
      - ManifestStore
      - EscalationKind
      - HumanAction
      - commit_all
    depends_on: [u4-preflight-gate]
    implements: []
    consumes: ["Preflight"]
  - task_id: u6-reviewer-scratch
    description: Name one reviewer scratch directory, exclude it per-worktree, and archive then remove it at round end under a size cap
    slice: preflight
    files:
      - orchestrator/prompts/reviewer.md
      - orchestrator/execution/prompting.py
      - orchestrator/execution/review.py
      - orchestrator/execution/manifest.py
      - orchestrator/config.py
      - tests/test_review_scratch.py
    size_hints:
      tests/test_review_scratch.py: medium
    symbols:
      - render_reviewer_prompt
      - _GroupExecution
      - _review_round
      - RunPaths
      - group_dir
      - log_event
      - is_dirty
      - OrchestratorConfig
    depends_on: []
    implements: ["review-scratch"]
    consumes: []
  - task_id: u7-retry-command
    description: A retry command that resets a terminally failed group to pending, refreshes its branch, and backs up run state first
    slice: operator-verbs
    files:
      - orchestrator/execution/retry.py
      - orchestrator/cli.py
      - orchestrator/execution/review.py
      - tests/test_retry.py
    size_hints:
      orchestrator/execution/retry.py: medium
      tests/test_retry.py: medium
    symbols:
      - RunState
      - GroupRunState
      - GroupState
      - TERMINAL_STATES
      - RunPaths
      - atomic_write_text
      - log_event
      - _refresh_onto_tip
      - worktree_path
      - group_branch
      - integration_branch
      - ManifestStore
      - SessionEntry
      - main
      - _add_common_args
    depends_on: [u1-failure-classification, u2-worktree-lifecycle, u11-run-driver-liveness]
    implements: []
    consumes: ["worktree_path", "GroupFailure", "run-driver-lock"]
  - task_id: u8-finish-pr
    description: A finish command that pushes the integration branch and opens a ready-for-review PR against the recorded launch branch
    slice: operator-verbs
    files:
      - orchestrator/execution/finish.py
      - orchestrator/cli.py
      - orchestrator/model.py
      - orchestrator/execution/manifest.py
      - tests/test_finish.py
    size_hints:
      orchestrator/execution/finish.py: large
      tests/test_finish.py: large
    symbols:
      - RunManifest
      - GroupManifestEntry
      - RunState
      - GroupRunState
      - GroupState
      - RunPaths
      - ManifestStore
      - log_event
      - integration_branch
      - group_branch
      - IntegrationMerger
      - _cmd_run
      - _print_outcomes
      - main
    depends_on: [u2-worktree-lifecycle]
    implements: ["finish"]
    consumes: ["worktree_path"]
  - task_id: u9-finish-teardown
    description: Archive scratch and heartbeats, patch out leftovers, then remove only worktrees and branches provably merged
    slice: operator-verbs
    files:
      - orchestrator/execution/finish.py
      - orchestrator/execution/worktrees.py
      - tests/test_finish.py
    symbols:
      - remove_worktree
      - is_dirty
      - worktree_path
      - group_branch
      - integration_branch
      - RunPaths
      - group_dir
      - log_event
      - heartbeat_path
    depends_on: [u8-finish-pr]
    implements: []
    consumes: ["finish", "worktree_path", "review-scratch"]
  - task_id: u10-heartbeat-and-log-truth
    description: Log a round's start before its work, honour the heartbeat overlay, report paused time separately, and refresh a resumed transcript
    slice: observability
    files:
      - orchestrator/execution/heartbeat.py
      - orchestrator/execution/review.py
      - tests/test_heartbeat.py
    symbols:
      - RoundHeartbeat
      - _due_log_line
      - mark_phase
      - mark_round
      - push_phase
      - pop_phase
      - snapshot
      - write_once
      - _humanize
      - _run_generation
      - _reenter
      - _refresh_transcript
      - _round_tag
      - SessionEntry
      - UsageLimitGate
      - PhaseOverlay
    depends_on: []
    implements: ["run-driver-heartbeat"]
    consumes: []
  - task_id: u11-run-driver-liveness
    description: Hold an advisory driver lock for the run's lifetime and rebuild status, run_liveness and the Observatory job launcher on it
    slice: observability
    files:
      - orchestrator/execution/driver.py
      - orchestrator/execution/heartbeat.py
      - orchestrator/execution/manifest.py
      - orchestrator/cli.py
      - orchestrator/observatory/launch.py
      - tests/test_driver_liveness.py
      - tests/test_observatory_launch.py
    size_hints:
      orchestrator/execution/driver.py: medium
      tests/test_driver_liveness.py: medium
    symbols:
      - RunPaths
      - RoundHeartbeat
      - heartbeat_path
      - atomic_write_text
      - run_liveness
      - RunLiveness
      - check_not_live
      - ConflictError
      - read_job
      - _cmd_status
      - _cmd_run
      - RunState
    depends_on: [u10-heartbeat-and-log-truth]
    implements: ["run-driver-lock"]
    consumes: ["run-driver-heartbeat"]
```
