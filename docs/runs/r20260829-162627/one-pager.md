# Mechanical plan split and the deepen skill — r20260829-162627

## TL;DR

First live exercise of the deterministic grouper: the mapper and speccer LLM calls were both skipped, a three-group chain ran unattended, and every group merged on generation 1. The run's real lessons came after it, from a split defect and a verification hole that none of the run's own checks caught.

- All 3 groups completed and merged clean; base 5eca4f14 to tip a76fec68, PR opened (g1)
- Mechanical plan-split (R16, R17) and the deepen skill's edge-case grilling (R18, R19) both shipped as specced (R16)
- No escalations and no surprises recorded across any of the three groups (g3)

## Problems found

Three findings, all found by hand after the run rather than by the run: a P0 in split's output, a self-verify group that merged on its own report alone, and a reviewer that approved for a wrong reason.

- split's output failed plan-check on every real plan: a missing newline glued the task-map fence onto the first unit heading and the section tail after the units was dropped, found only after the run by hand (orchestrator/grouping/plan_edit.py)
  Thirty tests missed it because the fixture put the task map before the units, an ordering no real plan uses.
- g2 ran as self_verify and merged on its own report alone; item g2-5 claimed plan-check verifiable output that was never run (g2-5)
  Nothing in the merge decision read the report's verification results, so a report marking every item fail merged the same as one marking them pass.
- g3's reviewer ran the suite inside the Landlock sandbox, saw 5 phantom failures, and approved for a wrong stated reason (g3/reviewer/gen1)
  The reason given was "pre-existing", which the clean baseline contradicts; unconfined the same tree passes 1562 of 1562.

## Run notes

No escalations or respawns, so the driver's time went into independent verification and the two follow-up diagnoses, both fixed the next day and shipped as plugin 0.9.0.

- First live exercise of the deterministic grouper: the mapper and speccer LLM calls were both skipped and the three-group chain ran unattended, generation 1 each (g1/coder/gen1)
- Verified the integration branch by hand: 1562 pytest passed against 1505 on main (a76fec68)
- Diagnosed the split defect after the run against the real 2026-08-29 plan and fixed it the next day with the fixture reordered to the real section layout (orchestrator/grouping/plan_edit.py)
- Traced the self_verify hole to the merge decision reading only the report's status field, never its verification results; closed it the next day with a structural gate on required items (g2)
- g1's edit to plan_sections.py sat outside u1's declared file list; checked and kept, since the change is additive and g3 never touched the file (orchestrator/grouping/plan_sections.py)

## Next steps

Two of the three findings are already partly closed: the split fix and the structural verification gate landed on 2026-08-30. What remains is the evidence-quality half of the gate and the sandbox exemptions the reviewer finding calls for.

- Require each verification pass to name a test id and treat "manually verified" as skipped: the structural gate now blocks a missing or failed item, but g2-1's "manually verified" still satisfies it; done means a pass with no test id is demoted to skipped and the gate refuses the merge (g2-5)
  - how: a notes check next to the required-items check, plus dropping the pre-filled "pass" from the coder nudge skeleton
- Exempt the tests the Landlock sandbox breaks, or grant the sandbox what they need: a reviewer that learns to dismiss red tests is the habit that lets a real regression through; done means a confined full-suite run on a clean tree is green (g3/reviewer/gen1)
  - how: the REFER grant already fixed the cross-device half; the remaining test_cli.py failures need the pytest tmp-dir project slug allowlisted
- Add declared-but-untouched files to the merge gate: a group whose verification names a file its diff never touches is not verified, as g2's untouched test_advisory.py showed; done means the gate compares the task map's declared files against the merge diff (g2)
