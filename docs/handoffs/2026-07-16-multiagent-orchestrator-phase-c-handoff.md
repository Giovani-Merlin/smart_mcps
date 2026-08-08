---
date: 2026-07-16
topic: multiagent-orchestrator
phase: C (product surface, U9–U10)
plan: docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md
branch: feat/multiagent-orchestrator
---

# Phase C handoff — multi-agent orchestrator product surface

For the session executing Phase C. Phases A (grouping, U1–U4) and B (execution
engine, U5–U8) are complete and committed on `feat/multiagent-orchestrator`
(through `2755456`). Nothing is pushed; no PR exists. 172 tests, zero tokens in
the suite. The plan is the decision artifact — this handoff adds only
session-learned context the plan does not carry.

## How to start

```
/compound-engineering:ce-work Execute Phase C of the plan docs/plans/2026-07-15-001-feat-multiagent-orchestrator-plan.md , context from last session on docs/handoffs/2026-07-16-multiagent-orchestrator-phase-c-handoff.md
```

- Stay on `feat/multiagent-orchestrator` — the whole feature ships from here.
- Read the plan's **U9–U10** and `docs/research/design-deviations.md` (now carries
  the U5 spike findings and Phase B implementation notes) before writing code.
- Per the Phase B decision: the full deep code review runs **once, at the
  end-of-plan PR** — after U10, not per unit.

## What Phase B left you (the U9 wiring surface)

All under `orchestrator/execution/`:

- `sessions.py` — `SessionRunner(claude_bin, timeout_s, model, permission_mode, allowed_tools, transcript_root, env, tracker)`. `preflight()`,
  `start_base(run_id, base_context, cwd)`, `start_fork(base_id, prompt, name, cwd)` (serialized), `resume(...)`, `usage_of(sid)`, `transcript_path(sid)`
  (glob by UUID under `transcript_root`). `claude_bin` accepts a list — tests
  pass `[sys.executable, "tests/fake_claude.py"]`.
- `review.py` — `make_executor(ReviewDeps) -> Executor` is the whole review
  loop. `ReviewDeps` seams U9 must wire: `workspace_for`, `merge_group`,
  `rewrite_spec`, `base_ref_for` (plus runner/store/manifest/board/configs).
  `GroupFailure` is raised for cap exhaustion; the scheduler catches it and
  records `failure` in the state file — the CLI maps failed groups to a
  nonzero exit, it does not see exceptions.
- `scheduler.py` — `Scheduler(groups, paths, executor, config, resume)` with
  `await scheduler.run() -> dict[gid, GroupState]`. `scheduler.tracker` is the
  `SubprocessTracker` to hand the `SessionRunner`. Sequential mode and
  concurrency come from `ExecutionConfig`.
- `merge.py` — `IntegrationMerger(repo_root, run_id, launch_ref)`:
  `ensure()`, `tip()`, `merge_group(group, worktree)` (matches the ReviewDeps
  seam; raises `review.MergeConflict`).
- `worktrees.py` — `create_worktree(repo_root, group_id, name, branch, start_point)`, `group_branch(run_id, gid)`, `remove_worktree`, `diff_stat`.
- `manifest.py` — `RunPaths(repo_root, run_id)` (single source of the
  `.orchestrator/runs/<run_id>/` layout), `ManifestStore`, `record_session`,
  `atomic_write_text`.
- `grouping/pipeline.py` `run_grouping()` / CLI `group` command (with
  `--dry-run`) already write `.orchestrator/groups.json` +
  `.orchestrator/base-context.md` — `run` consumes those artifacts;
  `GroupingResult.model_validate_json` reads groups.json back.

## U9 wiring recipe (the non-obvious parts)

- **Construction order is circular on paper, late binding resolves it.**
  `Scheduler` needs an executor; the executor needs `ReviewDeps`; `ReviewDeps`
  needs a `SessionRunner`; the runner wants `scheduler.tracker`. Build the
  scheduler with an executor closure over a variable assigned afterwards
  (`executor = lambda ctx: make_executor(deps)(ctx)` style) — it is only called
  inside `scheduler.run()`, after `deps` exists.
- **`workspace_for` and `base_ref_for` must agree per group.** At
  ready→running, capture `tip = merger.tip()` once, create the worktree with
  `start_point=tip` and branch `group_branch(run_id, gid)`, and remember
  `tip` per group so `base_ref_for` returns the same commit for the reviewer's
  diff and the handoff's `diff_stat`. Do not call `merger.tip()` twice — an
  interleaved sibling merge would move it.
- **`rewrite_spec` adapts to the Phase A speccer.** Go through
  `grouping/speccer.py::write_specs` unchanged: build a one-group skeleton
  (tasks/descriptions/files from the `Group`) and fold the `Surprise`
  descriptions into it as context. Phase B already synthesizes context
  surprises for blocked/too_hard/structural/merge-conflict escalations, so the
  list is never empty on those paths.
