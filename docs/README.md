# Documentation index

Everything in `docs/` — what it is, and where to start depending on what you
are trying to do.

The orchestrator's own vocabulary lives in `CONTEXT.md` at the repo root, not
here. **If a term in any document below reads as ambiguous, `CONTEXT.md` is the
authority** — it defines Preflight, Work Failure, Interrupted, Resolve, Slice,
Unit Recipe, Artifact Manifest and the rest, each with the synonyms that were
deliberately rejected.

______________________________________________________________________

## Start here

| If you want to…                                     | Read                                                                 |
| --------------------------------------------------- | -------------------------------------------------------------------- |
| Understand how a run actually executes              | [`orchestrator-flow.md`](orchestrator-flow.md)                       |
| Understand how a plan becomes groups                | [`orchestrator-grouping.md`](orchestrator-grouping.md)               |
| Write a plan the grouper can consume without an LLM | [`orchestrator-task-map.md`](orchestrator-task-map.md)               |
| Tune the grouper                                    | [`orchestrator-grouping-config.md`](orchestrator-grouping-config.md) |
| Use the local front-end                             | [`observatory.md`](observatory.md)                                   |
| Generate or read a run report                       | [`orchestrator-report.md`](orchestrator-report.md)                   |
| Consume a finished run from outside                 | [`run-bundle-contract.md`](run-bundle-contract.md)                   |
| Know what is currently broken                       | [`orchestrators_improvements.md`](orchestrators_improvements.md)     |

______________________________________________________________________

## Decisions — `adr/`

Architecture decisions, numbered and immutable. A decision recorded here
outlives the plan that produced it; reversing one is a deliberate act, not an
incremental change. `CONTEXT.md` cites several of these directly.

| ADR                                                                                        | Decision                                                                         |
| ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| [0001](adr/0001-plan-time-semantic-grouping-signals.md)                                    | Plan-time discrete labels are the grouping semantic layer, not embeddings        |
| [0002](adr/0002-per-run-dag-snapshot.md)                                                   | The run DAG is snapshotted into the run directory, not read from the shared file |
| [0003](adr/0003-groupings-are-named-directories.md)                                        | A grouping is a named directory, selected explicitly at run time                 |
| [0004](adr/0004-orchestrator-resolves-a-failed-groups-stranded-work.md)                    | The orchestrator commits and merges a failed group's stranded work               |
| [0005](adr/0005-hitl-permission-routing-transport.md)                                      | Transport for live PreToolUse permission routing — **proposal only**             |
| [0006](adr/0006-delete-the-grouping-time-speccer.md)                                       | Delete the grouping-time speccer; specs are assembled deterministically          |
| [0007](adr/0007-workers-start-fresh-instead-of-forking-the-base-session.md)                | Workers start fresh instead of forking the base session                          |
| [0008](adr/0008-run-reports-are-generated-from-artifacts-the-llm-fills-validated-slots.md) | Run reports are generated from artifacts; an LLM only fills validated slots      |
| [0009](adr/0009-suspend-cure-kills-the-pid-tree-and-resumes-in-place.md)                   | A suspend cure kills the child's pid tree and warm-resumes in place              |
| [0010](adr/0010-the-pipeline-is-human-planned-agents-never-create-nodes.md)                | The pipeline is human-planned; agents never create nodes                         |

______________________________________________________________________

## The work pipeline

Work moves through four stages, each with its own directory and its own skill:

```
ideation/  →  brainstorms/  →  plans/  →  runs/
  a seed      requirements     units +     what actually
              with R-IDs       task map    happened
              /orchestrator-   /orchestrator-   /orchestrator-
              brainstorm       plan             run
```

`findings` documents and `handoffs/` fall out of the last stage and feed the
first. `todos/` holds deferred work that has no stage yet.

### Traced chains

Following one topic end to end is usually more useful than reading a directory.

