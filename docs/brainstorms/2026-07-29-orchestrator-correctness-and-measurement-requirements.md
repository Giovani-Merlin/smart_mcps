---
date: 2026-07-29
topic: orchestrator-correctness-and-measurement
---

# Orchestrator Correctness and Measurement — Requirements

## Summary

Close the three execution defects that let run `r20260726-grouping` mark a group `completed`
having merged nothing, make a permission denial a recoverable, visible event instead of a
silent context sink, give the grouper an explicit granularity dial, and — the thread that ties
the rest together — establish a way to tell whether any of it is working. Today the grouper is
tested for *legality* (acyclic, slices intact, within cap, byte-stable) and never for *quality*,
and the execution failures that actually cost a run have no regression test at all. This adds a
grouping scorecard, starts collecting attributable observations for a future corpus, and
fault-injects the four real failures into the existing zero-token stub harness.

## Problem Frame

Run `r20260726-grouping` merged all 7 groups, but not on its own. Two of the last three groups
(g5, g7) hit an intermittent `git commit` denial. g7 escalated because `--hitl` was on; **g5 did
not, and was marked `completed` having merged nothing** — its 237 insertions across 5 files were
recovered by hand from the worktree. The reviewer approved because it inspects the working tree,
not commits; the merge ran against a branch byte-identical to the integration tip and silently
no-op'd.

Three defects made that possible, and each alone leaves a hole the others fall through:

- `merge.py:63` `merge_group` never counts commits. An empty branch is indistinguishable from a
  real one.
- `worktrees.py:88` — when the branch already exists, `git worktree add <path> <branch>` ignores
  `start_point`. A resumed group keeps its original fork point and never sees merges that landed
  while it was down. g7's branch was at `697b98f` and contained neither g5's nor g6's work.
- `scheduler.py:300` `_blocked_by_failure` strands only *transitive DAG dependents*. Dependencies
  are logical, not file-based, so when g5 failed, g7 was DAG-unblocked and ran — even though both
  edit `cli.py`, `pipeline.py`, `partition.py`.

The permission denial itself is unexplained and is **not** being root-caused here. What is known
and should not be re-derived: 9 command variants were denied including a bare probe matching the
allowlist rule character-for-character; the rule was present in the worktree's
`.claude/settings.json`; it is intermittent (g5 ✗ 19:23Z, g6 ✓ 19:38Z, g7 ✗) under identical
settings; and the operator ran the same `git commit` in g7's own worktree minutes later and it
succeeded. Settings and allowlist-shape hypotheses are dead. What the coder does *in response*
is fixable regardless of cause, and that is the scope taken here.

The cost of not fixing the response is measured. In g4's transcript: **82 Bash calls, 26
permission denials** (≈1 in 3 wasted, each a full turn), **32 calls routed through a
`python3 -c` + `subprocess.run` workaround**, only 7 direct pytest invocations attempted — 332,522
context tokens, driven by the permission gate rather than by the work.

Prior art in this repo: the grouping side has already been through this cycle. D2/D3/D4 were
resolved in the 2026-07-25 grouping-improvement plan, and the fixture register
(`tests/fixtures/grouping/`, 7 plans) plus three property tests came out of the run-hardening
requirements (R20/R21). Those assert legality only. `docs/orchestrator-grouping.md` documents the
methodology well across 482 lines; the ~30 config knobs it depends on are documented nowhere
outside `config.py` docstrings, and exactly one (`token_budget`) is CLI-reachable.

## Key Decisions

- **Permission denials are contained, not root-caused.** The cause is unknown, intermittent, and
  session-scoped; chasing it is open-ended and blocks everything else. A typed, recoverable denial
  plus the commit gate makes the failure visible and cheap whatever the cause. Rejected:
  filling `SessionConfig.allowed_tools` with a default (a cheap hedge, but it cannot cure a
  session-scoped cause and would look like a fix); a pre-run permission probe; and an explicit
  orchestrator-declared permission contract — the user's call, on the grounds that the ambient
  `.claude/settings.json` already carries the allowlist and duplicating it adds surface without
  addressing the cause.

- **A permission denial is an Envelope Failure, not a Work Failure.** `CONTEXT.md` already defines
  an Envelope Failure as "caused by the harness, not the work… Recoverable by definition: the work
  itself was never judged." A denial is exactly that. Today it arrives as a generic `blocked`
  report, which `review.py:552` routes to escalate-then-rewrite, burns both `max_rewrites`, and
  lands in terminal `FAILED` — three coder generations spent on something a `resume` fixes.

- **HITL escalation is on by default.** `EscalationConfig.enabled = False` means an unattended run
  has no one to ask, which makes the M3 requirement ("ask before continuing past a failure")
  unimplementable in its own default configuration. Running with `--hitl` is what saved g7 and
  what g5 lacked. The halt path is retained for runs that explicitly disable it.

