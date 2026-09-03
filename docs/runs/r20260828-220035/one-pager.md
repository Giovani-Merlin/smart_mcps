# Deterministic spec assembly, validation legibility, and the advisory grouper — r20260828-220035

## TL;DR

- All 11 groups completed and merged on generation 1; base a2098a08 to tip 93dadc02, PR opened (g1)
- Zero-LLM-call spec assembly and the advisory grouper both landed as specced, closing R1 (R1)
- Legibility work (validation errors, contract labels) shipped across the run's later groups (g6)

## Problems found

- g4's plan-cap bucketing read as a mathematically redundant no-op; implemented as a distance-from-target-fill term instead, see groups/g4/report-g1-r1.json (g4)
- g6's upstream/downstream contract labels missed pure implements/consumes pairs with no direct dependency edge; added a separate tag-matching pass, see groups/g6/report-g1-r1.json (g6)
- g7 did not find ADR 0006, referenced by its spec as required reading, and proceeded from the plan's Decisions section instead, see groups/g7/report-g1-r1.json (g7)
- g7 found speccer.py's write_specs()/GroupSpec still live for the rewrite speccer and relocated rather than deleted them, see groups/g7/report-g1-r1.json (g7)
- The merge gate runs pytest only: g7's TypeScript changes merged with zero UI verification because the check command never reaches package.json when uv markers exist (ui/src/routes/Launch.tsx)

## Run notes

- This is the re-run of the same plan and partition after run r20260828-212053 died at g1; the two fixes in the launch commit gave a clean 1410-outcome baseline and g1 merged first try (a2098a08)
- No escalations, no respawns, no usage-limit pauses: every group merged on its first coder generation, about 2h45m wall clock (g1/coder/gen1)
- Checked g7's UI changes by hand against the integration branch since the gate never ran them: 199/199 vitest and a clean tsc (ui/src/types.ts)
- Verified the integration branch independently: 1482 pytest passed against 1410 on main, ADR 0006's speccer deletion confirmed, --price and --advise exercised live (93dadc02)
- The four over-budget groups (g3, g7, g10, g11) all landed on generation 1 without tripping the 250k breaker, so the estimator's over-budget flag is conservative rather than predictive (g7)

## Next steps

- Make the merge gate run the UI suite alongside pytest, with the baseline captured by the same compound command (ui/src/routes/Launch.tsx)
- Confirm the boolean-bucket framing in the plan prose is corrected before U12 is revisited or tuned in the eval harness (g4)
- Verify no other unit assumed speccer.py's removal was a pure deletion before building on it (g7)
