# Mechanical plan split and the deepen skill — r20260829-162627

## TL;DR

- All 3 groups completed and merged clean; base 5eca4f14 to tip a76fec68, PR opened (g1)
- Mechanical plan-split (R16, R17) and the deepen skill's edge-case grilling (R18, R19) both shipped as specced (R16)
- No escalations and no surprises recorded across any of the three groups (g3)

## Problems found

- split's output failed plan-check on every real plan: a missing newline glued the task-map fence onto the first unit heading and the section tail after the units was dropped, found only after the run by hand (orchestrator/grouping/plan_edit.py)
- g2 ran as self_verify and merged on its own report alone; item g2-5 claimed plan-check verifiable output that was never run (g2-5)
- g3's reviewer ran the suite inside the Landlock sandbox, saw 5 phantom failures, and approved for a wrong stated reason (g3/reviewer/gen1)

## Run notes

- First live exercise of the deterministic grouper: the mapper and speccer LLM calls were both skipped and the three-group chain ran unattended, generation 1 each (g1/coder/gen1)
- Verified the integration branch by hand: 1562 pytest passed against 1505 on main (a76fec68)
- Diagnosed the split defect after the run against the real 2026-08-29 plan and fixed it the next day with the fixture reordered to the real section layout (orchestrator/grouping/plan_edit.py)
- Traced the self_verify hole to the merge decision reading only the report's status field, never its verification results; closed it the next day with a structural gate on required items (g2)
- g1's edit to plan_sections.py sat outside u1's declared file list; checked and kept, since the change is additive and g3 never touched the file (orchestrator/grouping/plan_sections.py)

## Next steps

- Require each verification pass to name a test id, and treat "manually verified" as skipped, so the evidence-quality half of the self_verify gap closes too (g2-5)
- Exempt the tests the Landlock sandbox breaks, or grant the sandbox what they need, so reviewers stop learning to dismiss red tests (g3/reviewer/gen1)
- Add declared-but-untouched files to the merge gate: a group whose verification names a file its diff never touches is not verified (g2)