- **`run` command flow:** load config → load groups.json + base-context.md
  (actionable error if missing: "run `group` first") → `SessionRunner`
  preflight → `merger.ensure()` → `start_base(cwd=repo_root)` → new
  `RunManifest` with `base_session_id` + save → `asyncio.run(scheduler.run())`
  → print per-group outcomes (state + `failure` from the state file) → exit 0
  only if every group completed.
- **`resume` command:** `Scheduler(..., resume=True)` reads state.json itself
  and reaps orphans; load the existing manifest (do not recreate it) and take
  `base_session_id` from it. `status` just pretty-prints state.json + manifest.
- **run_id convention is yours to pick** (nothing depends on its shape yet;
  it lands in branch names, session display names, and paths — keep it short,
  filesystem- and ref-safe, e.g. `r<YYYYMMDD-HHMMSS>`).
- **Flags > config-file > defaults** (plan U9): flags at least for
  `--sequential`, `--concurrency`, `--permission-mode`, token budget, review
  intensity override. `load_config` already handles file > defaults; layer
  flag overrides with `model_copy(update=...)` on the loaded config.
- **pyproject is already done** (Phase A): script, `networkx`, wheel packages.
  U9's packaging verification is just `uv build` + `--help` from a clean
  checkout.
- **Manifest/state writes stay on the event-loop thread.** review.py persists
  manifest/artifacts in the coroutine and only wraps CLI calls in
  `asyncio.to_thread` — keep the CLI wiring on that pattern.

## U10 notes

- `tests/fake_claude.py` already speaks the full CLI surface: `$FAKE_CLAUDE_HOME`
  with `script.jsonl` (front-popped response queue: `result`, `usage`,
  `is_error`, `exit_code`, `stderr`, `delay_s`), `calls.jsonl` (argv/prompt/cwd/
  timings), `sessions/` (resume validation + fork parents), `projects/`
  (transcript stubs — point the runner's `transcript_root` here),
  `fork.lock`/`fork_overlaps.log` (fork-serialization detector), env knobs
  `FAKE_CLAUDE_HIDE_FLAGS`, `FAKE_CLAUDE_DEFAULT_DELAY_S`. Extend it for E2E
  scenario scripting rather than building a second stub; per-session scripting
  beyond the global queue is the likely gap (the in-process `StubRunner` in
  `test_review_loop.py` shows the scenario shapes: approve, reject-then-approve,
  reject-forever, surprise, too-hard).
- The E2E fixture repo needs `git config user.email/name` set (see the fixtures
  in `test_merge.py` / `test_sessions.py`) and the toy plan + stubbed LLM
  runner pattern from `test_grouper_pipeline.py`.
- README must carry the `.gitignore` guidance: target repos should ignore
  `.orchestrator/` and `.worktrees/` (System-Wide Impact section of the plan).
  This repo's own `.gitignore` will want them too before any live self-run.
- E2E asserts the analyzer contract (AE6): manifest session IDs ↔ stub
  transcript paths, `<run-manifest ...>` first-prompt block, worktree paths
  containing the repo dir name.

## Phase B gotchas that will bite Phase C

- **The PostToolUse format hook strips momentarily-unused imports** — it bit
  again in Phase B. Batch import+usage in one edit, and re-check imports after
  any edit that adds them separately.
- **Group branches deliberately do not nest under the integration branch**:
  `orchestrator/<run_id>-<gid>` vs `orchestrator/run-<run_id>` — git refuses a
  ref that is both a name and a directory. Don't "clean up" the naming.
- **Breaker context signal** = latest round's `input + cache_read + cache_creation + output` tokens (`SessionUsage.last_context_tokens`), never
  `input_tokens` alone.
- **Dirty group worktrees are left in place by design** after merge (cleanup
  refuses to destroy uncommitted state) — the E2E happy path should script
  coders that commit, or the fixture repo accumulates worktrees.
- **`ExecutionConfig.max_rewrites` (default 2) exists since Phase B** — a
  perpetual too_hard loop fails the group; E2E's too-hard scenario must script
  within (or assert) that bound.
- **The suite must stay token-free (R24).** One manual live smoke run happens
  at U10's end on a small real plan; everything else runs against the stub.
- pytest-asyncio is in strict mode: `@pytest.mark.asyncio` on every async test.

## Definition of done for Phase C

`smart-mcps-orchestrate group|run|status|resume` working end-to-end against the
stub; flag > config-file > default precedence tested; `uv build` and `--help`
from a clean checkout; the full-run E2E on the toy fixture repo green offline
with every scripted failure scenario (rejection, breaker, surprise, conflict)
ending in its documented terminal state; `orchestrator/README.md` documenting
commands, config, run-artifact layout, and gitignore guidance; deviations doc
updated with anything U9/U10 teaches. Then: one manual live smoke run on a
small real plan, and the end-of-plan deep review on the PR.
