# Orchestrator lifecycle

One pass through the pipeline, in order. Each stage names its driver (a skill
or a `smart-mcps-orchestrate` subcommand) and, where relevant, the ADR that
explains why it works the way it does. All subcommands below appear in
`smart-mcps-orchestrate --help`.

## 1. Brainstorm

**Driver:** `/orchestrator-brainstorm` (skill).
Grills the human from a vague idea to a requirements document with stable
`R-`ids: purpose, constraints, success criteria, scope boundaries, failure
modes, non-goals. No code, no plan yet.

## 2. Plan

**Driver:** `/orchestrator-plan` (skill).
Turns the brainstorm doc (or a direct feature description) into an
orchestrator-ready plan with an embedded task map, grounded in codegraph and
checked against `smart-mcps-orchestrate plan-check`. The plan's unit sections
are the only LLM-authored spec content in the run — see Group, below.

## 3. Deepen

**Driver:** `/orchestrator-deepen` (skill).
Grills the human with codegraph-grounded, EVPI-ranked edge-case questions per
group and writes answers back into the plan as per-unit edge cases,
non-goals, and Run:/Pass: verification items. Never touches the task map or
unit ids.

## 4. Split

**Driver:** `smart-mcps-orchestrate split`.
Mechanically splits one plan document into several by moving unit sections
and task-map entries verbatim, at an advisory seam or an explicit assignment
(`orchestrator/grouping/plan_edit.py`). No LLM call — pure text surgery.

## 5. Group

**Driver:** `smart-mcps-orchestrate group <plan>`.
Partitions the plan's units into groups (a DAG of worktree-sized chunks) using
codegraph facts (symbols, calls, impact) and writes `.orchestrator/groupings/<name>/`.
Group specs are assembled **deterministically** from the plan's unit sections —
the grouping-time speccer LLM call was deleted (`adr/0006`); the planner from
stage 2 already *is* the speccer. `--advise` batches codegraph exploration
per group when the deepen skill needs it.

## 6. Run

**Driver:** `smart-mcps-orchestrate run`.
Executes the groups: `scheduler.py` enforces a serial concurrency cap,
`review.py` drives each group through coder/reviewer generations and rounds
to a merge gate. Every worker session starts **fresh** — no forked base
session — because forking bought no real prompt-cache reuse once cwd is a
per-group worktree (`adr/0007`); the base context text is simply prepended to
each worker's first prompt. If a group fails, a mid-run rewrite speccer LLM
call revises its spec from the failure history (the one LLM call this stage
still makes); surprises found by any worker are recorded on the
`SurpriseBoard`. `/orchestrator-run` is the skill that launches, watches, and
triages a run end-to-end, including HITL escalations.

## 7. Resume / resolve / finish

**Drivers:** `smart-mcps-orchestrate resume`, `status`, `answer`, `retry`,
`finish`.
`resume` restarts a crashed or interrupted run; `status` shows run state and
sessions; `answer` answers a pending HITL escalation; `retry` releases a
terminally failed or quarantined group for another attempt
(`adr/0004` — the orchestrator resolves a failed group's stranded work rather
than discarding it). `finish` pushes the integration branch, opens a PR, and
tears down merged groups' worktrees.

## 8. Report

**Driver:** `smart-mcps-orchestrate report`.
Renders every human-facing document (changelog entry, HTML report, PR body,
postmortem-lite) from the run's own artifacts and git history, with **zero**
LLM calls for the facts. The one LLM-authored piece is a one-pager written
under a hard contract (fixed headings, bullet/word caps, every bullet ending
in an artifact pointer) that the same command validates and rejects on
violation (`adr/0008`). This closes the loop on free-form, unread run
write-ups.

## 9. Export

**Driver:** `smart-mcps-orchestrate export <run_id>`.
Writes a framework-agnostic `ingest.json` bundle from a finished run's
artifacts, for external analyzers that consume run data without reading this
repo's internals directly.

## Where the LLM runs

Every LLM call site in the pipeline, and no others:

- Planning skills (`/orchestrator-brainstorm`, `/orchestrator-plan`,
  `/orchestrator-deepen`) — interactive, human-in-the-loop.
- Worker sessions (coder/reviewer) during `run`.
- The mid-run rewrite speccer, on group failure, during `run`.
- The `report` one-pager slot — the only LLM output in stage 8, and it is
  validated before acceptance.

Everything else — grouping, splitting, scheduling, merging, resuming,
reporting facts, exporting — is deterministic.
