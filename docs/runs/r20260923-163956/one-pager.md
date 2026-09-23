# Split the review loop into responsibility modules with zero behaviour change — r20260923-163956

## TL;DR

- All 7 groups completed: `review.py` went from one 1,993-line class to a 226-line composition root over six mixin modules plus a host Protocol, with every existing test body unchanged (g5-2)
- Cost 1.33M tokens across 8 sonnet sessions (~$11.64); one preflight escalation and one driver restart, both caused outside the plan (9a9ce09cee54)
- The non-llm suite ends at 1943 passed; the live tier shows 20 passed and 2 failures that reproduce identically on the launch commit, so the split adds no live-tier regression (g7-6)

## Problems found

Three defects surfaced, none in the code this plan moved.

- `over_merge_ceiling`, added to `merge_small_groups` in 6e68605, was never added to the trace's `MergeReason` literal; any grouping hitting the merge headroom band crashed in `TraceRecorder` with a pydantic `ValidationError` (orchestrator/grouping/trace.py)
  test_cli_price groups a real plan against live file bytes, so g3's size changes pushed one merge into the band.
- The run started on a stale orchestrator: `smart-mcps-orchestrate` was a non-editable `uv tool` copy from 2026-09-07 (0.17.0), without the `Run (driver):` marker, so `group` wrote no `driver_run` flag and the gate held g7 on its live-tier item (g7-6)
- The report shows g7 as `changes_required` and u7 as not landed; that verdict is a stale `verdict-g1-r1.json` from the pre-restart session, which the resumed session's same-id report did not replace — g7 merged (g7)
- Two live tests already failed on the launch branch: a user-level `PreToolUse` hook runs `uv run` inside Landlock and hits EACCES on `~/.cache/uv`, masking the kernel denial; and `Bash(*python *)` now matches under Claude Code 2.1.280 (g7-6)

## Run notes

The driver fixed two things by hand and ran the one deferred check.

- 9a9ce09cee54 (preflight_failed, g3): classified as a launch-branch bug, not g3's diff; added `over_merge_ceiling` to the closed set plus a regression test, committed in g3's worktree and cherry-picked onto the integration branch, then `retry` with no text — gate re-ran and g3 merged (9a9ce09cee54)
- g7 round 1 held on g7-6 with the old build; driver stopped the run with SIGINT (work already committed), reinstalled the tool from the launch commit, set `driver_run` on g7-6 in both `groups.json` copies, and resumed; the fresh coder self-verified and merged (g7/coder/gen1)
- g6 and g7 exceeded the 200k estimate (331k, 299k) but were import-only edits; neither tripped the 250k breaker (g6/coder/gen1)
- Driver-run `uv run pytest -q -m llm` on the integration tip: 2 failed, 20 passed in 244s; both failures (`test_a_kernel_write_denial_does_reach_the_wire`, `test_a_leading_wildcard_alone_does_not_match`) fail identically on launch commit 00ba02a (g7-6)
- Before launch, `uv.lock` was committed to match the ruff/mdformat dev dependencies (00ba02af)

## Next steps

- Fix the live permission-pattern test against Claude Code 2.1.280: the recorded "`*python *` does not match" fact is now false, and the allowlist rules built on it need re-probing; done when the test encodes current CLI behaviour and passes (g7-6)
  - how: re-run the probe with `--setting-sources ''` and update the assertion plus `claude-cli-permission-rule-matching` notes
- Stop user-level hooks from polluting confined workers: the infinity-skills `PreToolUse` hook fails inside Landlock and hides real denials; done when the kernel-denial live test passes with the hook installed (g7-6)
  - how: run the live test with `--setting-sources ''` or grant the hook's uv cache read access in the worker allowlist
- Add a stale-install check to the run-driver preflight: a console script that imports an older `orchestrator` than HEAD silently runs old gates; done when preflight compares the tool's `orchestrator` against the checkout (g7)
- Make the report prefer the newest round artifact per group: a resumed session reusing `gen1/r1` leaves a stale verdict that misstates the outcome; done when g7 reports as landed (g7)
