---
title: Orchestrator Correctness and Measurement
type: fix
date: 2026-07-29
origin: docs/brainstorms/2026-07-29-orchestrator-correctness-and-measurement-requirements.md
---

# Orchestrator Correctness and Measurement

## Objective

Close the three execution defects that let run `r20260726-grouping` mark a group
`completed` having merged nothing (R1–R5), make a permission denial a typed,
recoverable, visible event instead of a silent context sink (R6–R8), give the
grouper an explicit granularity dial (R9–R10), and establish the instruments to
tell whether any of it works — a grouping scorecard with attributable provenance,
an append-only metrics log, and four fault-injection scenarios in the zero-token
stub harness (R11–R15). Plus the calibration, durability, and reporting fixes the
same run surfaced (R16–R24).

All 24 R-IDs are covered. Three were amended against the code during planning and
are marked **[amended]** where they appear.

## Execution status — updated 2026-07-30 (run `r20260729-correctness`)

**7.5 of 9 units are done and merged into `feat/multiagent-orchestrator`.** The run was
stopped after g2 because the orchestrator's own per-group overhead was not worth the
remaining work; U6's tail and U9 are to be finished in a single monolithic session.

| unit                      | status                                 | evidence                        |
| ------------------------- | -------------------------------------- | ------------------------------- |
| U1 merge-integrity        | ✅ done, reviewed, merged              | `28b930b`                       |
| U2 failure-gate           | ✅ done, reviewed, merged              | `98ee16a`                       |
| U7 durability             | ✅ done, reviewed, merged              | `089a0c6`, `ae1fd46`, `cd2b6c0` |
| U8 operator-surface       | ✅ done, reviewed, merged              | `6464cb5`                       |
| U3 typed-denial           | ✅ done, merged (no reviewer pass)     | `e1c7ab8`                       |
| U4 granularity            | ✅ done, merged (no reviewer pass)     | `d4a4f4b`                       |
| U5 scorecard              | ✅ done, merged (no reviewer pass)     | `6bc20bf`, `7bdfe37`            |
| **U6 fault-injection**    | ⚠️ **4 of 5 scenarios green, 1 hangs** | `875d47c`                       |
| **U9 conflict-exclusion** | ❌ **not started**                     | —                               |

g1 (U1/U2/U7/U8) completed the full loop including reviewer approval and merged cleanly.
g2 (U3/U4/U5/U6) was cut off mid-U6 by a usage limit, so its four units landed **without a
reviewer pass** — they are merged on the strength of a green suite, not a review.

### What is left, precisely

1. **U6** — `tests/test_e2e_faults.py::test_fault_stale_base_resumed_group_absorbs_a_concurrent_sibling_merge`
   **blocks indefinitely instead of failing**, which wedges the whole suite, so it is
   `@pytest.mark.skip`-ed. Un-skip and fix it. The other four scenarios pass. Likely cause: an
   escalation wait that never receives an answer — it is the one scenario driving a resume
   across a sibling merge.
2. **U9** — conflict-exclusion, untouched. `self_verify` intensity, the smallest unit in the plan.

### Defects this run found in the orchestrator that this plan does *not* fix

Recorded because they cost real credits and are invisible from the code alone. Full evidence
in `.orchestrator/notes-r20260729-correctness.md` (findings 1–11).

- **A failed round reports an empty reason.** `sessions.py` surfaces only `stderr`, but the CLI
  writes to `stdout` under `--output-format json`, so every failure reads
  `SessionError: claude exited 1 (…): `. Diagnosing anything requires hand-reading transcript
  jsonl. Highest-value fix remaining.
- **A usage-limit exit is not classified as one** — it presents as a generic envelope failure.
- **Escalation config is not persisted**, so `resume` silently returns to `autonomous` unless
  every flag is retyped.
- **A worker can write outside its worktree** — the g1 coder edited the operator's global
  auto-memory and marked its own group closed before review.
- **Lifecycle log lines are written only when a round completes**, so a working coder and a hung
  one look identical; g2's 41 minutes produced no log line at all.
- **`context_token_limit` is a re-entry admission gate, not a circuit breaker** — nothing bounds
  context within a round (observed peak 439,575 tokens against a 200k setting).
- **The run dies with the session that launched it** unless detached (`setsid`).

**Fixed and verified during the run:** U7's pre-fork session recording (`ae1fd46`) made warm
re-entry work — g2's interrupted coder left a usable session entry where the same failure
previously persisted nothing at all.

## What we already know (resolved context)

Established by reading the code this session. A worker should not re-derive any of it.

### The g5 failure chain — three defects, each covering the next one's blind spot

1. `IntegrationMerger.merge_group` (`orchestrator/execution/merge.py:63`) never
   counts commits. It reads the branch name, runs `git merge --no-ff`, and treats
   returncode 0 as success. A branch byte-identical to the integration tip merges
   as a silent no-op, indistinguishable from real work.
2. `create_worktree` (`orchestrator/execution/worktrees.py:72`) has **two** paths
   that skip `start_point`, not one. Line 88: when the branch already exists,
   `git worktree add <path> <branch>` ignores `start_point` entirely. Lines 79–82:
   when the *worktree* already exists on the right branch, it is returned as-is
   with no refresh at all — and worktrees are **not** removed on interrupt, so this
   is the more common resume case. g7's branch sat at `697b98f` and contained
   neither g5's nor g6's work.
3. `Scheduler._blocked_by_failure` (`orchestrator/execution/scheduler.py:300`)
   strands only transitive DAG dependents. Dependencies are logical, not
   file-based, so g7 was DAG-unblocked and ran even though it and g5 both edit
   `cli.py`, `pipeline.py`, and `partition.py`.

The reviewer approved g5 because it inspects the working tree, not commits.

### Why the orchestrator can recover what the coder could not

