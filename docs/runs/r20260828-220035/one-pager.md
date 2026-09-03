# Deterministic spec assembly, validation legibility, and the advisory grouper — r20260828-220035

## TL;DR

This is the re-run of the plan and partition that run r20260828-212053 abandoned when its first group failed terminally. Two fixes in the launch commit gave a clean 1410-outcome baseline, and the same eleven groups then ran unattended for about 2h45m.

- All 11 groups completed and merged on generation 1; base a2098a08 to tip 93dadc02, PR opened (g1)
- Zero-LLM-call spec assembly and the advisory grouper both landed as specced, closing R1 (R1)
- Legibility work (validation errors, contract labels) shipped across the run's later groups (g6)

## Problems found

Five findings, none of them blocking: two are design deviations the coders documented in their own reports, two are gaps a group worked around, and one is a merge-gate hole the run exposed rather than caused.

- g4's plan-cap bucketing read as a mathematically redundant no-op; implemented as a distance-from-target-fill term instead, see groups/g4/report-g1-r1.json (g4)
  The coder argued the case in its report rather than silently diverging, which is the behaviour the ground rules ask for.
- g6's upstream/downstream contract labels missed pure implements/consumes pairs with no direct dependency edge; added a separate tag-matching pass, see groups/g6/report-g1-r1.json (g6)
- g7 did not find ADR 0006, referenced by its spec as required reading, and proceeded from the plan's Decisions section instead, see groups/g7/report-g1-r1.json (g7)
- g7 found speccer.py's write_specs()/GroupSpec still live for the rewrite speccer and relocated rather than deleted them, see groups/g7/report-g1-r1.json (g7)
- The merge gate runs pytest only: g7's TypeScript changes merged with zero UI verification because the check command never reaches package.json when uv markers exist (ui/src/routes/Launch.tsx)
  The launch-commit baseline is pytest-only for the same reason, so a UI regression is invisible on both sides of the comparison.

## Run notes

No escalations, no respawns, no usage-limit pauses: the driver's work was the launch, the watch, and an independent verification of the integration branch that the gate's pytest-only check never performed for the UI.

- This is the re-run of the same plan and partition after run r20260828-212053 died at g1; the two fixes in the launch commit gave a clean 1410-outcome baseline and g1 merged first try (a2098a08)
- No escalations, no respawns, no usage-limit pauses: every group merged on its first coder generation, about 2h45m wall clock (g1/coder/gen1)
- Checked g7's UI changes by hand against the integration branch since the gate never ran them: 199/199 vitest and a clean tsc (ui/src/types.ts)
- Verified the integration branch independently: 1482 pytest passed against 1410 on main, ADR 0006's speccer deletion confirmed, --price and --advise exercised live (93dadc02)
- The four over-budget groups (g3, g7, g10, g11) all landed on generation 1 without tripping the 250k breaker, so the estimator's over-budget flag is conservative rather than predictive (g7)

## Next steps

Ordered by the damage each does if left: the gate hole first, since it makes every UI change in every future run unverified, then the two plan-prose corrections that stop the next plan from repeating the same misreadings.

- Make the merge gate run the UI suite alongside pytest: today a TypeScript regression is invisible on both sides of the baseline comparison; done means a compound check command, the baseline captured by that same command, and a UI-only red merge refused (ui/src/routes/Launch.tsx)
  - how: make `[preflight] check_command` a list or a compound command in config.toml and reuse it for baseline capture, so the two sides stay comparable
- Correct the boolean-bucket framing in the plan prose before U12 is revisited: the redundant no-op reading is what the g4 coder had to argue past; done means the plan states the distance-from-target-fill term the code implements, before the eval harness tunes it (g4)
- Verify no other unit assumed speccer.py's removal was a pure deletion: write_specs()/GroupSpec are still live for the rewrite speccer; done means every consumer of the relocated symbols is checked and ADR 0006's wording matches what actually remains (g7)