| Topic                                        | Ideation                                                                                      | Brainstorm                                                                                      | Plan                                                                                                                | Run                                                    |
| -------------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| **Unit Recipes** (current)                   | [B — not every unit is a coding task](ideation/2026-09-15-B-agent-kinds-and-plan-checks.md)   | [2026-09-21](brainstorms/2026-09-21-unit-recipes-requirements.md)                               | Plan A: [2026-09-23-001 review-loop split](plans/2026-09-23-001-refactor-review-loop-split-plan.md); Plan B pending | —                                                      |
| **Stall detection & decision carry**         | [A — stalled work is invisible](ideation/2026-09-15-A-stall-detection-and-decision-carry.md)  | [2026-09-16](brainstorms/2026-09-16-stall-detection-and-decision-carry-requirements.md)         | [2026-09-16-001](plans/2026-09-16-001-feat-stall-detection-and-decision-carry-plan.md)                              | [r20260916-113121](runs/r20260916-113121/one-pager.md) |
| **Deterministic grouper + advisory**         | —                                                                                             | [2026-08-28](brainstorms/2026-08-28-grouper-speccer-flow-requirements.md)                       | [2026-08-28-001](plans/2026-08-28-001-feat-deterministic-grouper-advisory-plan.md)                                  | [r20260828-220035](runs/r20260828-220035/one-pager.md) |
| **Plan split + deepen skill**                | —                                                                                             | —                                                                                               | [2026-08-29-001](plans/2026-08-29-001-feat-plan-split-and-deepen-plan.md)                                           | [r20260829-162627](runs/r20260829-162627/one-pager.md) |
| **Crash recovery & teardown**                | —                                                                                             | [2026-08-19](brainstorms/2026-08-19-orchestrator-crash-recovery-requirements.md)                | [2026-08-19-001](plans/2026-08-19-001-fix-orchestrator-crash-recovery-and-teardown-plan.md)                         | —                                                      |
| **Control isolation & observability**        | [2026-08-11](ideation/2026-08-11-orchestrator-control-isolation-and-observability-changes.md) | —                                                                                               | [2026-08-11-001](plans/2026-08-11-001-fix-orchestrator-control-isolation-observability-plan.md)                     | —                                                      |
| **Correctness & measurement**                | —                                                                                             | [2026-07-29](brainstorms/2026-07-29-orchestrator-correctness-and-measurement-requirements.md)   | [2026-07-29-001](plans/2026-07-29-001-fix-orchestrator-correctness-and-measurement-plan.md)                         | —                                                      |
| **Run hardening**                            | —                                                                                             | [2026-07-22](brainstorms/2026-07-22-orchestrator-run-hardening-requirements.md)                 | [2026-07-22-001](plans/2026-07-22-001-feat-orchestrator-run-hardening-plan.md)                                      | —                                                      |
| **Grouping improvement**                     | —                                                                                             | [2026-07-22](brainstorms/2026-07-22-orchestrator-grouping-improvement-requirements.md)          | [2026-07-25-001](plans/2026-07-25-001-feat-orchestrator-grouping-improvement-plan.md)                               | —                                                      |
| **Observatory (front-end)**                  | —                                                                                             | [2026-07-21](brainstorms/2026-07-21-orchestrator-frontend-requirements.md)                      | [2026-07-21-001](plans/2026-07-21-001-feat-orchestrator-observatory-plan.md)                                        | —                                                      |
| **Run ingestion v2 / framework abstraction** | —                                                                                             | [2026-09-03](brainstorms/2026-09-03-run-ingestion-v2-and-framework-abstraction-requirements.md) | [2026-09-03-001](plans/2026-09-03-001-feat-run-bundle-v2-export-plan.md)                                            | —                                                      |
| **The orchestrator itself**                  | —                                                                                             | [2026-07-15](brainstorms/2026-07-15-multiagent-orchestrator-requirements.md)                    | [2026-07-15-001](plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md)                                         | —                                                      |

Plans without an upstream brainstorm in this repository:
[2026-07-11-001 formatting + codegraph](plans/2026-07-11-001-fix-ai-edit-formatting-codegraph-plan.md) ·
[2026-08-07-001 reliability gaps](plans/2026-08-07-001-fix-orchestrator-reliability-gaps-plan.md) ·
[2026-08-26-001 observatory legibility and run resilience](plans/2026-08-26-001-fix-observatory-and-run-resilience-plan.md) ·
[2026-09-02-001 run reports](plans/2026-09-02-001-feat-run-report-plan.md)

Ideation with no downstream document yet:
[2026-08-08 observatory grouping provenance, attempt history, cost accounting](ideation/2026-08-08-observatory-grouping-provenance-and-attempt-history.md)

______________________________________________________________________

## Research — `research/`

External prior art and analysis, kept so the same ground is not covered twice.
**Check here before commissioning research.**

- [**2026-09-21 — Typed agent pipelines, node registries, and dynamic graphs**](research/2026-09-21-agent-pipeline-prior-art.md)
  Registry survey (Goose, deepagents, Roo Code, Claude Code subagents,
  OpenHands, gh-aw, Dify, Argo, Flyte, Temporal), the AOrchestra counter-thesis,
  inter-step artifact passing, dynamic-graph mechanisms and why we decline them,
  MAST and long-context failure taxonomies, cost-gate prior art, and a
  per-project learnings/watch-outs table.
