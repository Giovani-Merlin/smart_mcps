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

**Run Directory**:
The per-run artifact tree at `<repo>/.orchestrator/runs/<run_id>/` (state.json,
manifest.json, groups.json, logs/run.log, escalations/, groups/). The Observatory's
entire read and write surface for a run — there is no other channel. Its
`groups.json` is a per-run snapshot of the shared `.orchestrator/groups.json`,
taken at run start so the DAG survives later re-planning (ADR 0002).

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

**Work Failure**:
A group failure decided by an agent or a bound — coder blocked/failed, reviewer
abort, operator skip, generation/rewrite caps exhausted. Includes `ReportError`:
a coder that exhausted its report-format nudges (2 warm corrective resumes) was
judged unable to report, not killed by the harness. Terminal by design; a
human must look at it.

**Interrupted**:
The non-terminal outcome recorded when a group dies of an envelope failure. A
plain `resume` re-enters interrupted groups automatically; dependents wait
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

**Size Hint**:
The optional size class (`small`|`medium`|`large`) on a task map's prospective
file, pricing an unwritten file for the estimator. Planner intent is the only
real size signal for files that do not exist yet.

**Lifecycle Event**:
An orchestrator-emitted control-plane line in `run.log`: round boundaries,
verdicts, generation forks/retirements, re-entry mode, merges, escalations.
Explicitly excludes coder activity — that is data-plane, read from session
transcripts by the Observatory, never pushed by the orchestrator.
