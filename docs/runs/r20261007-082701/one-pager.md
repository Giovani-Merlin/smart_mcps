# Fixes from run r20261006-162245 — export honesty, result-aware liveness, run phase lines, own-group surprises, the live-tier guard, and two plan-check seeds — r20261007-082701

## TL;DR

The run closed the tool-side backlog that driving infinity-skills run r20261006-162245 left behind, plus the two plan-check seeds S1 and S2. Eight groups ran serially on sonnet in 52 minutes of wall clock.

- 8/8 groups completed and 8/8 units landed in one generation each; two merge gates needed an operator fixture fix and no group was rewritten (g8)
- Cost $4.63 across 9 sessions, 660k output tokens plus 5.7M cache-read, 25 files changed at +988/-71 (78b4232a)
- Export refuses metadata-only bundles, liveness reads CPU ticks after a `result` as idle, run recipes log one line per command, own-group surprises are dropped, and plan-check lints Goal symbols against Files (u8)

## Problems found

Both merge-gate failures had one shape: the plan listed only the files a unit edits, not the existing test that asserts the behaviour the unit changes.

- g6's new report gate tripped the pre-existing driver-item fixture in test_review_loop.py, which names `tests/test_x.py` in an empty tmp workspace and reports it passed; U6's Files omitted that test file (9f5d036ceabc)
- g7's Goal-symbol lint lists tracked Python files with `git ls-files`, as U7 prescribes, and the plan-check guard in test_plan_edit.py patched `subprocess.run` wholesale, so git tripped a guard meant for codegraph and LLM calls (1b8bf51444af)
- g8-5 greps the run_executor.py source for the literal `: exit ` and g3's coder had split the f-string across two literals; the emitted line was correct all along and the gate held g8 one extra round over a formatting detail (g8-5)
- g4 updated one existing test that pinned the superseded own-task-to-`__run__` bucketing inside its own round, so the blast-radius gap bit a third unit without reaching the gate (g4-2)
- Seam bug between g1 and the run recipe, visible only to the live item g3-2: the new transcript census counted a run group's runner entry `g1-run-a1`, a shell command with no claude session, as transcript_missing and refused a clean run's export with exit 1, failing the two live tests that read ingest.json (g3-2)
- The run skill's launch snippet, pasted as one `&&` chain ending in `&`, backgrounds the whole chain and leaves RUN and PLAN unset in the parent shell; I recovered the run id from the process list and killed a stray wrapper shell before it overwrote the notes file (skills/orchestrator-run/SKILL.md)

## Run notes

- Preflight: clean tree at 78b4232, tool reinstalled from source, `uv sync --all-extras` green, codegraph synced before grouping, 8 code groups; g1 estimated 216k against the 200k budget and ran at 100k (g1/coder/gen1)
- Escalation 9f5d036ceabc (g6, preflight_failed): the fixture now creates `tests/test_x.py` in the harness workspace; committed ed616d0 on the integration branch only, verified 2 passed against g6's gate in its worktree, answered `retry` with no text (9f5d036ceabc)
- Escalation 1b8bf51444af (g7, preflight_failed): the guard raises only when argv names codegraph or claude and runs anything else for real; committed eb29a44 on integration only, 24 passed in g7's worktree, answered `retry` with no text (1b8bf51444af)
- g8's late surprise about g3 needed no operator action: the coder re-split the literal in run_executor.py in round 2 and the reviewer approved (g8/reviewer/gen1)
- Live item g5-2, run from the integration worktree with `--basetemp` under the run dir: 5 passed in 49s including the new two-follow-up termination test (g5-2)
- Live item g3-2, same setup: first pass 2 failed / 6 passed on the census seam above; the U3 run.log assertion itself passed. Census now skips `runner` rows (they still export flagged transcript_missing), regression test test_runner_session_without_transcript_is_not_missing added, 157 export-suite tests green, live re-run 8 passed in 60s; committed on integration after the merges (tests/test_run_recipe_live.py)
- Rescued git-ignored files were hook-left `.codegraph/` databases in every worktree; nothing the repo needs (g1)

## Next steps

- Deepen asks per unit which existing test asserts the behaviour the unit changes, and the plan lists it in Files: both gate failures and g4's in-round edit came from that omission, done when the deepen grill carries the question and a U6-shaped unit names its guard test (skills/orchestrator-deepen/SKILL.md)
  - how: add the question to the *Existence* paragraph beside the named-test hold
- Rewrite g8-5-style items to assert on the emitted log line or a format constant, never on source text: done when no verification item in a plan reads a `.py` file for a literal (g8-5)
  - how: a plan-check lint for `read_text()` of a `.py` path inside a `Run:` line
- Reinstall the tool before the next run so the shipped gate, lint and phase lines are the code that runs: done when `run` says nothing about a stale install (orchestrator/execution/driver_items.py)
- Fix the launch snippet in the run skill so the variables survive the backgrounding: done when Phase 1 sets RUN and PLAN on their own line and only the `setsid` command carries the `&` (skills/orchestrator-run/SKILL.md)

<!-- valid pointers: 1b8bf51444af, 78b4232a, 9f5d036ceabc, CONTEXT.md, c011d0cb, docs/orchestrator-grouping.md, docs/run-bundle-contract.md, g1, g1-1, g1-2, g1/coder/gen1, g2, g2-1, g2-2, g2-3, g2/coder/gen1, g3, g3-1, g3-2, g3/coder/gen1, g4, g4-1, g4-2, g4-3, g4/coder/gen1, g5, g5-1, g5-2, g5/coder/gen1, g6, g6-1, g6-2, g6-3, g6/coder/gen1, g7, g7-1, g7-2, g7-3, g7/coder/gen1, g8, g8-1, g8-2, g8-3, g8-4, g8-5, g8/coder/gen1, g8/reviewer/gen1, orchestrator/cli.py, orchestrator/execution/driver_items.py, orchestrator/execution/export.py, orchestrator/execution/generation.py, orchestrator/execution/liveness.py, orchestrator/execution/run_executor.py, orchestrator/execution/surprises.py, orchestrator/grouping/verification_lint.py, skills/orchestrator-deepen/SKILL.md, skills/orchestrator-run/SKILL.md, tests/test_export.py, tests/test_export_honesty.py, tests/test_goal_symbol_lint.py, tests/test_liveness_after_result.py, tests/test_named_test_hold.py, tests/test_plan_edit.py, tests/test_review_loop.py, tests/test_run_phase_lines.py, tests/test_run_recipe_live.py, tests/test_streaming_live.py, tests/test_surprise_board.py, tests/test_surprise_own_group.py, u1, u2, u3, u4, u5, u6, u7, u8 -->
