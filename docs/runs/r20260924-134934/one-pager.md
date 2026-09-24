# Unit Recipes v1 — the registry, the `run` recipe, and the Artifact Manifest — r20260924-134934

## TL;DR

- All 8 groups completed, run serially from the deepened plan for $60.82 across 11 sessions; g7 (the `run` executor) and g8 (docs) were the only multi-round groups. (g7)
- The `run` recipe was broken end to end as merged: it could not dispatch, could not finish a unit that commits nothing, and a failed unit was merged as RESOLVED. The driver found all three in live verification and fixed each on the integration branch with a regression test. (orchestrator/execution/run_executor.py)
- With those fixes every driver-run item passes on the integration tip — live e2e, Landlock under the real home, kill -9 re-adoption, the live two-group and triage tests — and a second session closed the four seam gaps; the full suite reads 2062 passed. (tests/test_run_recipe_live.py)

## Problems found

Every group passed its own verification; the defects below sit on seams between groups or behind stubs, and only a real run exposed them.

- `dispatch._resolve_factory` split the executor path on the last `.`, while the registry spells it `module:attr`, so every `run` group refused to start; `code` was special-cased and hid it. (orchestrator/execution/dispatch.py)
- The `run` executor always called `merge_group`, which refuses a branch with no commits, so a unit with no `commit_paths` ended INTERRUPTED after its commands succeeded; the executor tests stub the merge. (g7-7)
- Autonomous resolve committed a failed `run` group's leftover outputs and merged them as RESOLVED, bypassing the `commit_paths` gate (R15). (orchestrator/execution/scheduler.py)
- A speccer rewrite drops `driver_run` from verification items; only the grouping assembler set it. g8 was rewritten before launch, so its "Run (driver):" items became coder-gated and unpassable inside Landlock; fixed after the run by deriving the flag in the model. (orchestrator/model.py)
  The marker regex also misses the plan's "Run (driver), optional —" wording, so that item was required even before the rewrite.
- The plan invalidated itself once merged: `size_hints` mark U1–U8 files as prospective, the files now exist, and `group` on the plan was a hard parse error (now a flag, fixed after the run). That made g8's "the plan still groups" item contradict its five-file scope item. (docs/plans/2026-09-24-001-feat-unit-recipes-run-plan.md)
- The group heartbeat read "still starting the coder" for a whole coder round, because the probe flipped the phase only when the newest event was `assistant` and streaming makes it `stream_event`; fixed after the run. (orchestrator/execution/liveness.py)
- The report counted 4/9 units landed: driver-run items read "unverified", a rewritten spec's items were sliced by the original plan, and the spec-rewrite resume restarted g8 at round 1, overwriting the pre-interrupt round-1 report; all three fixed after the run. (orchestrator/report/facts.py)
- Auto-finish fired the moment g8 merged, before any driver-run item ran; only the invalid draft one-pager stopped a push of the broken recipe. The CLI now holds while required driver-run items exist. (orchestrator/execution/finish.py)

## Run notes

No escalation was raised; every intervention below came from driver-run verification or from watching g8 loop.

- Preflight committed the finished deepen pass (U1–U9) and the ADR 0011 edit, which were uncommitted; the run launched on that commit. (90ea45ee)
- g2-3, g6-1, g4-4 and g7-5 passed as run: an old `groups.json` loads with every recipe `code`, an old run exports with `schema_version` 2 and no manifest key, the live e2e suite passes 10/10, and a Run Child is denied another project's `~/.claude/projects` dir while a declared `allow_write` path is writable. (g7-5)
- g7-7 on a scratch fixture found the dispatch bug, then the empty-merge bug; after both fixes a kill -9 of the orchestrator mid-`sleep 120` was re-adopted on resume with the same pid, one `sleep` ever existed, and the group completed. (g7-7)
- g8's first coder retired after 3 rounds ($16.95), and the second generation oscillated applying and reverting the plan fix. The driver stopped the run with SIGINT, cherry-picked the coder's `size_hints` drop onto integration, merged integration into g8's branch, and wrote an operator `spec-gen2.json` restoring `driver_run` on v5/v6; the fresh coder then passed in one round ($0.98). (g8/coder/gen2)
- g8-v5, the live two-group run, passed 6/6. g8-v6, the live triage test, first ended RESOLVED; after the scheduler fix it ended FAILED as intended, which exposed a test that asserted exit 0 and a vacuous diagnosis check. Both were corrected, and the live file now passes 7/7. (g8)
- Auto-finish aborted before pushing because the unfilled one-pager scaffold, swept into a recover commit, failed validation; nothing reached the remote until this report. (docs/runs/r20260924-134934/one-pager.md)
- After the run, on the human's request, the driver fixed the orchestrator defects above on the integration branch — plan reader, driver-run flag, phase flip, auto-finish guard, round numbering, landed metric — each with a regression test; full suite 2034 passed and both live suites 17/17. (orchestrator/execution/generation.py)
- A second session landed the gap fixes, one commit each with a revert-checked regression test: an `interface_exports` difficulty signal, active only when non-zero (against this run's trace, g1/g2/g4/g5 read paired); a `SURPRISE … (already merged)` line in run.log; a coder prompt allowing a driver item that spawns no nested `claude`; a heartbeat relabel per poll; the preflight baseline captured in the provisioned integration worktree (a stale `node_modules` gave 606 UI tests at launch against 649 at the gate); a nonzero exit with zero failures named `unattributed_exit`. (orchestrator/execution/merge.py)

## Next steps

- Drive one run on a repo with a `ui/` frontend and no `node_modules` in the main checkout: "done" is the launch line `preflight baseline: captured … in <integration worktree>` with a test count matching the gate. (orchestrator/cli.py)
- Watch the next grouping for `interface_exports` in the trace and `→ paired` on its interface producers; the bidirectional form was declined. (orchestrator/grouping/pipeline.py)
- Label the untracked-file commit by cause (F5) and dedupe near-duplicate surprises (F7), both deferred as cosmetic. (orchestrator/execution/merge_ladder.py)
