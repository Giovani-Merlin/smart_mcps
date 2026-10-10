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
\[[Retry]\].

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
the same \[[Preflight]\] and conflict ladder every other merge passes. Reachable
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

**Spec Assembly**:
The deterministic construction of a group's name, summary, spec, and
verification from the plan's own unit sections plus graph facts (depends-on
order, implements/consumes, slices) — zero LLM calls, regenerated whenever the
partition changes. The default source of group specs.
_Avoid_: speccer (that is the opt-in LLM overlay stage), packer (loose spoken
alias for the same stage)

**Advisory Report**:
The deterministic artifact `group --advise` emits from one cached task graph:
the partition at every granularity level side by side, plus plan-cohesion
diagnostics (weak connectivity → "this reads as N plans"; layering → "this
reads as serial phases"). It advises a human or skill; it never decides.
_Avoid_: recommendation (the report names seams, someone else chooses)

**Plan Seam**:
A boundary the Advisory Report detects along which one plan could split into
several — either a weakly-connected task set (an independent sub-plan) or a
phase boundary in a mostly-serial layering. Splitting along a seam is a
mechanical move of existing unit sections, never a rewrite.

**Deepening**:
The optional interactive pass (`/orchestrator-deepen`) that enriches a plan's
per-unit specs — edge cases, sharpened verification — by exploring the codebase
with read-only subagents and then grilling the human. Writes into the plan doc
itself, so Spec Assembly carries it into groups for free.
_Avoid_: enrichment overlay (deepening edits the plan, not groups.json)

**Plan Digest**:
The deterministic condensation of a plan for shared worker context: the plan's
preamble plus every unit's tagged one-line summary, parsed from the plan doc —
never LLM-summarized at group time. Full unit sections travel only in the
owning group's spec; cross-group needs are served contracts-only.
_Avoid_: plan summary (implies an LLM wrote it), shared plan (the full doc is
exactly what workers no longer receive)

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
and never executed. A failure is never treated as one undifferentiated thing —
see \[[Preflight Kind]\] and \[[Preflight Baseline]\].
_Avoid_: pre-merge check (too generic), validation (overloaded with the
reviewer's judgement)

**Preflight Kind**:
The classification a Preflight failure is given *before* anyone is blamed for
it: `env` (the check command never actually ran a test — a dirty worktree, a
collection-phase import error, or an interrupted/internal/usage pytest exit),
`timeout` (the check command hung), or `regression` (tests ran and genuinely
failed). Only `regression` — and only once it clears the \[\[Preflight
Baseline\]\] — spends a group's rewrite/generation budget; `env` and `timeout`
fail the group fast with the diagnosis attached instead of burning a rewrite on
a cause the diff cannot fix.
_Avoid_: exit code (that is the raw signal a kind is classified from, not the
classification itself)

**Preflight Baseline**:
The check command's per-test outcome set, captured once on the launch branch at
run start and persisted as `preflight-baseline.json`. Every later preflight
failure's failing-test set is compared against it: `new_failures` (attributable,
keeps the rewrite path), `pre_existing` (every failing test was already red on
the launch branch — routed the same as an `env` \[[Preflight Kind]\], never
charged to the diff), or `no_baseline` (none could be captured — degrades to
"cannot attribute," never to a false attribution).
_Avoid_: baseline (alone, without "preflight" — this repo has more than one kind
of recorded-before-the-fact comparison)

**Informational Surprise**:
A `Surprise` whose `kind` is `"informational"` — a broadcast fact (a changed test
baseline, a pre-existing red suite) folded into the next generation's briefing
without incrementing `rewrites` and without triggering a speccer call. Every
other surprise kind still spends a rewrite when consumed; this kind exists
specifically so a fact worth telling every affected group does not drain their
rewrite budget just for having been told.
_Avoid_: notification (implies a UI concept; this is a briefing mechanism inside
the spec-rewrite pipeline)

**Auth Pause**:
The armed-and-self-releasing pause a run enters when an expired OAuth credential
survives both cheaper rungs of the auth-refresh ladder — the local `expiresAt`
check and the unconfined orchestrator's own refresh attempt. Reuses the same
"arm, poll, self-release" gate machinery a usage-limit pause uses, with a health
probe in place of a fixed reset deadline. Distinct from a usage-limit pause: an
auth pause has no reset-time prose to parse, only "healthy again."
_Avoid_: 401 (that is the wire signal that triggers rung (a), not the pause
state itself), re-auth (implies a human must intervene, which is true only once
both cheaper rungs have already failed)

**Stranded Work**:
Uncommitted changes left in a group's worktree by a process that died before the
group reached any outcome — the orchestrator's own crash included. Never
discarded; committed to the group's own branch under a `recover(...)` or
`resolve(...)` subject, and merged only if it passes \[[Preflight]\].
_Avoid_: WIP (says nothing about who abandoned it), lost work (it is recoverable
by construction)

**Retry**:
The operator's deliberate re-entry of a group that reached a terminal Work
Failure — resets it to pending, keeps its branch, worktree and warm session, and
lets the normal path pick it up. Distinct from `resume`, which re-enters
Interrupted groups automatically and never touches a Work Failure.
_Avoid_: resume (that is the automatic, non-terminal path), rerun (implies
starting the group over cold)

**Run Bundle**:
The versioned, self-contained package export of one finished run
(`<run_dir>/ingest/`: an `ingest.json` manifest plus per-session parsed-event
files, written by `orchestrate export`) — the entire boundary between an agent
framework and Infinity Skills. Harness-agnostic by design: the exporter parses
its own harness's raw transcripts into neutral events, so no consumer ever
learns the Claude Code jsonl format; and rich by design: it keeps the
framework's own shapes (groups, generations, verdicts, surprises), normalized
to the corpus by the Framework Adapter, not the exporter.
_Avoid_: ingest contract (names the file's role but not the artifact), universal
schema (rejected — one generic schema per all frameworks loses framework structure)

**Framework Adapter**:
The mapping-only ingester class inside Infinity Skills that turns one framework's
Run Bundle into canonical corpus rows. Registered with a manifest (framework name,
supported schema_version range), validated by a golden fixture contract test, and
forbidden from interpreting — it maps fields, never re-derives behaviour.
_Avoid_: plugin (too generic), pipeline (the pipeline is the fixed downstream
stages the adapter feeds)

**Base-Context Strip**:
The export-time omission of the shared base-context prefix from each worker's
parsed event stream, recorded per session and referencing one base-context blob
keyed by its sha256. Dedup, never erasure: every worker's replay still opens
with a reference to the blob it actually received. The post-ADR-0007 successor
to uuid-based fork dedup: fresh workers repeat the base context with new uuids,
so only the exporter — which knows the exact bytes — can dedup it.

**Failure Policy**:
What the run does about *other* groups once one has ended without landing its
work — overlap (the default: admit anything not sharing a declared file) or `halt`
(admit nothing further; opt-in with `--on-failure halt`). Both \[[Work Failure]\]
and Interrupted trigger it, because both leave the same hole in the integration
tip. In-flight groups are never cancelled to effect a halt; they run to their own
outcome first.
_Avoid_: fail-fast (says nothing about the groups already running), abort (that
is the operator stopping the whole run)

**Sign of Life**:
Evidence that a worker child is doing something, read by the orchestrator every
heartbeat tick: a stream-json event of any type from the child, a live process
whose parent is the child (a tool call in flight), or the child's own CPU ticks
advancing. Any one suffices; none is a judgement about the work's quality or
speed. After a `result` event CPU ticks are not evidence — a child that finished
its turn and is still alive is idle, not working.
_Avoid_: heartbeat (that is the driver's own tick, which advances whether or not
the child does anything), progress (implies the work is nearer done)

**Liveness Window**:
The maximum age of the freshest Sign of Life before a child is reported Not
Live — a cap on the *signal's* freshness, never on the round or the work, which
carry no wall-clock limit (R7). Default ten minutes, config-overridable.
_Avoid_: timeout (implies the work is cut off), round limit, stall threshold

**Not Live**:
The reported condition of a child with no Sign of Life for a whole Liveness
Window. A fact the orchestrator writes and `status`, the run log and the
Observatory show with its evidence; it changes group state only in one case —
after a detected machine suspend, when the driver kills the child and warm-resumes
it in-process.
_Avoid_: stalled, wedged, dead, hung (all four were used interchangeably for
"alive but silent", "silent but working", and "process gone")

**Operator Decision**:
A human answer to a `coder_question` escalation, binding by default: it amends
the group's spec for every later coder, handoff, reviewer and speccer prompt of
that group, derived at prompt time from the escalation record itself — never a
second file. Work that follows it is never "self-invented"; work that
contradicts it is `changes_required`. An answer given with `--guidance` is
advice for the warm session only and does not bind.
_Avoid_: operator note (that is the one-shot `retry` note, cleared after one
prompt), guidance (the explicit non-binding opt-out)

**Suspend Cure**:
The one automatic recovery the orchestrator performs on a Not Live child: only
after a detected machine suspend, and only when the child has shown no Sign of
Life since the wake for a whole Liveness Window, the driver kills the child's
process group and warm-resumes the same session in-process. Counted per group and
generation, capped, and never a Re-entry.
_Avoid_: watchdog kill (implies any silence can trigger it), restart (the
session and worktree are kept)

**Unit Recipe**:
The named bundle that determines how one plan unit is executed — its prompt,
tool allowlist, completion contract, reviewer prompt, merge behaviour, pricing
function and handoff prompt. Declared per unit in the task map
(`recipe: research`); a unit declaring none runs `code`, today's coder-and-
reviewer machine, unchanged. v1 of the registry (`orchestrator/recipes/`)
ships exactly two entries, `code` and `run`; the next increment (brainstorm
2026-09-27) adds `research`, `evaluate` and `optimize`; `synthesize` is
deferred until a pipeline needs an N→1 reduce.
Always written "Unit Recipe" in prose, never bare
"recipe" — for the same reason \[[Preflight Kind]\] is never bare "kind".
_Avoid_: kind (taken by \[[Preflight Kind]\]), machine (taken by machine
suspend/wake, which \[[Suspend Cure]\] depends on), role (taken by SessionRole:
base/coder/reviewer), executor (taken by the scheduler's Executor seam), agent
type (a recipe is a category of work, not a category of agent)

**Run Child**:
The detached process a `run` \[[Unit Recipe]\] unit's declared command executes
in — launched by the orchestrator itself, not by a worker, under the worker
Landlock profile plus the unit's declared extra write paths, and re-adopted
(not killed) when a resumed run finds it still alive (ADR 0011).
_Avoid_: worker (a worker is a `claude` session), job (says nothing about
ownership across a crash)

**Artifact Manifest**:
The run-level index of every declared output a run produced — one entry per
artifact carrying its id, path, sha256, producing unit, declared schema,
bounded summary and status. Lives in the \[[Run Directory]\] and rides the
\[[Run Bundle]\] as an additive key. It is what a downstream unit's prompt
receives *instead of* the payload: the entry, never the file body, which the
unit opens only when it needs the detail.
_Avoid_: artifact store (that is `data_dirs` — the bytes; this is the index
over them), findings index (research is only one of its producers)

**Findings Artifact**:
The document a `research` \[[Unit Recipe]\] unit commits under `docs/research/`
and registers in the \[[Artifact Manifest]\]: a list of findings, each carrying at
least one source reference and a confidence, plus an optional \[[Spec Refinement]\].
Its summary is the manifest entry a downstream unit reads.
_Avoid_: research report (prose with no schema), answers file (the research
skills' `research_answers.md` is an input format, not this artifact)

**Spec Refinement**:
A bounded edit to the spec of an existing downstream unit that a document
unit's artifact may carry — applied through the existing spec-rewrite path to
the unit named in the plan's `depends_on`, and never anything else. Refining a
unit is allowed; creating, splitting or removing one is not (ADR 0010).
_Avoid_: replan (changes the graph), recommendation (prose nobody applies)

**KPI Contract**:
The block on an `evaluate` or `optimize` unit naming the one measurement key
that is the objective, its direction, the minimum effect worth keeping, the
guard metrics that may not worsen, and the \[[Evaluation Harness]\] paths. Read
off the command's measurements JSON; the orchestrator never computes a KPI
itself.
_Avoid_: metric (any measurement is one; the KPI is the one being optimised),
objective (overloaded by the plan's Objective section)

**Evaluation Harness**:
The set of paths — scoring script, held-out inputs, seeds, budget constants and
the lines that enforce them — that a \[[KPI Contract]\] declares protected. Built
or named by a human-planned unit before any loop, excluded from the optimizer's
mutable files, and hash-checked by the evaluate child before every scoring run.
_Avoid_: eval script (only one of the protected paths), benchmark (implies a
public suite)

**Champion**:
The commit an `optimize` group currently holds as best — the baseline until a
candidate is kept. Every candidate is scored against it and every discard resets
the worktree to it; it is what merges when the loop ends.
_Avoid_: best-so-far (fine in prose, but the ledger column is `champion`),
HEAD (the coder's HEAD may be a discarded candidate)

**Attempt Ledger**:
The per-group, append-only record of every optimize attempt: candidate commit,
KPI and guard values, delta to the \[[Champion]\], noise floor at the time,
outcome, harness hash and one line of why. Injected into every round's prompt
so the coder knows what was tried, and registered as the group's artifact when
the loop ends with no keep.
_Avoid_: results.tsv (autoresearch's untracked, unread file — the anti-pattern),
history (the transcript is also history)

**Keep-or-Revert**:
The orchestrator's accept rule for an optimize candidate, decided from numbers
alone: `keep`, `promising` (improved, awaiting one confirmation run),
`inconclusive` (within the noise floor), `discard` (regressed or a guard
worsened) or `crash`. The coder proposes; the orchestrator decides and moves the
\[[Champion]\]. The single deliberate divergence from autoresearch, where the
agent decides.
_Avoid_: accept/reject (two-valued; hides `inconclusive` and `promising`)

**Driver Item Record**:
The run's own memory of which `Run (driver):` verification items the run driver
has executed, kept in `groups/<gid>/driver-items.json` and written by the
`driver-item` subcommand. The scheduler holds dependents of a group whose items
are unrecorded, and auto-finish waits on the same record.
_Avoid_: driver log (a log is not read back), sign-off (nobody approves; the
record is evidence)

**Data Step**:
A driver command that writes the shared data layer (`[workspace] data_dirs`),
run through `data-step` so it holds the run's `data-step.lock` and no merge gate
reads the data while it writes. A long one holds every merge for its duration.
_Avoid_: ingest (one kind of data step), driver item (a verification, not a write)
