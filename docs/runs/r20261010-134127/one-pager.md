# Fixes from run r20261007-100412 — driver-item hold, harness pin on success, untracked archive-first, data-step lock, overlap default, report commit pointers — r20261010-134127

## TL;DR

Nine serial groups closed the four seams the run-driver hit on infinity-skills run r20261007-100412, plus the smaller items from the same handoff. Every group completed; the only friction was two merge-gate failures on guard tests the plan did not list and one coder retired at the round cap.

- All nine groups completed and the plugin reads 0.23.0; the facts count u9 as not landed only because its second item is unverified in the gen-2 report, while the bump commit ce73f5e2 is on the branch (g5-6)
- Cost $10.42 across 13 sessions; g5 took 25 min and four sessions against 4–13 min and one or two sessions for every other group (g5/coder/gen1)
- Driver items are now recorded and hold dependents, untracked leftovers archive on the first strike, the harness pin is written only after a green smoke and reset by retry, a run-level lock serialises driver data steps and the gate, and overlap is the default failure policy (orchestrator/execution/scheduler.py)

## Problems found

Both gate failures and the retired coder trace back to the plan, not to the coders.

- The plan's Files omitted an existing drift test that pins every `EscalationKind` to the UI union, so g4's gate failed the moment it added the new kind (006a7b155a75)
- U11 depends on U2, so the unit that extends the enum landed before the unit that mirrors it; a unit extending a mirrored enum owns the mirror edit, or the mirror unit comes first (u11)
- The plan's Files omitted the e2e stub test that pinned the old two-strike untracked ladder, so g6's gate failed on it (047184290ce2)
- A required coder item "whole suite green" is unmeetable inside a confined worker: two baseline tests fail whenever pytest's basetemp sits inside the repo tree, which is every worker's TMPDIR, and the reviewer held g5's gate two rounds over it (tests/test_preflight_baseline.py)
- The reviewer's third round found a bug the unit text missed: optimize's pin lives at `groups/<gid>/eval/harness.sha256` and the retry reset only looked under `run/eval/` (g5/reviewer/gen1)
- The live harness-pin test asserted a manifest field that never existed: the artifact entry carries `measurements` only, never the record's `kpi_value`, `threshold_cleared` or `harness_hash` (tests/test_harness_pin_live.py)
- Session hooks leave `.codegraph/`, `.cursor/` and `__pycache__/` untracked in every scratch worktree, and the first-strike ladder now archives them with an informational surprise on every live run (g1-8)

## Run notes

Preflight was green on all seven checks, the plan was committed as the launch commit, and the run was launched serial with HITL at on_stuck and a four-hour timeout.

- Escalation 006a7b155a75: added `driver_items_pending` to the UI union and label map on integration only and released the gate with a text-less retry; g3 then found the mirror in place and added a label drift test instead (85165b95)
- Escalation 047184290ce2: reshaped the e2e test to the one-strike ladder, verified it green against g6's code by copying the file into g6's worktree and restoring it, committed on integration only, text-less retry (60c72b1b)
- Committed a rootdir pin for the baseline tests after g5's surprise, then reverted it when the gen-1 coder's own fix 4383862c conflicted in a trial merge, so the gate refresh stayed clean (0c57abc9)
- g5's gen-1 coder was retired at the three-round cap; the gen-2 coder fixed the optimize pin path with a test and was approved in one round (g5/coder/gen2)
- Driver items g1-8, g4-4 and g6-4 passed first time from the integration worktree, with the hold, record, release, data-step wait and archive lines all in their scratch runs' logs (g4-4)
- g5-7: reinstalled the tool with `--no-cache` from the integration worktree; `driver-item --help` and `data-step --help` both exit 0 from `$HOME` (g5-7)
- g5-4 failed once on its own assertion, passed in 25 s after the assertion was pointed at `measurements.score`; the seam (no pin after a failed smoke, retry and resume complete, no reset line) held on both runs (g5-4)
- Recorded all five items with the new `driver-item` subcommand; the status subcommand prints them, and the gen-1 g5 coder's stray comparison worktree under `.coder-scratch/` was pruned after the run (orchestrator/cli.py)

## Next steps

- Register the evaluation record's KPI fields in the artifact entry: reports and the Observatory see only `measurements` today, so an evaluate group's KPI value and threshold verdict are invisible; done when `artifacts.json` carries `kpi_value` and `threshold_cleared` and the live test asserts them (orchestrator/execution/evaluate_executor.py)
  - how: extend `_register_artifact` with optional record fields and the `ArtifactEntry` model
- Teach the plan skill to find existing guard tests: both gate failures were tests outside Files that pinned the behaviour a unit changed; done when plan-check warns for a test file that names a changed symbol or log line and is not in Files (u11)
- Exclude hook residue from the untracked ladder: `.codegraph/`, `.cursor/` and `__pycache__/` are never a coder's work; done when a scratch run archives nothing and spreads no surprise for them (orchestrator/execution/merge_ladder.py)
- Make "whole suite green" sandbox-provable: either give workers a TMPDIR outside the tree or keep in-tree-basetemp-sensitive tests out of required coder items; done when a confined coder's `uv run pytest -q` is green on main (tests/test_preflight_baseline.py)
- Reinstall the tool from main after this PR merges: the current install was built from the integration worktree and the stale-install check names that path (.claude-plugin/plugin.json)
- Settle g5-6 so the facts stop reporting u9 as not landed: either the gen-2 coder's unverified item is re-run by the driver, or landing math ignores items the driver recorded (g5-6)
