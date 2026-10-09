# Handoff — orchestrator seams found while driving infinity-skills run r20261007-100412

Written 2026-10-09 by the run-driver session (smart_mcps, `/orchestrator-run docs/plans/2026-10-07-001-feat-analysis-v3-plan.md` via `--repo ../infinity-skills`). Use this as the opening prompt of a smart_mcps fix session.

## Prompt

> Fix the four orchestrator seams below, found live in run r20261007-100412 (infinity-skills, PR #10). Evidence for each is in `/home/gbm1996/wksp/infinity-skills/.orchestrator/notes-r20261007-100412.md` and the run's `logs/run.log`; the memory note `orchestrator-driver-step-scheduling-seam` summarises them. Add a live-tier test per seam where a seam exists only in a real run (the driver-run item pattern), keep `uv run pytest -q` green, bump the plugin version, and reinstall the tool with `uv tool install --reinstall --no-cache .` before any run.

## The seams, with evidence

1. **No window for a `Run (driver):` step between dependent groups.** `run.log` 2026-10-08 10:26:14 `group g16: completed` → 10:26:19 `group g17 … researcher launching`. g17's research question read `docs/profile/*.md`, which the g16-3 driver item renders; the plan's Decisions assumed "the run-driver runs them between groups". g17 wrote a note against an absent profile (SURPRISE `[missing_dependency]` at 10:28:02); the driver redid U24 by hand after the ingest and render. Same shape for g9 (evaluate) needing the human-reviewed fixture from g7-2.
   Fix candidates: hold a group whose dependency carries unrun `Run (driver):` items until the driver confirms them (an `on_stuck`-tier `group_start` escalation, or a `wait_for_driver` marker the `not run by the coder` merge line already knows about).
   Where: scheduler admission (`orchestrator/execution/`), the merge line that lists `driver-run verification item(s) not run by the coder`.

2. **Evaluate/optimize pin the harness hash before the smoke.** `orchestrator/execution/evaluate_executor.py` `_before_commands` writes `run/eval/harness.sha256` on attempt 1 even when the smoke then fails (`smoke failed (exit 2)` — fixture `reviewed: false`). The operator fixed the fixture, `retry g9` → `evaluate failure — harness path changed since the first evaluation: data/eval/cause_fixture.json` (2026-10-09 03:01:44). Workaround used: move the pin file aside before `retry`. Same code in `optimize_executor.py` (~line 524).
   Fix: pin after a successful smoke/first evaluation, or have the `retry` subcommand reset the pin; test: failed smoke → fixture changed → retry passes.

3. **Autonomous retry after a timed-out `preflight_failed` hands the coder a note that invites `rm -r`.** 2026-10-07 12:23 ESC 8146509c3bb2 (untracked `file:data/` in g2's worktree) timed out at 16:23 → `relaunching on the same spec (operator retry: untracked files left in the worktree: file:data/)` → gen-2 coder ran `rm -r ./file:data` → `PermissionDenied … (harness_allowlist)` → group interrupted → `on_group_failure=halt` → run halted overnight. The run-driver skill text says "untracked leftovers are handled for you (relaunch, then archive)" — they were not.
   Fix: the gate archives untracked leftovers into `groups/<gid>/leftover.patch`/`ignored-outputs/` itself before the relaunch, and the note says so; never ask the coder to delete.

4. **Target-repo tests that read the live data layer race driver data steps.** infinity-skills' server tests build their engine over `data/corpus.db` (shared via `[workspace] data_dirs`); the driver's `ingest-sessions` inserted a session before its atoms were distilled and g19's gate failed one test (ESC fedc4a348855, gate-only retry passed). Orchestrator-side option: a documented "pause while a gate runs" hook for driver steps, or at least a preflight warning when `data_dirs` are also read by the check command's tests. Repo-side: a frozen fixture corpus.

## Smaller items from the same run

- `report --scaffold one-pager` lists commit pointers (`0d10b9fe`) that `--validate` then rejects as unknown; file pointers work.
- The `not auto-finishing` list counted g9-1/g11-1 (harness commands the evaluate groups themselves ran) as unrun driver items — an evaluate/run group's own command should satisfy a `Run (driver, sandbox-safe):` item with the same command.
- Pre-launch spec rewrite of g16 consumed an upstream surprise fine; verification item ids survived (no re-id this time).
- Every designed fail-fast (census bar, unreviewed fixture) under `on_group_failure=halt` costs a full `retry` + `resume` cycle and a new driver process; consider `--on-failure overlap` as the default for runs with `run`/`evaluate` groups whose first command is a gate.
- Run-driver skill: the monitor's "pending escalation" heuristic (request without response file) false-alarms on a timed-out escalation; `status` is the truth.

## Cost/outcome facts for context

24/24 groups, 93 files, +8197/−219; workers ≈ $30 over 29 sessions + enrich rerun $15.74 + uncosted nested `claude -p` (fixture judges ×3 builds, ingest, profile render). Three escalations answered by the driver, one by the human (g13 patience), one timed out (g9 → autonomous → retry).
