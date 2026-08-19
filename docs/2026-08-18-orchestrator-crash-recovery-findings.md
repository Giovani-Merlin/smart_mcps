# Crash recovery is the gap: what run r20260812-202855 exposed

Written 2026-08-18, from the live validation against `drummAI-practice-app`. Two
P0s were found *and fixed* during the run (§1); three remain open and all three
are about the same thing — **what happens to a group the orchestrator itself
killed** (§2–§4). §5 collects a family of reporting bugs that mislead an operator
without breaking anything. §6 is the concrete state to pick up from.

Every claim here was observed on a real run, not reasoned from the source.

______________________________________________________________________

## 1. Fixed during the run (context for the rest)

| # | Bug | Fix | Live proof |
|---|-----|-----|-----------|
| P-A | `--session-id` retry after a usage limit always died `already in use` — the CLI spends the id even on a call that fails, so replaying argv verbatim could never work. Cost 3h42m of waiting then instant failure. | `_with_fresh_session_id` in `sessions.py`; `_adopt_actual_session_id` in `review.py` reconciles the plan-U7 pre-registered id. | Fired twice: g4 gen 2 (`79168846` ≠ `0cd63de8`) and g2 gen 1 (`319cd6f9` ≠ `78fcd1ae`). Manifest verified to carry the id that really ran, so the run stayed resumable. |
| P-B | `.venv/bin/python -m pytest` denied though `Bash(python *)` was allowed — a rule matches the command *as written*, and one program has many names. | `_with_path_qualified_forms()` in `config.py` pairs every `Bash(<cmd> …)` with a `Bash(*/<cmd> …)` twin. 36 rules → 71. | g2 completed and merged on the next resume. |

**The `*python *` trap.** The intuitive wildcard does not work, and the failure is
silent. Probed against the live CLI:

```
Bash(python *)            denied     <- the g2 failure
Bash(*python *)           denied     <- a bare leading * does NOT match
Bash(*/python *)          ALLOWED    <- the wildcard must align to a `/`
Bash(.venv/bin/python *)  ALLOWED    <- exact, one rule per path
Bash(*)                   ALLOWED    <- grants everything; deliberately unused
Read (control)            denied     <- proves the probe can still fail
```

Pinned in `tests/test_permission_patterns_live.py`. Two probe mechanics are
load-bearing and cost a wrong answer before being noticed: `--allowedTools` is
**variadic** (a trailing prompt is swallowed as another tool name — pass it on
stdin), and the probe must pass `--setting-sources ''` because `--allowedTools`
*adds* to the operator's settings rather than replacing them.

______________________________________________________________________

## 2. OPEN — a crash guarantees the next run fails for that group

### What happens

g6 was mid-round when the orchestrator process died (2026-08-14 23:22). It left
uncommitted files. On the next resume:

```
15:26:38  group g6: failed (WorktreeError: refreshing group g6's worktree onto
          9b1bbd7 failed: error: Your local changes to the following files would
          be overwritten by merge:
              app/routes/__init__.py
          Please commit your changes or stash them before you merge. Aborting)
```

### Root cause — an ordering problem, not a missing feature

`ensure_group_worktree` refreshes a re-entered branch onto the integration tip
before handing it back (`worktrees.py:128` and `:138`). `_refresh_onto_tip`
(`worktrees.py:153`) runs a plain `git merge`, which refuses to touch a dirty
tree, and the raise at `worktrees.py:178` is **deliberate** — its comment says
"the uncommitted changes are exactly what must survive untouched."

That choice is right. The bug is that nothing ever commits those changes, so the
group can never be re-entered again. Meanwhile `commit_stranded` — the exact
routine needed — already exists (`cli.py:1246`, wrapping `commit_all`) but only
runs *after* the group has already failed, from `_resolve_autonomously`
(`scheduler.py:557`). The machinery is present and sequenced backwards.

**Consequence: the crash of run N deterministically fails run N+1 for every group
that was mid-round.** The orchestrator does not clean up after its own death.

### Proposed fix

**Preferred — commit before refreshing.** In the re-entry path of
`ensure_group_worktree`, commit any stranded work *first*, then refresh:

```python
if path.exists() and _registered_branch(repo_root, path) == branch:
    _ensure_worktree_config_extension(path)
    commit_all(path, f"recover({run_id}): {group_id} work stranded by an interrupted run")
    _refresh_onto_tip(path, group_id=group_id, tip=start_point)
```

Cheap, reuses tested code, and the commit is a truthful record. It makes a
distinct commit subject (`recover(...)`, not `resolve(...)`) so the two paths stay
distinguishable in `git log`.

*Caveat to decide:* this commits work no reviewer has seen. It is on the group's
own branch, not integration, and §3 shows that merging such work unvalidated is
the real danger — so this fix must land **with** §3, not instead of it.