- **Granularity is an explicit flag, orthogonal to concurrency.** `merge_small_groups`
  (`partition.py:788`) is already maximally aggressive; what limits it is `chain_compatible`
  (`partition.py:811`) refusing to merge groups that are not dependency-ordered, plus the makespan
  no-regression guard. Deriving the relaxation from `concurrency == 1` was considered and
  **rejected**: those guards keep groups *disjoint*, and file overlap between groups is precisely
  the M3 hazard — so independence is a correctness property that a serial run still wants, not
  parallelism it has given up. Level names are `independent` / `balanced` / `monolithic`;
  `parallel` was rejected as the default's name for exactly this reason.

- **A golden corpus is started, not asserted.** `.orchestrator/groupings/` is empty, no
  `grouping-trace.json` exists anywhere in the repo, and neither historical run directory carries
  its grouping snapshot. `GroupingTrace` (`trace.py:172`) records the full pipeline but has **no
  provenance at all** — no timestamp, plan hash, repo SHA, or codegraph index fingerprint — and is
  overwritten whenever `group --name <same>` re-runs. A partition also depends on the codegraph
  index at the moment it ran, so re-running an old plan today yields a different, incomparable
  result. Assertion against golden files is therefore not possible yet; making each grouping
  self-dating and self-measuring, appended somewhere never overwritten, is what makes it possible
  later.

- **Fault injection uses the existing stub harness.** `tests/fake_claude.py` impersonates the
  `claude` executable at zero token cost while everything else runs for real — `SessionRunner`,
  `ReviewLoop`, `Scheduler`, `IntegrationMerger`, real `git worktree add`, real `git merge --no-ff`
  — and a scripted coder performs real file writes and a real `git add -A && git commit`
  (`fake_claude.py:238`). Omitting the `commit` key produces a genuinely empty branch, reproducing
  g5 in milliseconds. Rejected: unit-testing the new guards in isolation (proves the functions
  work, not that they are called at the right moment, which is exactly where g5 slipped through);
  and a live opt-in `@pytest.mark.llm` smoke run (real tokens, deferred).

## Requirements

### Execution correctness

- R1. `IntegrationMerger.merge_group` counts commits on the group branch **before** attempting the
  merge — `git rev-list <base>..<group-branch>` — and fails loudly on zero, naming the group and
  its branch. The check must precede the merge: afterwards the branch is an ancestor of
  integration and returns 0 either way. A group that contributed no commits must never reach
  `completed`.
- R2. On re-entry, an existing group branch is fast-forwarded onto the current integration tip
  before the coder resumes. If it cannot fast-forward (it has diverged), the group fails loudly
  naming the group and the divergence rather than proceeding on a stale base. This is the
  `create_worktree` path that silently ignores `start_point` for an existing branch.
- R3. A group's worktree is cut from the **current** integration tip, not the last successful
  one — a predecessor that failed must not cause its successor to be branched from before it.
- R4. When a group ends `failed` or `interrupted`, before any further group starts: compute file
  overlap between that group's declared files and each remaining group's. With HITL enabled, raise
  an `EscalationBroker` escalation naming the failed group, the overlapping files, and the concrete
  risk, offering (a) resolve now — fix, commit, and merge the failed group so the run continues
  genuinely serially — or (b) continue anyway, having been warned a merge conflict is likely.
  With HITL disabled, stop scheduling new groups and exit with a clear status. Silent continuation
  is never the default.
- R5. `EscalationConfig.enabled` defaults to `True`.

### Permission denials

- R6. The coder prompt forbids working around a denied tool call: at most one retry of the
  identical command, then report. No alternate quoting or spellings, no shelling through another
  interpreter, no `python3 -c` + `subprocess.run` substitution for a denied command.
- R7. A denial is reported in a form the review loop can distinguish from a generic `blocked`
  report without parsing prose, and carries the exact command that was denied. Whether that is a
  new report status or a dedicated surprise kind is a planning-time choice.
- R8. A typed permission denial is classified as an Envelope Failure: the group becomes
  `interrupted` and is picked up by a plain `resume`. It does not consume `max_rewrites` and does
  not reach terminal `FAILED`.

### Grouping granularity