- [**2026-09-21 — KPI-optimization loops, and what Karpathy's autoresearch teaches**](research/2026-09-21-kpi-optimization-prior-art.md)
  FunSearch through AlphaEvolve, OpenEvolve, ShinkaEvolve, ADAS, DGM/MGM, GEPA,
  OPRO, AIDE, MLE-STAR with their real evaluation budgets; `autoresearch` read
  from source; the 2026 reward-hacking literature; a statistical accept
  construction that costs zero extra evaluations. **Input to the KPI-loop
  brainstorm, which has not been written yet.**
- [infinity-skills — ingestion & analysis-readiness report](research/infinity-skills-analysis.md)
  The downstream consumer of the Run Bundle.
- [CoCoder analysis](research/cocoder-analysis.md) ·
  [Design deviations from source research](research/design-deviations.md)

______________________________________________________________________

## Findings — post-mortems

Dated investigations of things that went wrong, at the repository root of
`docs/`. Several became brainstorms; all are evidence a later design can cite.

- [2026-08-13 — post-validation findings and fix plan](2026-08-13-orchestrator-post-validation-findings.md)
- [2026-08-18 — crash recovery is the gap (r20260812-202855)](2026-08-18-orchestrator-crash-recovery-findings.md)
- [2026-08-20 — why the estimate under-predicts coder context ~3×](2026-08-20-estimator-underestimation-findings.md)
- [2026-08-20 — Observatory front-end findings](2026-08-20-observatory-frontend-findings.md)
- [2026-08-30 — the rewrite speccer's new name never reaches `groups.json`](2026-08-30-speccer-rewrite-persistence-findings.md)
- [2026-08-31 — learning_podcast run notes (r20260830-211717)](2026-08-31-orchestrator-notes-r20260830-211717-findings.md)

______________________________________________________________________

## Handoffs — `handoffs/`

Written when a session ends with work someone else (or a later session) picks
up. Each names what exists, what is owed, and what to read first.

- [2026-09-21 — Unit Recipes brainstorm + prior-art research](handoffs/2026-09-21-unit-recipes-session-handoff.md) *(most recent)*
- [2026-07-25 — run hardening landed; the to-do it exposed](handoffs/2026-07-25-orchestrator-run-hardening-executed-followups.md)
- [2026-07-16 — Phase D wrap + the grouping problem to solve next](handoffs/2026-07-16-multiagent-orchestrator-phase-d-and-grouping-next.md)
- [2026-07-16 — Phase C, product surface](handoffs/2026-07-16-multiagent-orchestrator-phase-c-handoff.md)
- [2026-07-15 — Phase B, execution engine](handoffs/2026-07-15-multiagent-orchestrator-phase-b-handoff.md)

______________________________________________________________________

## Deferred work — `todos/`

- [Grouping improvements — what made a pre-mapped plan take 15 validation runs](todos/grouping_improvements.md)
- [Orchestrator → Infinity Skills ingestion, post-v1](todos/orchestrator-infinity-skills-ingestion.md)

______________________________________________________________________

## Runs — `runs/`

One directory per run that opted into report generation
(`[docs] formats` in `.orchestrator/config.toml`), each holding a one-pager and
a CHANGELOG entry generated from run artifacts, never from an LLM's memory
(ADR 0008). [`RUNLOG.md`](RUNLOG.md) is the chronological log across runs.

Present: `r20260828-220035` · `r20260829-162627` · `r20260916-113121`

______________________________________________________________________

## Conventions

**Naming.** Brainstorms are `YYYY-MM-DD-<topic>-requirements.md`; plans are
`YYYY-MM-DD-NNN-<feat|fix>-<topic>-plan.md`; ADRs are `NNNN-<kebab-title>.md`;
ideation, research, findings and handoffs are date-prefixed.

**Stable identifiers.** A brainstorm's `R<N>` IDs and their short tags are
**stable forever** — never renumbered or renamed. A plan unit that mainly
implements a requirement reuses that requirement's tag as its slug, so the same
handle follows an idea from requirements through planning, deepening and the
run. Retire an ID by marking it; append new ones at the end.

**Durability.** Anything a later session would want belongs in a committed
document under `docs/`. On a local machine `.orchestrator/` survives a Claude
Code restart, but **in a remote container it is destroyed with the container and
is no safer than `/tmp`** — see the durability section in `CLAUDE.md`. Promote
findings, research and handoff notes into `docs/` before the work is done.
