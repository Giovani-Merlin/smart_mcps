---
date: 2026-07-16
topic: multiagent-orchestrator
phase: D done (HITL, U11–U14) → next session focus: grouping quality + forced live testing
plan: docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md
branch: feat/multiagent-orchestrator
---

# Handoff — Phase D wrap + the grouping problem to solve next

Phase D (human-in-the-loop escalation, U11–U14) is **implemented and verified
offline** on `feat/multiagent-orchestrator` (232 tests green, ruff clean, `uv build` OK) and has had **one live smoke run** against the real `claude` CLI. The
plan document now carries a Phase D section; `docs/research/design-deviations.md`
carries the Phase D notes. Nothing is pushed; no PR exists yet.

This session's live smoke also surfaced the thing the **next session should own:
grouping quality**. This handoff records what the live run proved, answers the
"do we group for testability?" question authoritatively, and lays out the grouping
improvements + the forced-testing plan.

## What the live smoke run proved (run id `smoke1`)

Target: a throwaway git worktree `../smart_mcps-fe-test` (branch
`test/orchestrator-frontend`), plan `frontend-plan.md` (a React+Vite+TS dashboard,
3 tasks). Command: `run --hitl --sequential --review-intensity paired`. Result:
**both groups completed, exit 0, ~5 min wall-clock**, integration branch
`orchestrator/run-smoke1` assembled with real, correct code (11 `ui/` files, typed
React that even mirrors our own `GroupState` union). Manifest joins base + coder +
reviewer per group with transcripts (AE6 holds). Paired review ran live; the
reviewer's `git diff` worked (Bash allowlisted in the target `.orchestrator/config.toml`).

**What it did NOT prove:** no escalation actually *fired* — nothing got stuck, so
the live HITL trigger→answer→resume round-trip is still only covered **offline**
(`tests/test_e2e_stub.py::test_hitl_answer_a_question_then_skip_a_too_hard_group`).
Forcing those paths live is a next-session task (see "Engineering-force the rest").

**Reproduce / inspect:** the worktree and its `orchestrator/run-smoke1` branch are
left in place. `cd ../smart_mcps-fe-test && git log --oneline orchestrator/run-smoke1`.
The run artifacts are under `../smart_mcps-fe-test/.orchestrator/runs/smoke1/`
(state.json, manifest.json, logs/run.log event log, escalations/ — empty this run).
Note: codegraph had to be `codegraph init . && codegraph index .`'d in the target
before `group` would run ("CodeGraph not initialized").

## The question answered: do we group for testability / vertical slices?

**No. Grouping is driven entirely by codegraph *structural* signals — there is no
testability, vertical-slice, or feature/module objective anywhere.** Concretely:

- **Affinity (symmetric clustering weight)** — `orchestrator/grouping/graphing.py::build_task_graph`:
  - `shared_file` (1.0): two tasks that touch the same file.
  - `call` (2.0): one task's symbol calls another's (`client.callers/callees`).
  - `impact` (1.5): one task's write-surface affects code another task owns (`client.impact`).
  - `prose_neighbor` (0.5): the *only* non-code signal — `pipeline.py::_with_prose_fallback`
    attaches a **region-less** task to its plan-order neighbor with a weak edge.
- **Dependencies (directed, → group DAG + merge order)** — the same call/impact
  edges, kept directed (caller-task depends on callee-task), lifted to groups in
  `graphing.py::build_group_dag`.
- **Partition** — `partition.py::DefaultPartitionStrategy`: hub isolation → Louvain
  community detection on the affinity graph → lift independents → split over-budget
  → merge small. Objectives baked in: affinity modularity, a token **budget cap**,
  and a makespan no-regression guard. **Testability is not among them.**
- **Mapper** (`grouping/mapper.py`) maps each plan task → files/symbols (LLM),
  verified against codegraph. There is **no** "module" / "feature" / "slice" concept
  — a task is a bag of code regions, nothing more.

### Consequences (why this matters)

1. **Cross-stack features fragment.** A backend task (Python route) and its frontend
   consumer (a TS `fetch("/api/x")`) share no file and have no call/impact edge —
   **codegraph builds edges within a language**, and there is no static edge from TS
   to a Python handler. So they land in **separate** groups, each coded+reviewed
   independently against its own verification items. **No group ever verifies the
   slice end-to-end** (that the frontend actually talks to the backend). That is
   exactly the "stack backend+frontend for a module so it's testable" idea — and we
   do not do it.
2. **Greenfield collapses to prose-order.** No code ⇒ no shared_file/call/impact
   edges ⇒ only the weak `prose_neighbor` fallback survives. In `smoke1` this
   produced **all-independent groups with the dependency chain lost**, even though
   the speccer's *prose* correctly wrote "this group builds on the scaffold group."
   The structural DAG never saw it.
3. **Testability is never an explicit objective** — difficulty picks review
   *intensity*, verification items are per-group, but nothing sizes/shapes a group
   around "what forms a coherent, independently-testable unit."

## Improvements needed (next session's focus) — prioritized

### 1. Greenfield dependency ordering (HIGH — bit us live)

Root cause is concrete and cheap to attack: **the mapper drops mappings to
nonexistent files** (we saw ~14 "mapped nonexistent file ui/... — dropped" flags),
so greenfield tasks that co-edit the same *planned* file lose their shared-file
affinity **and** their ordering. Leads to evaluate:

