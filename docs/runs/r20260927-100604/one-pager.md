# Research, Evaluate and Optimize — three Unit Recipes and the KPI loop — r20260927-100604

## TL;DR

The plan registered three new Unit Recipes on the registry and moved each recipe's prompt, contract, tools and merge policy into the registry entry.

- All 11 groups completed and all 15 units landed, with no escalation raised; every group but g2 finished in its first generation (g1)
- The driver-run live items were the only evidence that exercised the new recipes for real, and they found five seam bugs that every coder, reviewer and unit test had passed; all five are fixed on this branch and both live files now pass (g3-4)
- The `research`, `evaluate` and `optimize` recipes, the Attempt Ledger and the keep-or-revert decision are in, with a real optimize loop keeping three candidates and merging its champion in one generation (g3-5)

## Problems found

The first five problems were invisible to the loop: each group's own tests passed, because the tests scripted the worker instead of running one.

- A worker report that failed to parse was re-nudged with the reviewer verdict skeleton whenever its contract was not exactly the coder's, so a live research worker answered `approved` twice and the group failed (orchestrator/execution/sessions.py)
- The research prompt never received the unit's question or output path, which live only in `recipe_args`, and it did not say the findings go inside the report body (orchestrator/prompts/research.md)
- A research worker that wrote its artifact but skipped the commit failed the merge, because the globs check read the collapsed `docs/` directory name and the merge refuses a branch with no commits (orchestrator/execution/merge_ladder.py)
- The optimize loop left the harness's `measurements.json` in the worktree: every live loop failed its first merge gate, relaunched a second generation past its evaluation budget, and `git add -A` had been sweeping the file into every candidate commit (orchestrator/execution/optimize_executor.py)
- The optimize loop logged "merged champion" right after a merge that had just failed (g3-5)
- Two plan verification items were wrong as written: g2-7 was a `bash -c` shape check writing to `/tmp` that the sandbox refuses, and g3-2 named `ingest.json` where export writes `ingest/ingest.json` (g2-7)
- The per-group token totals for g4, g5, g9 and g11 read under 10k tokens for sessions of 17 to 33 minutes, so the run's cost figure undercounts (g11)

## Run notes

The run was interrupted three times, none by a group's own work, and each time the stranded progress survived.

- g2 was interrupted by a harness-allowlist denial on item g2-7 after its coder had committed the whole unit; the driver reclassified the item as driver-run in the run's groups file, ran it by hand (it printed the expected score), and resumed, costing one fresh coder (g2/coder/gen1)
- A usage limit paused the run for four hours during g6; the orchestrator resumed on its own when the limit reset (g6)
- A machine shutdown killed the run mid-g10 with uncommitted work in six files; the driver left the worktree untouched, resumed, and the same coder session warm-resumed and finished (g10/coder/gen1)
- The installed orchestrator CLI was a stale copy without `--plan`; the first launch died at argument parsing, and the driver reinstalled and regrouped, which is what tagged the five driver-run items (g3)
- The driver ran all six driver-run items: g2-7, g4-2 and g10-4 passed as written, g3-2 passed on the corrected path, and g3-4 and g3-5 passed after the fixes below (g4-2)
- The live research check took four attempts, one per seam bug plus a test that read the artifact from the main checkout, and the live optimize check found the measurements and merge-log bugs; the fixes are four commits with three new test files (tests/test_recipe_nudge.py)
- The live tamper test cannot force a real worker to cheat, since it refused and obeyed the optimize prompt; it now asserts that the harness never reaches the integration branch, and the scripted tests still prove the discard path (tests/test_recipes_live.py)
- After the run the driver closed the plan-side findings: `plan-check` now lints every `Run:` item for allowlist gaps, missing paths and any `/tmp` use, `run` and `resume` refuse a stale install, and the worker prompts and skills say scratch goes to `.coder-scratch/`, never `/tmp`; the full suite reads 2208 passed (tests/test_verification_lint.py)

## Next steps

- Run the two planned live runs on this build: an Infinity Skills analysis pipeline first, then a grouping KPI loop in this repo; done when each merges with its driver-run items passed (R12)
  - how: reinstall the CLI from main after merge, then /orchestrator-plan in infinity-skills
- Find why the token totals for g4, g5, g9 and g11 undercount; done when each group's total matches its transcript usage (orchestrator/report/facts.py)
- Reinstall the orchestrator from main once this merges, since the new stale-install guard refuses the current copy; done when `run --help` works and a launch passes the guard (skills/orchestrator-run/SKILL.md)