`worktrees.py:43` (`_git`) shells `subprocess.run(["git", ...])` directly. The
orchestrator process is **not** inside the worker's permission sandbox — which is
exactly why the operator's `git commit` succeeded in g7's worktree minutes after
the coder's was denied. This is the mechanical basis for the resolve routine
(ADR 0004).

### Current state of each surface the plan touches

| Surface                    | Fact                                                                                                                                                                                                                                                                                                  |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CoderReport.status`       | `Literal["completed","blocked","failed","needs_input"]` (`model.py:125`), with a `model_validator` already precedent-setting for conditional required fields (`needs_input` ⇒ `question`).                                                                                                            |
| Report routing             | `review.py:236` sends every non-`completed` report to `_on_coder_stuck` → escalate-then-rewrite → burns both `max_rewrites` → terminal `FAILED`.                                                                                                                                                      |
| Envelope classification    | `scheduler.py:274` catches `(SessionError, LlmProcessError)` → `INTERRUPTED`. `ReportError` is deliberately excluded as a work failure (`scheduler.py:269`).                                                                                                                                          |
| `GroupState`               | `pending, ready, running, reviewing, rewriting, merging, completed, failed, interrupted`; `TERMINAL_STATES = {COMPLETED, FAILED}` (`scheduler.py:48-64`).                                                                                                                                             |
| Escalation wiring          | `EscalationBroker`/`EscalationPolicy` are built at `cli.py:664-677` and injected into `ReviewDeps` — **after** the `Scheduler` is constructed at `cli.py:616`. The scheduler has no broker today; the R4 gate needs that plumbing.                                                                    |
| `HumanAction`              | `answer, skip, abort` (`model.py:169`). `EscalationKind` has 9 members (`model.py:150`); the tier matrix is `escalation.py:36-55`.                                                                                                                                                                    |
| `EscalationConfig.enabled` | `False` (`config.py:141`). `timeout_s = None` (block indefinitely) is already the HITL default.                                                                                                                                                                                                       |
| Fresh worktree cut         | `workspace_for` (`cli.py:734-744`) already calls `merger.tip()` per group at its ready→running transition; `tip()` is `rev-parse <integration-branch>` (`merge.py:56-61`).                                                                                                                            |
| `--review-intensity`       | Defaults to `None` (`cli.py:198`); rewrites intensities only when explicitly passed (`cli.py:548-550`).                                                                                                                                                                                               |
| Effective-config print     | `cli.py:566-582` already prints mode/HITL/permission-mode before any session spawns — the natural host for the intensity warning.                                                                                                                                                                     |
| `merge_small_groups`       | `partition.py:788`, always on. Two guards limit it: `chain_compatible` (`partition.py:811`) and the makespan no-regression check (`partition.py:840`, `_simulate_makespan` at `:729`). Budget cap and cycle checks are separate and stay hard.                                                        |
| `GroupingTrace`            | `trace.py:172`. Carries `config`, stages, louvain, splits, merges, repairs, dag, flags. **No provenance at all** — no timestamp, plan hash, repo SHA, or index identifier. Overwritten by `group --name <same>` (`cli.py:355-359`).                                                                   |
| Modularity                 | **Not computed anywhere.** Referenced only in comments (`graphing.py:350`, `partition.py:422`). R11 needs it implemented fresh over the affinity graph.                                                                                                                                               |
| `codegraph status -j`      | Returns `{initialized, projectPath, fileCount, nodeCount, edgeCount, dbSizeBytes, backend, journalMode, nodesByKind, languages, pendingChanges{added,modified,removed}, worktreeMismatch}`. No content hash; the CLI offers no other index identifier.                                                |
| `SurpriseBoard`            | `review.py:83`, a plain in-memory `dict` + `Lock`. Dies with the process.                                                                                                                                                                                                                             |
| Coder session recording    | `review.py:200-201` sets `coder_sid` and calls `_record` only **after** `start_fork` returns. `_find_reentry_session` (`review.py:278`) therefore finds nothing for a group interrupted during its first round.                                                                                       |
| Reviewer context           | `_persist_coder_usage` (`review.py:332`) exists for coders only; `_review_round` (`review.py:344`) calls `_record` at `:365` and never persists usage.                                                                                                                                                |
| `--no-spec` DAG print      | `cli.py:398-402` reads `trace.dag.get(gid)` — the **downstream** map — and labels it "depends on".                                                                                                                                                                                                    |
| Stale `failure`            | `Scheduler.set_state` (`scheduler.py:135-141`) writes `failure` only when non-`None`, so a value set on an earlier attempt survives a later success; `_cmd_status` prints it at `cli.py:841-842`.                                                                                                     |
| Stub harness               | `tests/fake_claude.py` impersonates the `claude` binary at zero token cost. A scripted response's `files` key performs real writes and `commit` performs a real `git add -A` + `git commit` (`fake_claude.py:234-245`). **Omitting `commit` produces a genuinely empty branch** — g5 in milliseconds. |
| Fixture register           | `tests/fixtures/grouping/` — 7 plans; property tests assert legality only (acyclic, slice intact, within cap, byte-stable).                                                                                                                                                                           |
| Unknown symbols            | A task-map symbol missing from the codegraph index is a **hard error** by default; `--allow-unknown-symbols` downgrades it (`cli.py:119-126`). Every symbol in this plan's map was verified present.                                                                                                  |

### The grouper P0 — diagnosed, fixed, and merged before this plan runs *(added 2026-07-29)*

This plan could not be grouped at all when it was written: `group` collapsed all 8
units into one 321k-token group, 3.8× the 84k cap, and **exited 0**. Fixed on
`feat/multiagent-orchestrator` outside this plan (435 → 459 tests green). A worker
must not re-derive any of the following.

**Root cause.** `build_task_graph` turned every codegraph `callers`/`callees`/`impact`
relation into a *directed precedence* edge. On this plan that produced **52 of 56
possible directed edges among 8 tasks — one SCC**, so the only acyclic partition was
the degenerate single group. Amplifiers, over 1,969 edge instances: `impact -d 2`
contributed 1,690, and `owners_of`'s file-ownership fallback matched 1,781 vs. 188 by
symbol (five of eight units map `orchestrator/cli.py`). Saturation also made all 8
nodes classify `aggregator_hub`.

**The distinction that resolves it, and its established names.** A structural
reference is *coupling*, not *ordering*: codegraph says how code references code
today, while task precedence is a claim about the intended change. Change-impact
analysis calls this **change set** vs **impact set** — an impact set guides review and
retesting and is explicitly not a set of mandatory edits, let alone an order. Edges
that constrain a schedule without real precedence are **pseudo-edges** /
**fictitious dependencies**, whose documented cost is a longer critical path and lost
parallelism.

**What shipped.** `graphing._drop_inferred_cycles` withdraws *inferred* precedence
until `dependencies` is a DAG — mutual pairs first, then residual SCCs — never
touching declared `depends_on`. Withdrawal costs no cohesion because
`_EdgeAccumulator.add` already banked the weight in `affinity`. Guards:
`TaskGraph.assert_acyclic_dependencies()` (builder-output contract) and
`pipeline._check_degenerate_partition` (repair overshoot is now a `GrouperError`, with
`--allow-degenerate-partition` as the hatch). Coverage: `hub-file-symbols` fixture +
`tests/fixtures/codegraph_hub/` cassette — the register previously excluded `symbols`
from **every** fixture by construction, which is why this shipped undetected.

**Result on this plan:** 52 → 9 dependency edges (4 declared + 5 surviving inferred),
6 groups all under cap, `last_stage=lift`, `repairs=0`, hub roles 3-of-8 non-core.

**Three things not to relearn:**

1. The acyclicity assert must **not** live in `TaskGraph.__post_init__`.
   `_contract_slices` legitimately creates cycles absent at task level (`a1→b1`,
   `b2→a2` is acyclic; contracting `a1+a2` and `b1+b2` yields `s1⇄s2`) — that is what
   `repair_cycles` exists for. The invariant is on *builder output*.
2. **The wave-skeleton idea is verified wrong.** "Condense into waves, cluster within
   a wave, merge only across adjacent waves" is *not* acyclic by construction.
   Counterexample checked against this repo: `a→d`, `c→b` with waves
   `{a:0, c:0, b:1, d:1}`; merging `a+b` and `c+d` (both adjacent) raises
   `GroupCycleError`. Only complete consecutive waves, or contiguous intervals of one
   fixed topological order (dagP's initial partitioning), are safe.
3. The fix is deliberately **narrower** than "route all reference edges to affinity".
   That stronger version would starve `dependencies` and break three stages:
   `lift_independent` splits hub-less groups by dependency components (→ singletons),
   `chain_compatible` requires pairs to be dependency-reachable (→ refuses nearly every
   merge), and `_louvain` reads `dependencies` only to pick edge direction (→ silently
   becomes undirected). Five inferred edges survived, so none materialised. **If a
   follow-up narrows the signal further, re-check these three.**

Full write-up, including the external-research pass:
`.orchestrator/notes-grouper-derived-dependency-cycle.md`. Methodology and the
47-field knob reference: `docs/orchestrator-grouping.md`,
`docs/orchestrator-grouping-config.md`.

### Operator actions, not worker work

`.orchestrator/config.toml` is **gitignored**. A worker editing it inside a
worktree produces no merge content — the change would evaporate at merge time. It
is therefore absent from the task map:

- The operator has already set `[breaker] context_token_limit = 200000` there.
- Once `config.py` defaults to 200 000 that override is redundant, and its comment
  ("Set here rather than in `orchestrator/config.py` because g5 and g6 of the
  in-flight run both edit that file") is false — those groups merged on 2026-07-29.
  **Operator: delete the `[breaker]` block from `.orchestrator/config.toml`.**

## Decisions

- **A resumed branch is refreshed with `git merge <tip>`, not `--ff-only`.** *[amended R2]*
  R2 as written cannot succeed: git only fast-forwards a branch that is strictly
  behind, so a group branch that committed anything has diverged by definition and
  `--ff-only` would reject exactly the resumed groups R2 exists to rescue. `git merge`
  fast-forwards when possible, makes a merge commit when diverged, and fails only on a
  real content conflict. Applied to **both** `create_worktree` paths, not just the
  branch-exists path R2 names. *Rejected:* literal `--ff-only` (fails the common case);
  rebase (rewrites SHAs a warm coder session already referenced in its context).

- **R3 needs no new cut logic.** *[amended R3]* `workspace_for` (`cli.py:734-744`)
  already cuts from `merger.tip()` at each ready→running transition. A failed
  predecessor's work is absent from the tip because it was never merged, not because
  the tip is stale — so R3 is delivered by the resolve routine moving the tip, plus a
  regression test pinning the fresh-cut behaviour.

- **A permission denial is a fifth `CoderReport.status`.** `permission_denied` with a
  validated non-empty `denied_command`, mirroring how `needs_input` already validates
  `question`. The review loop branches on it before `_on_coder_stuck` and raises
  `PermissionDenied`; the scheduler adds that type to its envelope tuple.
  *Rejected:* a `blocked` report discriminated by field emptiness (a coder filling the
  field on an unrelated blocked report silently gets envelope classification); a new
  `Surprise.kind` (surprises are the cross-group channel — `_spread` fans them to
  *other* groups, and a denial names none).

- **One resolve routine, two entry points.** HITL on → the escalation blocks (it
  already blocks indefinitely by default) while the operator fixes and merges by hand,
  and the orchestrator **verifies containment** before releasing successors; the
  operator may instead delegate the resolve. HITL off → the orchestrator resolves
  autonomously. *Rejected:* advisory-only (takes the operator's word, re-admitting
  silent continuation one level up); a merge seam that reverses a terminal state.

- **Resolve = commit the stranded work, then merge; the group lands `RESOLVED`.**
  Merging only already-committed work cannot recover g5, the motivating case. The new
  terminal state keeps a banked-but-unreviewed group from claiming a verdict it never
  had. (→ ADR 0004)

- **The gate splits by state.** `FAILED` → resolve. `INTERRUPTED` → never resolve: its
  work is unfinished by definition and warm `Re-entry` is built to finish it. Instead
  hold only the remaining groups that share a declared file and let independent ones
  keep banking. *Rejected:* one path for both (auto-merges half-written code on a
  transient usage limit and destroys the warm resume).

- **The coder retries an identical denied command three times, then reports.** At the
  measured ~1-in-3 denial rate, two attempts escape ~11% of the time and three ~4% —
  roughly 3 interruptions per g4-sized group down to ~1. Re-sending the *identical*
  command is not a workaround, so R6's ban on alternate quoting, alternate spellings,
  interpreter shims, and `subprocess.run` substitution is unchanged.
  *Rejected:* "retry until you judge it persistent" (unbounded, untestable, and exactly
  the improvisation R6 targets).

- **The index fingerprint is a sha256 of canonical `codegraph status -j`,** recorded
  beside the repo SHA and a worktree-dirty flag. R12's literal invariant ("two different
  index states cannot share one") is not reachable — codegraph exposes counts, not a
  content hash, and the only true fingerprint (the `.db` bytes) churns constantly under
  WAL and would report identical indexes as different. The pair carries the invariant in
  practice: the SHA distinguishes repo content, `pendingChanges` inside the digest
  distinguishes a stale index from a synced one at the same SHA. Residual documented.

- **R18 is a warning, not a default change.** *[amended R18]* `--review-intensity`
  already defaults to `None`; the 600k went to 7 reviewer sessions because the operator
  typed the flag. The fix is a warning on the effective-config line the run already
  prints, plus a regression test pinning the no-override default.

- **Granularity is an explicit flag, orthogonal to concurrency.** Deriving it from
  `concurrency == 1` was rejected in the brainstorm and stays rejected: `chain_compatible`
  keeps groups *disjoint*, and file overlap between groups is precisely the R4 hazard —
  independence is a correctness property a serial run still wants.

- **Grouping needs three relations, not two.** *[added 2026-07-29]* Cohesion
  (symmetric, weighted, from codegraph — which answers it well), precedence (directed,
  acyclic, from the plan's `depends_on` — which codegraph answers *not at all*), and
  **conflict** (symmetric, boolean, derivable from `Group.files` with zero planner
  input). The orchestrator conflated the first two, which caused the P0 above; it does
  not model the third at all, which is U9. *Rejected:* deriving precedence from code
  structure to compensate for sparse `depends_on` — that is the defect, not the fix.

- **Conflict is mutual exclusion, not ordering.** *[added 2026-07-29]* Two groups
  editing the same file must not run concurrently, but *either* order is safe, because
  `workspace_for` already cuts each worktree from `merger.tip()` at its ready→running
  transition. So the scheduler needs a symmetric "not at the same time" relation, never
  a directed edge. This is standard: GitLab CI `resource_group`, Jenkins Lockable
  Resources and Zuul semaphores all model it separately from the DAG, and the formal
  frame is conflict-serializability (groups are transactions, files are items, conflict
  is `W(G1) ∩ W(G2) ≠ ∅`, any serial order suffices). *Rejected:* flattening conflict
  into a directed dependency edge — the common pragmatic shortcut, but it is exactly the
  pseudo-edge over-constraint that produced the P0, and we already have
  `build_group_dag` to check acyclicity independently.

- **File-level conflict granularity, knowingly.** *[added 2026-07-29]* Two edits to
  different functions in one file will serialize needlessly. Symbol/hunk granularity is
  more precise but needs identifiers stable across the very refactorings being
  scheduled. `file_owner` already exists in `graphing.py`; take the false positives.

- **We are not adopting convexity, and not rewriting the partitioner.**
  *[added 2026-07-29]* Convexity (every directed path between two members stays inside
  the block) is strictly stronger than what the scheduler needs, and tends toward either
  very coarse or fragmented groups. Acyclicity of the *quotient* graph is the right,
  weaker constraint — which is what `build_group_dag` already checks. Likewise the
  multilevel acyclic-partitioning line (dagP; Moreira/Popp/Schulz) is the known better
  form of a construction-time redesign, but post-hoc repair is itself a recognised
  pattern whose documented failure mode is exactly ours, and with precedence now sparse
  `repairs` measures **0** on this plan. Re-measure before redesigning.

- **Open, and only the planner can close it.** *[added 2026-07-29]* Whether to keep
  *any* call-derived precedence. Refactoring literature (Opdyke 1992; Mens & Tourwé
  2004\) is right that contract-changing edits do impose order — change the definition,
  migrate callers, remove the old path — and the current code already encodes that
  direction. But nothing in `TaskMapping` distinguishes "this task changes the symbol's
  contract" (order is real) from "this task edits the body" (no order implied). That
  flag is a task-map schema change sourced from the planning session, not something the
  partitioner can infer. Out of scope here; recorded so it is not rediscovered.

## Units

### U1. u1-merge-integrity — no group merges nothing, and none builds on a stale base

- **Goal**: `merge_group` refuses a branch with zero commits, and every re-entered
  group branch carries the current integration tip before its coder resumes.
- **Files**: `orchestrator/execution/merge.py`, `orchestrator/execution/worktrees.py`,
  `orchestrator/cli.py`, `tests/test_merge.py`, `tests/test_sessions.py`
- **Symbols**: `merge_group`, `IntegrationMerger`, `MergeError`, `create_worktree`,
  `worktree_path`, `_registered_branch`, `is_dirty`
- **Depends-on**: —
- **Slice**: merge-integrity
- **Implements / Consumes**: implements `merge-gate`, `branch-refresh`
- **Verification**:
  - Merging a group branch with zero commits ahead of the integration branch raises an
    error naming the group id and its branch, and the integration branch's commit SHA is
    unchanged afterwards.
  - A branch carrying real commits still merges cleanly, and the merge commit's message
    names the run and the group.
  - The commit count is taken before the merge: a test asserts that running the same
    count after a successful merge reports zero, documenting why the order matters.
  - A group worktree whose branch is strictly behind the integration tip has HEAD equal
    to that tip after re-entry, with no new merge commit created.
  - A group worktree whose branch has its own commits and is also behind the tip reaches
    a HEAD from which both its own commits and the tip's commits are reachable.
  - A refresh that hits a content conflict raises an error naming the group and the
    conflicted paths, and leaves the worktree at its pre-refresh HEAD with no merge in
    progress.
  - Both re-entry paths refresh: a commit landed on the integration tip while the group
    was down is reachable from HEAD whether the worktree still existed or only the branch did.
  - A freshly cut group worktree contains a commit merged into integration immediately
    before that group started.

### U2. u2-failure-gate — a failed or interrupted group never lets an overlapping successor start silently

- **Goal**: on group termination, compute declared-file overlap with the remaining
  groups; resolve or escalate on `FAILED`, hold overlapping successors on `INTERRUPTED`,
  and default HITL on.
- **Files**: `orchestrator/execution/scheduler.py`, `orchestrator/execution/escalation.py`,
  `orchestrator/model.py`, `orchestrator/config.py`, `orchestrator/cli.py`,
  `tests/test_scheduler.py`, `tests/test_escalation.py`
- **Symbols**: `Scheduler`, `GroupState`, `TERMINAL_STATES`, `GroupRunState`,
  `_blocked_by_failure`, `_classify`, `EscalationBroker`, `EscalationPolicy`,
  `EscalationKind`, `HumanAction`, `EscalationConfig`, `IntegrationMerger`,
  `merge_group`, `is_dirty`
- **Depends-on**: u1-merge-integrity
- **Slice**: merge-integrity
- **Implements / Consumes**: implements `overlap-gate`, `resolve-routine`; consumes
  `merge-gate`
- **Verification**:
  - When a group ends failed and a later group declares at least one file in common,
    that later group does not start until the failed group is resolved or an operator
    decision is recorded.
  - With HITL on, the raised escalation names the failed group, the overlapping file
    paths, and the affected later groups, and `status` lists it as pending until answered.
  - With HITL on and an operator-resolved answer, successors are released only when the
    failed group's branch is an ancestor of the integration tip; when it is not, the run
    does not proceed silently.
  - With HITL off, the failed group's uncommitted worktree changes become a commit, its
    branch merges into integration, and its recorded state is `resolved` — never `completed`.
  - A resolve whose merge conflicts leaves the integration branch at its pre-merge SHA and
    stops the run with a message naming the group and the conflicted paths.
  - A failed group with neither commits nor uncommitted changes is recorded as having lost
    nothing, and the run continues.
  - A group ending interrupted is never resolved: its worktree keeps its uncommitted
    changes and a later `resume` re-enters it.
  - A group ending interrupted holds only the remaining groups sharing a declared file;
    groups with no overlap still run and merge.
  - With no config file and no flags, escalation is enabled.
  - The final outcome listing reports a resolved group distinctly from a completed one.

### U3. u3-typed-denial — a permission denial is typed, recoverable, and costs no rewrites

- **Goal**: `permission_denied` travels from the coder prompt through the report schema
  and review loop to an envelope classification, and the coder never improvises around a
  denial.
- **Files**: `orchestrator/model.py`, `orchestrator/prompts/coder.md`,
  `orchestrator/prompts/report_contract.md`, `orchestrator/execution/review.py`,
  `orchestrator/execution/scheduler.py`, `tests/test_review_loop.py`
- **Symbols**: `CoderReport`, `ReviewDeps`, `SurpriseBoard`, `GroupState`, `Scheduler`,
  `render_coder_prompt`, `SessionEntry`
- **Depends-on**: —
- **Slice**: denial
- **Implements / Consumes**: implements `denial-report`
- **Verification**:
  - A report with status `permission_denied` and a blank `denied_command` is rejected as
    invalid; the same report with a non-empty command validates.
  - A coder round returning `permission_denied` leaves the group interrupted, with the
    denied command recorded verbatim in the group's failure text.
  - That path consumes no rewrite: a group reporting `permission_denied` on its first
    round has the same rewrite count as one that never reported, and never reaches failed.
  - A group interrupted by a denial is re-entered by a plain `resume` in its existing
    worktree.
  - The rendered coder prompt states the three-identical-attempt budget and forbids
    alternate quoting, alternate spellings, shelling through another interpreter, and
    `subprocess.run` substitution for a denied command.
  - The report contract documents `permission_denied` and `denied_command` alongside the
    existing statuses, and the status attribute and JSON body must still agree.
  - A `blocked` report still routes to the existing escalate-then-rewrite path unchanged.

### U4. u4-granularity — an explicit granularity dial that never breaks a slice or the cap

- **Goal**: `--granularity {independent,balanced,monolithic}` relaxes the two
  `merge_small_groups` guards in order, with the property tests holding at every level.

  Read `docs/orchestrator-grouping.md` §"Prior art and known limits of the dial" first:
  both guards this unit relaxes are named algorithms, and two known limits of
  `louvain_resolution` explain shapes the flag cannot reach.

- **Files**: `orchestrator/grouping/partition.py`, `orchestrator/grouping/pipeline.py`,
  `orchestrator/config.py`, `orchestrator/cli.py`, `tests/test_partition.py`,
  `tests/test_grouping_fixtures.py`, `tests/fixtures/grouping/granularity-ladder.md` *(new, small)*

- **Symbols**: `merge_small_groups`, `PartitionConfig`, `OrchestratorConfig`,
  `load_config`, `compute_partition`, `run_grouping`, `_cmd_group`

- **Depends-on**: —

- **Slice**: granularity

- **Implements / Consumes**: implements `granularity-flag`

- **Verification**:

  - `group --granularity` accepts exactly `independent`, `balanced`, and `monolithic`,
    and exits non-zero on any other value.
  - Omitting the flag reproduces today's partition byte-for-byte on every fixture in the
    register.
  - On a fixture whose groups are chain-compatible but whose merge regresses the simulated
    makespan, `balanced` yields strictly fewer groups than `independent`, and `monolithic`
    yields no more groups than `balanced`.
  - At every level, no declared slice is split across groups, the group DAG is acyclic, and
    no group's summed work exceeds the budget cap.
  - Running the same plan twice at the same level produces identical group membership.
  - `[partition] granularity` in a config file has the same effect as the flag, and the flag
    wins when both are set.
  - The grouping-improvement plan groups at default settings without `--allow-oversized-slice`.

### U5. u5-scorecard — every partition is measured, attributable, and appended to a durable log

- **Goal**: a scorecard computed for every partition, provenance sufficient to attribute
  it, and one append-only row per `group` invocation.
- **Files**: `orchestrator/grouping/scorecard.py` *(new, medium)*,
  `orchestrator/grouping/trace.py`, `orchestrator/grouping/graphing.py`,
  `orchestrator/grouping/pipeline.py`, `orchestrator/cli.py`, `tests/test_grouping_trace.py`
- **Symbols**: `GroupingTrace`, `TraceRecorder`, `CodegraphClient`, `compute_partition`,
  `run_grouping`, `_cmd_group`, `_print_partition_report`
- **Depends-on**: —
- **Slice**: measurement
- **Implements / Consumes**: implements `scorecard`, `grouping-provenance`
- **Verification**:
  - `group --no-spec` prints group count, cross-group edge count, per-group work as
    min/mean/max fractions of the budget cap, critical-path length, modularity, and slice
    integrity as an explicit pass or fail.
  - The printed scorecard values equal those recorded in `grouping-trace.json` for the same
    invocation.
  - The trace records a timestamp, the plan path, a hash of the plan's content, the repo
    commit SHA, whether the worktree was dirty, and an index fingerprint.
  - Two invocations against the same plan, repo commit, and index produce the same index
    fingerprint; re-syncing the index after a source change produces a different one.
  - Each `group` invocation that produces a partition appends exactly one line to
    `.orchestrator/grouping-metrics.jsonl`, and re-running the same grouping name appends a
    second line rather than replacing the first.
  - Every appended line parses as JSON and carries both the scorecard and the provenance.
  - A grouping that fails before producing a partition appends no line.
  - **Golden partitions** *(added 2026-07-29)*: each fixture's partition is recorded as a
    committed baseline under `tests/fixtures/grouping/golden/`, and a test fails when a
    fixture's partition differs from its baseline, printing both. Regenerating is a single
    documented command, so a deliberate behaviour change lands as a **reviewable diff**
    rather than silently.
  - The golden test fails when a fixture's group *count* changes, when membership moves
    between groups, and when a group's summed work crosses the cap — the three drift
    directions ("grouped too much", "too little", "over budget") that today's byte-stability
    test cannot see, because it compares two runs of the same code rather than against a
    recorded baseline.
  - The baselines cover the symbol-bearing fixture too, so drift in the codegraph-derived
    edge layer is caught, not just in the plan-declared layer.

### U6. u6-fault-injection — the four real failures reproduce in the zero-token stub harness

- **Goal**: each of R14a–d fails without its fix and passes with it, at zero token cost.
- **Files**: `tests/fake_claude.py`, `tests/test_e2e_faults.py` *(new, large)*
- **Symbols**: `IntegrationMerger`, `Scheduler`, `GroupState`, `CoderReport`, `ReviewDeps`,
  `ManifestStore`
- **Depends-on**: u1-merge-integrity, u2-failure-gate, u3-typed-denial
- **Slice**: —
- **Implements / Consumes**: consumes `merge-gate`, `branch-refresh`, `overlap-gate`,
  `resolve-routine`, `denial-report`
- **Verification**:
  - A scripted coder that writes files and omits the commit step produces a branch with no
    commits; the merge is refused and the group does not reach completed.
  - A group interrupted while a sibling merges, then resumed, reaches a HEAD from which the
    sibling's commit is reachable — or the run stops with the refresh error naming it.
  - A group that fails while a later group declares an overlapping file raises the
    escalation with HITL on, and holds the overlapping group with HITL off.
  - A scripted coder reporting `permission_denied` leaves the group interrupted, resumable,
    and with its rewrite budget untouched.
  - All four scenarios run with no network access and no real `claude` binary.

### U7. u7-durability — calibrated thresholds and state that survives a restart

- **Goal**: the breaker default matches measured reality, and cross-group surprises,
  first-round sessions, and reviewer context all survive.
- **Files**: `orchestrator/config.py`, `orchestrator/execution/review.py`,
  `orchestrator/execution/manifest.py`, `orchestrator/model.py`,
  `tests/test_review_loop.py`, `tests/test_sessions.py`
- **Symbols**: `BreakerConfig`, `SurpriseBoard`, `ReviewDeps`, `SessionEntry`,
  `RunManifest`, `ManifestStore`, `load_config`
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - With no config file present, the breaker's context token limit is 200000.
  - A surprise marked for a group that has not yet run survives a restart: after reloading
    from the run directory, that group still consumes it before launching.
  - A group interrupted during its very first round has a recorded coder session entry, and
    a later resume re-enters that session instead of forking a fresh one.
  - After a reviewer round, the reviewer's manifest entry carries a non-zero context token
    count.
  - Persisting and reloading the surprise board preserves which groups a surprise names and
    never re-delivers one already consumed.

### U8. u8-operator-surface — what the operator reads is true

- **Goal**: the DAG prints upstream, a completed group shows no stale failure, and an
  intensity override announces its cost.
- **Files**: `orchestrator/cli.py`, `orchestrator/execution/scheduler.py`,
  `tests/test_cli.py`
- **Symbols**: `_print_partition_report`, `_cmd_status`, `_cmd_group`, `Scheduler`,
  `GroupingTrace`

> **Descoped 2026-07-29 — done ahead of the run.** The configuration reference
> (`docs/orchestrator-grouping-config.md`) was written by hand outside this plan, verified
> complete against `orchestrator/config.py` (8 models, 47 fields, none missing), and
> `docs/orchestrator-grouping.md` was rewritten alongside it. The two verification bullets
> that covered it are struck below; nothing else in U8 changes.

- **Depends-on**: —
- **Slice**: operator
- **Implements / Consumes**: consumes `granularity-flag`, `scorecard`
- **Verification**:
  - `group --no-spec` lists, for each group, the groups it depends on — upstream, not
    downstream — matching the edges recorded in the trace's DAG.
  - `status` prints no failure line for a group whose recorded state is completed, including
    a group that failed on an earlier attempt and later succeeded.
  - Passing `--review-intensity` prints a warning naming how many groups it changes and the
    reviewer sessions that implies; omitting it prints no such warning and leaves every
    group's intensity exactly as recorded in `groups.json`.
  - ~~The configuration reference documents every field of every config model…~~
    **(descoped — delivered 2026-07-29)**
  - ~~Every config field named in the reference exists in `orchestrator/config.py`…~~
    **(descoped — verified: 47/47 fields covered)**
  - U4's granularity levels, once they exist, are added to the existing
    `docs/orchestrator-grouping-config.md` under `[partition]` — the reference itself is no
    longer this unit's deliverable.

### U9. u9-conflict-exclusion — two groups that edit the same file never run at once *(added 2026-07-29)*

- **Goal**: file overlap becomes a first-class **symmetric mutual-exclusion** relation
  the scheduler enforces at admission, independent of the group DAG and of group health.

  This is the other half of the plan's own defect 3. U2 reacts *after* a group
  terminates badly (`FAILED` → resolve, `INTERRUPTED` → hold overlapping successors);
  nothing stops two *healthy* groups sharing `cli.py` from running concurrently and
  colliding at merge. In `r20260726-grouping`, g7 ran while g5 was in flight because it
  was DAG-unblocked — the DAG is logical, not file-based. Either order is safe
  (`workspace_for` cuts each worktree from `merger.tip()` at its ready→running
  transition), so this is exclusion, never a new directed edge — see the Decisions
  entries "Conflict is mutual exclusion, not ordering" and "File-level conflict
  granularity, knowingly".

  Only observable at `--concurrency > 1`; the serial default already excludes
  everything. That is exactly why it must be a scheduler invariant rather than a
  property of the default config.

- **Files**: `orchestrator/execution/scheduler.py`, `orchestrator/model.py`,
  `orchestrator/config.py`, `orchestrator/cli.py`, `tests/test_scheduler.py`,
  `tests/test_e2e_stub.py`

- **Symbols**: `Scheduler`, `GroupState`, `GroupRunState`, `_blocked_by_failure`,
  `Group`, `ExecutionConfig`, `GroupingResult`

- **Depends-on**: u2-failure-gate

- **Slice**: —

- **Implements / Consumes**: consumes `overlap-gate`

- **Verification**:

  - At `--concurrency 4`, two groups declaring at least one file in common are never
    both in `running` at the same time, on a grouping where the DAG leaves them
    unordered.
  - Groups declaring no file in common still run concurrently at `--concurrency 4` — the
    exclusion holds back only real overlaps, and the run's wall-clock ordering shows it.
  - Whichever of two overlapping groups is admitted first, the run completes and every
    group's work reaches the integration branch — no ordering between them is required
    or recorded.
  - The relation is symmetric: no group DAG edge, no `Group.dependencies` entry, and no
    `grouping-trace.json` DAG edge is created by file overlap alone.
  - A group held for overlap is reported distinctly from one blocked by a DAG dependency
    and from one held by U2's failure gate, and `status` names the overlapping file(s)
    and the group holding the lock.
  - Overlap is computed from the union of existing and prospective files, so two groups
    that both plan to create the same not-yet-existing file are excluded from each other.
  - At the default `concurrency = 1` the scheduler's admission decisions are byte-identical
    to today's — this unit changes nothing about a serial run.
  - The exclusion survives a `resume`: a run interrupted with one of two overlapping
    groups in flight does not admit the other before the first is re-entered and finished.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-merge-integrity
    description: Refuse a merge from a group branch with zero commits, and refresh a re-entered group branch onto the current integration tip on both worktree paths
    slice: merge-integrity
    files:
      - orchestrator/execution/merge.py
      - orchestrator/execution/worktrees.py
      - orchestrator/cli.py
      - tests/test_merge.py
      - tests/test_sessions.py
    symbols:
      - merge_group
      - IntegrationMerger
      - MergeError
      - create_worktree
      - worktree_path
      - _registered_branch
      - is_dirty
    depends_on: []
    implements: ["merge-gate", "branch-refresh"]
    consumes: []
  - task_id: u2-failure-gate
    description: Gate on declared-file overlap when a group terminates, resolve a failed group by committing and merging its stranded work, and default HITL escalation on
    slice: merge-integrity
    files:
      - orchestrator/execution/scheduler.py
      - orchestrator/execution/escalation.py
      - orchestrator/model.py
      - orchestrator/config.py
      - orchestrator/cli.py
      - tests/test_scheduler.py
      - tests/test_escalation.py
    symbols:
      - Scheduler
      - GroupState
      - TERMINAL_STATES
      - GroupRunState
      - _blocked_by_failure
      - _classify
      - EscalationBroker
      - EscalationPolicy
      - EscalationKind
      - HumanAction
      - EscalationConfig
      - IntegrationMerger
      - merge_group
      - is_dirty
    depends_on: [u1-merge-integrity]
    implements: ["overlap-gate", "resolve-routine"]
    consumes: ["merge-gate"]
  - task_id: u3-typed-denial
    description: Carry a permission denial as a typed coder report status from prompt to envelope classification so it interrupts without consuming rewrites
    slice: denial
    files:
      - orchestrator/model.py
      - orchestrator/prompts/coder.md
      - orchestrator/prompts/report_contract.md
      - orchestrator/execution/review.py
      - orchestrator/execution/scheduler.py
      - tests/test_review_loop.py
    symbols:
      - CoderReport
      - ReviewDeps
      - SurpriseBoard
      - GroupState
      - Scheduler
      - render_coder_prompt
      - SessionEntry
    depends_on: []
    implements: ["denial-report"]
    consumes: []
  - task_id: u4-granularity
    description: Add an explicit granularity dial relaxing the two small-group merge guards in order, with slice must-link and the budget cap hard at every level
    slice: granularity
    files:
      - orchestrator/grouping/partition.py
      - orchestrator/grouping/pipeline.py
      - orchestrator/config.py
      - orchestrator/cli.py
      - tests/test_partition.py
      - tests/test_grouping_fixtures.py
      - tests/fixtures/grouping/granularity-ladder.md
    size_hints:
      tests/fixtures/grouping/granularity-ladder.md: small
    symbols:
      - merge_small_groups
      - PartitionConfig
      - OrchestratorConfig
      - load_config
      - compute_partition
      - run_grouping
      - _cmd_group
    depends_on: []
    implements: ["granularity-flag"]
    consumes: []
  - task_id: u5-scorecard
    description: Compute a quality scorecard for every partition, add provenance to the grouping trace, and append one row per invocation to a durable metrics log
    slice: measurement
    files:
      - orchestrator/grouping/scorecard.py
      - orchestrator/grouping/trace.py
      - orchestrator/grouping/graphing.py
      - orchestrator/grouping/pipeline.py
      - orchestrator/cli.py
      - tests/test_grouping_trace.py
    size_hints:
      orchestrator/grouping/scorecard.py: medium
    symbols:
      - GroupingTrace
      - TraceRecorder
      - CodegraphClient
      - compute_partition
      - run_grouping
      - _cmd_group
      - _print_partition_report
    depends_on: []
    implements: ["scorecard", "grouping-provenance"]
    consumes: []
  - task_id: u6-fault-injection
    description: Reproduce the four real run failures as scenarios in the zero-token stub harness so each regression is caught without tokens
    slice: null
    files:
      - tests/fake_claude.py
      - tests/test_e2e_faults.py
    size_hints:
      tests/test_e2e_faults.py: large
    symbols:
      - IntegrationMerger
      - Scheduler
      - GroupState
      - CoderReport
      - ReviewDeps
      - ManifestStore
    depends_on: [u1-merge-integrity, u2-failure-gate, u3-typed-denial]
    implements: []
    consumes: ["merge-gate", "branch-refresh", "overlap-gate", "resolve-routine", "denial-report"]
  - task_id: u7-durability
    description: Calibrate the breaker default to measured reality and make cross-group surprises, first-round sessions, and reviewer context survive a restart
    slice: null
    files:
      - orchestrator/config.py
      - orchestrator/execution/review.py
      - orchestrator/execution/manifest.py
      - orchestrator/model.py
      - tests/test_review_loop.py
      - tests/test_sessions.py
    symbols:
      - BreakerConfig
      - SurpriseBoard
      - ReviewDeps
      - SessionEntry
      - RunManifest
      - ManifestStore
      - load_config
    depends_on: []
    implements: []
    consumes: []
  - task_id: u8-operator-surface
    description: Print the DAG upstream, clear a stale failure on success, and warn when an intensity override changes every group
    slice: operator
    files:
      - orchestrator/cli.py
      - orchestrator/execution/scheduler.py
      - tests/test_cli.py
    symbols:
      - _print_partition_report
      - _cmd_status
      - _cmd_group
      - Scheduler
      - GroupingTrace
    depends_on: []
    implements: []
    consumes: ["granularity-flag", "scorecard"]
  - task_id: u9-conflict-exclusion
    description: File overlap becomes a symmetric mutual-exclusion relation the scheduler enforces at admission, so two groups editing the same file never run concurrently
    files:
      - orchestrator/execution/scheduler.py
      - orchestrator/model.py
      - orchestrator/config.py
      - orchestrator/cli.py
      - tests/test_scheduler.py
      - tests/test_e2e_stub.py
    symbols:
      - Scheduler
      - GroupState
      - GroupRunState
      - _blocked_by_failure
      - Group
      - ExecutionConfig
      - GroupingResult
    depends_on: [u2-failure-gate]
    implements: []
    consumes: ["overlap-gate"]
```