- **Retain nonexistent-file mappings as "prospective files"** — flagged, not
  codegraph-verified — so `build_task_graph`'s shared-file affinity and a
  prospective-file dependency ("the task that *creates* F is upstream of tasks that
  *use* F") still fire greenfield. This alone would have clustered the three `ui/`
  tasks and inferred scaffold→consumers. Touch points: `mapper.py` (stop dropping;
  tag as prospective), `graphing.py::build_task_graph` (accept prospective files),
  `pipeline.py` (feed them through).
- **Let the mapper/LLM emit explicit inter-task dependency hints** (`depends_on`)
  and fold them into `build_group_dag` as first-class edges — a semantic ordering
  signal for when there is no code to infer from.
- Until fixed, the operator workaround is what I did: hand-edit `groups.json`
  `dependencies` after `group`, before `run` (the `--dry-run`/artifact split exists
  for exactly this checkpoint). Document it as the interim path.

### 2. A testability / vertical-slice / cross-stack objective (the deeper gap)

- Add a semantic **feature/module label** from the mapper (LLM) as an affinity
  signal, so a backend task and its frontend consumer can co-group *across
  languages where codegraph has no edge*. A cross-language edge (TS↔Python) is
  invisible structurally — an LLM/semantic bridge is the only option.
- Or a post-partition **cohesion pass** that keeps a task with the tasks its
  verification depends on (so a group is independently testable).
- Decide the objective explicitly: is a group "a cluster of tightly-coupled code"
  (today) or "an independently-shippable, independently-testable slice" (arguably
  what we want)? This is a design decision, not just a knob.

### 3. Grouping non-determinism

`--dry-run` gave **3 groups**, the real `group` gave **2** (merged T2+T3) — same
plan, same repo, minutes apart. Both valid, but the boundary is LLM-driven
(mapper + speccer) and unstable. Decide: seed/cache the mapper, or accept it and
lean on `--dry-run` as the human checkpoint (and say so in the README).

### Thorough grouping testing (currently thin on real behavior)

`test_graphing.py` / `test_partition.py` / `test_grouper_pipeline.py` all stub
codegraph. Add: **greenfield fixtures** (no code → assert prospective-file
clustering + ordering once #1 lands), **cross-stack fixtures** (assert a
feature's halves co-group once #2 lands), explicit **dependency-ordering**
assertions, and a **real-repo grouping smoke** (init+index a fixture repo, run the
real pipeline, characterize output). Grouping is the least-live-tested stage and
the one we most need to trust.

## Engineering-force the rest (live HITL, still only offline-proven)

Each Phase D trigger, with a concrete way to force it against the real CLI:

- **coder_question (`needs_input`)** — a one-task plan whose spec embeds a genuine
  ambiguity plus "if X is unclear, end your turn with status `needs_input` and your
  question." Then answer live via `smart-mcps-orchestrate answer <run> <esc> --action answer --text ...`
  and confirm the coder resumes with the answer.
- **caps_exhausted → grant** — tight breaker (`[breaker] max_generations=1, max_rounds_per_generation=1`) + a task a paired reviewer will bounce once
  (`changes_required`) → breaker trips → generation cap → `caps_exhausted`
  escalation. `answer` grants gen-2.
- **merge_conflict** — two groups with **no** dependency both editing the same file
  with different content; the second to merge conflicts → escalation (or autonomous
  rewrite if HITL off).
- **reviewer_too_hard / \_structural** — hardest to force honestly; a deliberately
  over-scoped single task, or accept the offline coverage.
- **abort → resume** — trigger any escalation, answer `--action abort`, confirm the
  run stops cleanly with a resumable state, then `resume` continues.

Supervision loop that worked this session: launch `run --hitl` in the background
(**stdout to a *separate* console log, NOT `logs/run.log`** — the orchestrator
writes the event log there itself; redirecting the CLI's stdout onto it collides),
then a Monitor poll over `escalations/request-*.json` (unanswered) + tail
`logs/run.log`, answering via the `answer` subcommand.

## Gotchas carried forward

- **PostToolUse format hook strips momentarily-unused imports** — bit repeatedly in
  Phases B/C/D. Add code that *uses* a symbol first, or add import+usage in one
  edit; re-check imports after any edit that adds them separately.
- **Target repo needs codegraph init+index** before `group`, and **`.gitignore`**
  needs `.orchestrator/` + `.worktrees/` (this repo already has them).
- **Headless workers need tools allowlisted** — the reviewer runs `git diff` (Bash).
  The smoke config set `[session] allowed_tools = ["Bash","Read","Write","Edit", "MultiEdit","Glob","Grep","LS","TodoWrite"]` with `permission_mode="acceptEdits"`;
  that let coders write files and reviewers diff without prompts. No worker got
  stuck on permissions.
- **HITL event log vs CLI stdout** collide if both point at `logs/run.log` (see
  supervision note above).
- **Nested worktrees are fine**: the target's `.worktrees/<gid>/` sit inside the
  target worktree; git handles it because `.worktrees/` is gitignored.

## Suggested next-session kickoff

```
Read docs/handoffs/2026-07-16-multiagent-orchestrator-phase-d-and-grouping-next.md
and docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md. Phase D is done
and smoke-tested. This session: (1) fix greenfield dependency ordering + add a
testability/vertical-slice grouping objective, with thorough grouping tests; (2)
engineering-force the live HITL trigger paths. Stay on feat/multiagent-orchestrator.
```

Still open from before (unchanged): push the branch + the **end-of-plan deep code
review on the PR** (per the Phase B decision, the full review happens once, there)
once we're satisfied with grouping.