- R9. `group` accepts `--granularity {independent,balanced,monolithic}`, defaulting to
  `independent` (today's behaviour, both guards on). `balanced` relaxes the makespan
  no-regression guard while keeping chain-compatibility. `monolithic` drops both, leaving the
  budget cap as the only limit on merging. Also settable in `config.toml`.
- R10. The declared-slice must-link and the budget cap remain hard constraints at every
  granularity level: no level may split a slice or exceed the cap.

### Measurement and test methodology

- R11. A grouping scorecard is computed for every partition: group count, cross-group edge count,
  per-group work as a fraction of the budget cap (min / mean / max), critical-path length,
  modularity, and slice integrity as a pass/fail fact. It is printed by `group --no-spec` and
  recorded in the trace.
- R12. `GroupingTrace` gains provenance sufficient to attribute a partition to the exact inputs
  that produced it: timestamp, plan path and content hash, repo commit SHA, an identifier for the
  codegraph index state, and the resolved config (it already carries `config`). Whichever
  identifier codegraph can supply for its index is acceptable so long as two different index
  states cannot share one. Without this a trace is not comparable to any other.
- R13. Every `group` invocation appends one row — scorecard plus provenance — to a durable
  append-only log at `.orchestrator/grouping-metrics.jsonl`, never overwritten by a re-run of the
  same grouping name. This is the collection step; optimising against the accumulated corpus is
  out of scope here.
- R14. Four fault-injection scenarios land in the stub harness:
  - R14a. A coder that writes files and never commits — the merge is refused and the group does
    not reach `completed` (R1).
  - R14b. A group interrupted, a sibling merged while it was down, then the group resumed — its
    branch is refreshed onto the tip, or fails loudly (R2).
  - R14c. A group that fails while a later group shares declared files — an escalation is raised
    with HITL on, the scheduler halts with HITL off (R4).
  - R14d. A coder that reports a permission denial — the group goes `interrupted`, stays
    resumable, and burns no rewrites (R8).
- R15. The existing property tests (acyclicity, slice integrity, within-cap, byte-stability) hold
  at every granularity level and are the guard on R9; the fixture register is extended to cover
  the non-default levels.

### Calibration and durability

- R16. `BreakerConfig.context_token_limit` default moves from `120_000` to `200_000`, and the
  repo's `config.toml` 600k override is removed. 600k is likely unreachable — a measured
  compaction went `preTokens 463,426 → postTokens 21,950`, so the CLI rescues the session before
  the breaker could ever trip.
- R17. The grouping-improvement plan's `grouping-trace` slice (83,124 tokens against an 83,070
  cap — a 0.06% overshoot) groups without `--allow-oversized-slice`. Re-measure after R9 lands,
  since granularity changes the partition; if the overshoot survives, raise the default
  `estimator.token_budget` rather than requiring an override for a legitimate declared slice.
- R18. Runs no longer override plan-declared review intensity by default. All 7 groups of the last
  run declared `self_verify` (difficulty 0.136–0.293, every one below `d_review`) and
  `--review-intensity paired` created 7 unnecessary reviewer sessions at 72k–106k context each.
  The flag stays available as an explicit override.
- R19. `SurpriseBoard` (`review.py:88`, a plain in-memory dict) persists to the run directory, so
  pending cross-group surprises survive a restart. g4's surprise reached g5 but was lost before
  g6.
- R20. A coder session is recorded before its round is awaited (`review.py:200` records
  `first.session_id` only after `start_fork` returns). A group interrupted during its first round
  currently has no `SessionEntry`, so `_find_reentry_session` returns `None` and warm re-entry is
  impossible exactly when it matters. `start_fork` already generates the id internally.

### Reporting

- R21. `group --no-spec` prints DAG edges in the correct direction — it currently lists downstream
  groups under "depends on".
- R22. `status` clears a group's `failure` field on success; it currently prints a stale
  `failure:` line under a `completed` group.
- R23. Reviewer sessions record `last_context_tokens`. They are all zero in the manifest today
  against real occupancy of 72k–106k, making the manifest useless for reviewer-side cost
  accounting.

### Documentation

- R24. A grouper argument and configuration reference documents every knob: what it does, which
  direction moves grouping which way, and whether it is CLI-reachable or config-only. It covers
  the ~30 fields in `config.py` that today exist only as docstrings, and the granularity semantics
  from R9. The existing methodology write-up in `docs/orchestrator-grouping.md` is not duplicated.

## Non-Goals

- **Root-causing the intermittent permission denial.** Contained (R6–R8), not explained.
- **An orchestrator-declared worker permission contract.** `SessionConfig.allowed_tools` stays
  empty by default; workers continue to inherit the repo's ambient `.claude/settings.json`.
- **A pre-run permission preflight probe.**
- **Asserting partitions against golden files.** R11–R13 collect attributable observations; the
  corpus and any tuning against it come later.
- **A live `@pytest.mark.llm` end-to-end smoke run.** The opt-in marker already exists and stays
  unused by this work.
- **Changing the partition algorithm itself.** R9 relaxes two existing guards under an explicit
  flag; Louvain, hub detection, slice contraction, splitting, and DAG repair are untouched.
- **`bypassPermissions` as a worker permission mode.**

## Open Questions

None.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-07-29-orchestrator-correctness-and-measurement-requirements.md`.