**Alternative — stash instead of commit.** `git stash push -u` before the refresh,
pop after. Keeps the branch history clean, but a popped stash can conflict, and a
stash is invisible to `git log` — worse for an operator hunting lost work.

**Alternative — refuse earlier, more usefully.** Detect the dirty tree at run
*start* and raise a message naming the backup command, rather than failing the
group mid-flight. Doesn't fix anything; just fails legibly. Worth doing anyway as
a message improvement.

### Test that would have caught it

Live tier, and it needs a real interrupted run, not a stub: start a group, kill
the orchestrator mid-round (SIGKILL, so no cleanup runs), assert the worktree is
dirty, then resume and assert the group reaches RUNNING. Nothing in the current
suite kills a run mid-round — that is the actual coverage hole.

______________________________________________________________________

## 3. OPEN — the autonomous resolver commits work it never validated

### What happens

After g6 failed, HITL raised a `group_resolve` escalation. Nobody answered, and
after 30 minutes:

```
15:56:38  ESCALATION 82aeb8db37fc timed out → autonomous
          → committed 889f2cd "resolve(r20260812-202855): g6 stranded work"
          → merge conflicted on app/routes/__init__.py
          → run exited: "run did not complete"
```

`_resolve_autonomously` (`scheduler.py:550`) commits the stranded work and merges
it straight to the integration branch. There is **no build, no test, no review**
between the commit and the merge.

### Why this matters more than it looks

I resolved that conflict by hand to see what was in it. The conflict itself was
trivial and the resolution obviously correct — integration had added `devices`
(g2), g6 had added `edits`, and the union of both routers is right; the merged app
imports with all 7 routers registered. But the suite on the merged tree gave **6
failures against 2 pre-existing**, and all 4 new ones were g6's own code:

```
app/edits.py: _spell                       <- notation logic inside app/, which
                                              test_app_holds_no_notation_or_chart_logic forbids
effective_chart() takes 2 positional arguments but 3 were given   (x3)
                                           <- g6's apply_edits calls an existing
                                              g3 function with a signature that
                                              does not exist
```

Attribution was verified, not assumed: the same test files at `9b1bbd7` (before
the merge) give **25 passed**. So these are g6's, not the conflict resolution's —
which touched only `app/routes/__init__.py`.

This is code caught **mid-thought**: a half-written edit layer calling a function
its author had not written yet. That is exactly what an interrupted coder leaves
behind. The resolver's instinct — never discard — is right; merging it to
integration unvalidated is not. Had it succeeded, g5 and g7 would have been built
on a broken base.

### Proposed fix

**Preferred — gate the resolve merge on the group's own verification.** The
orchestrator already knows how to run a group's checks. Before
`self._resolve.merge_group(group)`, run them in the group's worktree; on failure
leave the group FAILED with its work committed on its branch, and say so:

```
group g6: resolve committed 889f2cd but its checks fail — work preserved on
  orchestrator/r20260812-202855-g6, NOT merged (3 failures, see <path>)
```

That keeps the "never lose work" guarantee while dropping the "and ship it
unexamined" part. The branch stays as a starting point for a re-run.

**Alternative — never merge autonomously; only ever commit.** Simpler and safer:
autonomous resolve commits and stops, and merging always requires a human or a
re-run. Costs unattended throughput, which is the whole point of `--intensity
on_stuck`, so probably too blunt.

**Also worth changing regardless:** a `group_resolve` escalation timing out into
*merge* is a surprising default. Timing out into *commit-and-stop* would be a
safer floor whatever else is decided.

______________________________________________________________________

## 4. OPEN — FAILED is terminal, so a failed group can never be retried

`TERMINAL_STATES` (`scheduler.py:102`) includes `FAILED`, and `_resolve_failure`'s
docstring is explicit: "a FAILED group never re-enters `_run_group` on resume."

So g6's work sits committed on `orchestrator/r20260812-202855-g6` at `889f2cd`,
unmergeable (§3) and unreachable — `resume` will not touch it. The only route back
is editing `state.json` by hand.

Note also `resolve_settled` is still `False` for g6, because the resolve raised
`ResolveConflict` before it could settle. Per `_held_by` (`scheduler.py:615`) a
FAILED group with `resolve_settled == False` **holds every file-overlapping
group** — so a single unresolvable failure can silently strand successors too.
g5 is separately DAG-blocked behind g6, so this run cannot complete past g7.

### Proposed fix

**Preferred — an explicit operator command**, e.g.
`smart-mcps-orchestrate retry --repo <r> <run-id> <gid>`, which resets the group
to PENDING, keeps its branch and its worktree, and lets the normal path re-enter
it. This is the missing verb: today the CLI has `resume` (continue what was
interrupted) but nothing for "that one failed, try it again."

