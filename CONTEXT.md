# smart-mcps

Domain glossary for smart-mcps. Opinionated, project-specific terms only.

## Language

**Observatory**:
The local front-end for the orchestrator — a web app that renders an orchestration
run's state from disk and writes back exactly one kind of input, escalation answers.
It observes and answers; it does not launch, abort, or otherwise drive runs.
_Avoid_: dashboard (too generic), control panel (implies launch/abort, which is out of scope)

**Project Registry**:
A single config file listing the target repos the Observatory can switch between,
each as a name plus a repo path. The source of the Observatory's project switcher.
_Avoid_: front-matter (the original loose term for this idea)

**Grouping**:
A named, self-contained partition of one plan into execution groups, stored at
`<repo>/.orchestrator/groupings/<name>/` (groups.json, base-context.md,
grouping-trace.json). Written by `group --name <name>`, selected by
`run --grouping <name>`. Several may coexist — two plans, or two alternative
partitions of one plan (ADR 0003).
_Avoid_: the groups file (there is no single one), the plan's grouping (a plan may have several)

**Run Directory**:
The per-run artifact tree at `<repo>/.orchestrator/runs/<run_id>/` (state.json,
manifest.json, groups.json, grouping-trace.json, logs/run.log, escalations/,
groups/). The Observatory's entire read and write surface for a run — there is
no other channel. Its copies of the grouping artifacts are a per-run snapshot of
the Grouping the run was launched from, taken at run start so the DAG survives
later re-planning (ADR 0002, ADR 0003).

**Run Snapshot**:
The single composed payload the Observatory serves for a run — state.json's group
states and generations, manifest.json's groups→sessions join, and the run's DAG,
merged into one JSON body the SPA renders without further fetches. A read model,
never a stored artifact.
_Avoid_: run state (that is state.json specifically, one of its three inputs)

**Envelope Failure**:
A group failure caused by the harness, not the work — the `claude` CLI process or
API layer dying (usage limit, aborted API call, nonzero exit with empty stderr).
Typed as `SessionError` in code — except `ReportError`, which despite its type is
a work failure (see below). Recoverable by definition: the work itself was
never judged.
_Avoid_: crash (too vague), transient error (some envelope failures last hours)

