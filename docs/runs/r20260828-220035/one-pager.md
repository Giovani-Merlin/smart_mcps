# Deterministic spec assembly, validation legibility, and the advisory grouper — r20260828-220035

## TL;DR

- All 11 groups completed and merged; base a2098a08 to tip 93dadc02, PR opened (g1)
- Zero-LLM-call spec assembly and the advisory grouper both landed as specced, closing R1 (R1)
- Legibility work (validation errors, contract labels) shipped across the run's later groups (g6)

## Problems found

- g4's plan-cap bucketing read as a mathematically redundant no-op; implemented as a distance-from-target-fill term instead, see groups/g4/report-g1-r1.json (g4)
- g6's upstream/downstream contract labels missed pure implements/consumes pairs with no direct dependency edge; added a separate tag-matching pass, see groups/g6/report-g1-r1.json (g6)
- g7 did not find ADR 0006, referenced by its spec as required reading, and proceeded from the plan's Decisions section instead, see groups/g7/report-g1-r1.json (g7)
- g7 found speccer.py's write_specs()/GroupSpec still live for the rewrite speccer and relocated rather than deleted them, see groups/g7/report-g1-r1.json (g7)

## Next steps

- Confirm the boolean-bucket framing in the plan prose is corrected before U12 is revisited or tuned in the eval harness (g4)
- Verify no other unit assumed speccer.py's removal was a pure deletion before building on it (g7)

<!-- valid pointers: 93dadc02, R1, R22, R5, R9, a2098a08, docs/orchestrator-grouping.md, docs/orchestrator-task-map.md, g1, g1-1, g1-2, g1-3, g1-4, g1-5, g1-6, g1-7, g10, g10-1, g10-2, g11, g11-1, g11-2, g2, g2-1, g2-2, g3, g3-1, g3-2, g3-3, g3-4, g4, g4-1, g4-2, g5, g5-1, g5-2, g5-3, g5-4, g6, g6-1, g6-2, g6-3, g7, g7-1, g7-2, g7-3, g7-4, g8, g8-1, g8-2, g8-3, g9, g9-1, g9-2, g9-3, orchestrator/cli.py, orchestrator/config.py, orchestrator/grouping/advisory.py, orchestrator/grouping/assembler.py, orchestrator/grouping/base_context.py, orchestrator/grouping/errors.py, orchestrator/grouping/estimator.py, orchestrator/grouping/graphing.py, orchestrator/grouping/partition.py, orchestrator/grouping/pipeline.py, orchestrator/grouping/plan_reader.py, orchestrator/grouping/plan_sections.py, orchestrator/grouping/speccer.py, orchestrator/grouping/trace.py, orchestrator/model.py, orchestrator/observatory/launch.py, orchestrator/prompts/{speccer.md => rewrite_speccer.md}, skills/orchestrator-plan/SKILL.md, tests/fixtures/grouping/golden/brownfield-cross-stack.json, tests/test_advisory.py, tests/test_assembler.py, tests/test_cli_price.py, tests/test_cwd_contract.py, tests/test_e2e_stub.py, tests/test_fingerprint_compare.py, tests/test_grouper_pipeline.py, tests/test_grouping_trace.py, tests/test_observatory_launch.py, tests/test_partition.py, tests/test_plan_reader.py, tests/test_plan_sections.py, u1, u10, u11, u12, u13, u2, u3, u4, u5, u6, u7, u8, u9, ui/src/routes/Launch.tsx, ui/src/types.ts -->