**Secondary — make the terminal state say so.** When a group goes FAILED, log the
recovery route explicitly (branch name, backup path, the command above). Right now
an operator has to read `scheduler.py` to learn that `resume` will not help.

**Consider — settle the hold.** A FAILED group whose resolve conflicted should
arguably stop holding its successors once an operator has been told about it,
otherwise one bad group quietly freezes unrelated work.

### The manual recovery, performed 2026-08-18 — this is what `retry` should do

g6 was unblocked by hand. The steps are worth reading as the specification for the
missing verb, because each one maps to something the orchestrator already knows how
to do:

1. **Make the branch mergeable again.** Merge the integration tip into the group's
   branch and resolve. The conflict was the same trivial router-registration union
   as before, now three-way (g6's `edits`, g2's `devices`, g7's `runs`):
   `from . import charts, devices, edits, export, jobs, kits, runs, songs`.
   Committed as `11a1017 refresh(g6): onto 34b2630 (operator-resolved router union)`.
   **This is the step that matters**: afterwards `34b2630` is an ancestor of g6's
   HEAD, so `_refresh_onto_tip` becomes a no-op and the failure at §2 cannot recur.
2. **Flip the terminal state.** In `state.json`, `g6.state` `failed` → `pending` and
   `g6.failure` → `null`. Nothing else. (Back up `state.json` first — done here to
   `.orchestrator/backups/state.json.before-g6-retry-<ts>`.)
3. **Resume normally.** No special flags.

Result: `group g6: worktree ready` with **no refresh error**, and re-entry picked up
the *warm* session `bdd6ce25` — the coder killed mid-round four days earlier — so it
continued with its context rather than starting cold.

A `retry <gid>` verb should therefore: refresh-or-report the branch, reset the state,
and re-enter. Step 1 is the only one needing judgement, and only when the refresh
conflicts; everything else is mechanical. Note that steps 1–3 are *also* what a
human would have to do after §3's proposed "commit but do not merge" outcome, which
is an argument for building the verb alongside that change.

______________________________________________________________________

## 5. OPEN — four reporting bugs that make a healthy run look broken (and vice versa)

None of these break execution. All of them mislead the operator, and two actively
train you to raise false alarms.

1. **A first round is logged "started" only after it has finished.** In
   `review.py::_run_generation`, `start_fork` is the call that *performs* round 1
   — it blocks for the whole thing — but `round N: started` is logged after it
   returns. So g3's log reads `started` and `ended` in the **same second**, with
   its real 12m55s of work in the gap *before* "started". Same-second start/end is
   precisely the signature an operator is told to treat as an empty-merge P0, so
   the log manufactures false alarms on healthy groups. **Fix:** log `started`
   before the `start_fork` call (the `coder launching, forking base session` line
   already sits in the right place — this is a two-line move).

2. **The heartbeat phase lies during a pause.** For the whole of a 4h16m usage
   limit, the group heartbeat read `still forking the base session` — it was
   parked in the gate. Only the interleaved `usage limit: still paused, ~Nm to go`
   line was truthful. **Fix:** the gate already has `phase_text()`
   (`ratelimit.py:420`); make it win over the group phase while a pause is active.

3. **A pause is billed to the round's elapsed time.** `round 1, 4h36m elapsed` of
   which 4h16m was waiting. Any breaker or human reading elapsed time sees a
   catastrophically slow round. **Fix:** subtract paused time from the round
   clock, or report both (`4h36m elapsed, 4h16m paused`).

4. **A resumed session's transcript path is never backfilled.** `_refresh_transcript`
   runs once, right after the fork. g6's gen-1 session died on the limit before a
   transcript existed, so the manifest shows `transcript_path: NO` forever even
   though the session later resumed and ran for hours. The Observatory loses the
   link to a transcript that is on disk — in exactly the sessions worth
   inspecting. **Fix:** re-run `_refresh_transcript` on re-entry.

### And one that is not cosmetic

**A run that dies while paused is indistinguishable from one that is waiting.**
The orchestrator was killed on 08-14; its last log line was a *pause* until 05:50.
Nothing was written afterwards, and `status` still reported `g6: running` **40
hours later**. Both surfaces an operator would check said "fine". A pid liveness
check, or a heartbeat file whose mtime `status` reads and reports, is the only
thing that separates them. Worth treating as its own fix — it is what let the run
sit dead for two days unnoticed.

*Operational note:* the run had been launched with `nohup … &` and still did not
survive its parent. Launch with `setsid nohup … < /dev/null &` (used since) so it
detaches into its own session.

______________________________________________________________________

## 5b. OPEN — two things found by reading the worktree directory

### The reviewer litters the group worktree, and `commit_all` would sweep it up

`.worktrees/g1-note-values-and-chart-model` is **dirty months after g1 completed**,
with one untracked directory:

```
?? .review-scratch/     32K — verify.py, verify2.py, verify3.py,
                        golden.py, golden_check.py, golden_triplet.py
```

The reviewer writes scratch programs into the group's worktree to check the coder's
work, and nothing removes them or gitignores them.

**This directly revises §2's proposed fix.** `commit_all` (`worktrees.py:244`) runs
`git add -A`, so it commits untracked files too. Commit-before-refresh, as written
in §2, would sweep `.review-scratch/` onto the group branch and from there into the
integration branch. Any fix must either:

- have the reviewer clean up its scratch (best — the litter has no reason to
  outlive the review), or
- gitignore a well-known scratch path and have `commit_all` respect it, or
- have the recovery commit stage explicit paths rather than `-A`.

Note the same hazard already applies to today's `commit_stranded` in
`_resolve_autonomously` — it is not introduced by §2, only made more likely.

### Group worktree paths omit the run id, so two runs collide

`worktree_path` (`worktrees.py:66`) is `.worktrees/<gid>-<slug(name)>` — no run id —
while `group_branch` is `orchestrator/<run_id>-<gid>` and the integration worktree
*is* run-scoped (`run-<run_id>-integration`). Consequences:

1. A directory listing cannot tell you which run a group worktree belongs to; you
   have to ask git for its branch. This repo currently shows `g1-…` and `g6-…`
   beside integration worktrees from three different runs.
2. Worse, a stale group worktree from an earlier run **blocks a later run's
   same-named group**: `ensure_group_worktree` finds the path, sees a different
   branch, and raises `worktrees.py:130` — "exists but is not a worktree on
   `<branch>`". Two runs over the same plan is not an exotic scenario; it is what
   re-running a validation looks like.

**Fix:** include the run id in the path, e.g.
`.worktrees/<run_id>/<gid>-<slug>` (also groups a run's worktrees together for
cleanup), or `.worktrees/<run_id>-<gid>-<slug>`. Either makes the collision
impossible and the listing self-explanatory.

______________________________________________________________________

## 6. Where things actually stand

Run `r20260812-202855`, target `/home/gbm1996/wksp/drummAI-practice-app`, branch
`validation/practice-app`.

```
g1 completed   g2 completed   g3 completed   g4 completed (gen 2)
g5 pending (DAG-blocked behind g6)
g6 FAILED — work committed, unmerged, unreachable by resume
g7 completed   g8 completed
```

**Seven of eight groups merged.** Integration branch
`orchestrator/run-r20260812-202855` is at `34b2630`; the g6 merge was attempted and
aborted, deliberately, so the branch is clean. `calibration-web/` verified
byte-identical throughout (empty diff against the base at every check).

**Suite on the integration branch: 435 passed, 9 skipped, 2 failed.** Both failures
are `tests/test_separate.py`, both `ModuleNotFoundError: No module named
'audio_separator'` — the known missing extra in fresh worktree venvs, confirmed by
reading the error rather than assumed from the count. **Zero regressions across
~11,000 inserted lines from seven groups.**

That is the headline the open bugs should be read against: the orchestrator *builds
software correctly*. What it cannot yet do is survive its own death.

### g6 recovery kit

- Committed stranded work: `orchestrator/r20260812-202855-g6` @ `889f2cd`.
- Backup taken before anything was touched:
  `.orchestrator/backups/g6-uncommitted-2026-08-16/` (`tracked.patch`, `untracked/`,
  `HEAD.txt`).
- To finish it, a coder must fix two things: move `_spell` out of `app/edits.py`
  (architectural test forbids notation logic in `app/`), and reconcile
  `apply_edits` with the real `effective_chart(settings, chart_id)` signature.
- The merge conflict itself is trivial and already solved: take the **union** —
  `from . import charts, devices, edits, export, jobs, kits, songs`, and both
  `devices.router` and `edits.router` in `ROUTERS`.

### Suggested order for the next session

1. §2 (commit-before-refresh) and §3 (gate the resolve merge) **together** — §2
   alone increases the amount of unvalidated committed work that §3 would ship.
2. §4's `retry` command, which is what makes g6 recoverable at all.
3. §5.1 and the liveness check — cheapest, and they remove the two ways this run
   misled me.

### Uncommitted in the orchestrator tree right now

The two §1 fixes and their tests are **not committed** (the tree already carried
unrelated in-progress work, so they were left separate):
`orchestrator/execution/sessions.py`, `orchestrator/execution/review.py`,
`orchestrator/config.py`, `tests/test_ratelimit.py`, `tests/test_sessions.py`,
`tests/test_streaming_live.py`, `tests/test_permission_patterns_live.py`.
Suite at time of writing: **1079 non-LLM passed**, LLM tier passed (one known
flake, `test_a_mid_round_followup_is_answered_and_still_terminates`, which fails
only under high API latency because `on_turn` may never fire).