**Permission Denial**:
A worker's tool call refused by the harness's permission layer, not by the
orchestrator. A kind of Envelope Failure — the work was never judged, and the
same command succeeds once the grant exists — so it is recoverable, never a
Work Failure. A coder that meets one reports it; it never improvises around it.
_Avoid_: blocked (that is the coder's judgement that the work itself cannot proceed)

**Work Failure**:
A group failure decided by an agent or a bound. A **closed set** of four routes —
rewrite cap exhausted, generation cap exhausted, operator skip, and `ReportError`
(a coder that burned its report-format nudges was judged unable to report, not
killed by the harness). Nothing else reaches it: an exception the orchestrator
does not recognise is by definition not a judgement about the work, so it
classifies Interrupted instead. Terminal by design; a human must look at it, via
[[Retry]].

**Interrupted**:
The non-terminal outcome recorded when a group dies of anything that is not one
of the four Work Failure routes — every Envelope Failure, and every exception the
orchestrator does not recognise. The default classification, not the exceptional
one. A plain `resume` re-enters interrupted groups automatically; dependents wait
rather than strand.
_Avoid_: failed (that is the terminal, work-failure outcome)

**Re-entry**:
A `resume` picking an interrupted group back up in its existing worktree
(commits and dirty WIP intact). Two modes, always logged: warm resume, or a
fresh-generation fork from base.

**Warm Resume**:
Continuing an interrupted coder session by its session id, preserving its
conversation — the same mechanism `changes_required` rounds already use.
_Avoid_: respawn (that is the fork-fresh path)

**Resolve**:
The orchestrator's recovery of a group that ended in a Work Failure: commit
whatever its worktree still holds uncommitted, then merge the branch — through
the same [[Preflight]] and conflict ladder every other merge passes. Reachable
autonomously (HITL off) or on operator request (HITL on). Possible only because
the orchestrator shells git itself, outside the worker's permission sandbox — the
same reason an operator can commit by hand where the coder was denied. Never
applied to an Interrupted group, whose work is unfinished by definition and whose
Re-entry is designed to finish it.
_Avoid_: recover (the operator's manual version), retry (no agent runs again)

**Resolved**:
The terminal outcome of a group whose work was merged by a Resolve — banked, but
never reviewed. Deliberately distinct from completed: a group that reached
integration without a verdict must not claim one (ADR 0004).
_Avoid_: completed (that asserts a passed review), failed (its work is merged)

**Granularity**:
The explicit dial on how aggressively the partitioner merges small groups —
`independent` (both merge guards on), `balanced` (makespan no-regression guard
relaxed), `monolithic` (both relaxed). Orthogonal to concurrency: chain
compatibility keeps groups *disjoint*, and file overlap between groups is a
correctness hazard a serial run still wants avoided. Slice must-link and the
budget cap stay hard at every level.
_Avoid_: parallel (naming the default for parallelism concedes exactly that point)

**Slice**:
A plan-declared set of tasks that must execute as one group — the task map's
must-link. An output invariant of grouping: a slice lands whole in exactly one
group, or grouping fails loudly naming it. Not necessarily a vertical — any
task set the planner binds together.
_Avoid_: soft preference (pre-2026-07-22 semantics, where the budget splitter could dissolve it)

**Grouping Trace**:
The versioned sidecar artifact (`grouping-trace.json`) recording every partition
stage's output and each decision with its quantitative context — the grouping
engine's explanation of itself, and the contract a front-end renders. A record,
never an input: reading it back never influences grouping.
_Avoid_: explain log (it is structured data, not log lines)

**Grouping Scorecard**:
The measured quality of one partition's *outcome* — group count, cross-group
edge count, work spread against the cap, critical path, modularity, slice
integrity — carried with the provenance needed to attribute it (plan hash, repo
commit, codegraph index fingerprint, resolved config). A Grouping Trace explains
how the partition was decided; a scorecard says how good the result is.
_Avoid_: grouping metrics (too generic), grouping stats (implies incidental counters)

**Size Hint**:
The optional size class (`small`|`medium`|`large`) on a task map's prospective
file, pricing an unwritten file for the estimator. Planner intent is the only
real size signal for files that do not exist yet.

**Lifecycle Event**:
An orchestrator-emitted control-plane line in `run.log`: round boundaries,
verdicts, generation forks/retirements, re-entry mode, merges, escalations.
Explicitly excludes coder activity — that is data-plane, read from session
transcripts by the Observatory, never pushed by the orchestrator.

**Preflight**:
The mechanical, LLM-free gate every branch passes before it merges into the
integration branch: the worktree is clean, and the repo's configured check
command exits zero in it. Asks the same two questions of an approved branch and
of a Resolve's stranded work, and is the only gate a `self_verify` group has at
all. Distinct from a Verification Item, which is prose for the reviewer to judge
and never executed.
_Avoid_: pre-merge check (too generic), validation (overloaded with the
reviewer's judgement)

**Stranded Work**:
Uncommitted changes left in a group's worktree by a process that died before the
group reached any outcome — the orchestrator's own crash included. Never
discarded; committed to the group's own branch under a `recover(...)` or
`resolve(...)` subject, and merged only if it passes [[Preflight]].
_Avoid_: WIP (says nothing about who abandoned it), lost work (it is recoverable
by construction)

**Retry**:
The operator's deliberate re-entry of a group that reached a terminal Work
Failure — resets it to pending, keeps its branch, worktree and warm session, and
lets the normal path pick it up. Distinct from `resume`, which re-enters
Interrupted groups automatically and never touches a Work Failure.
_Avoid_: resume (that is the automatic, non-terminal path), rerun (implies
starting the group over cold)
