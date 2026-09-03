## 2026-08-28 — r20260828-220035 — Deterministic spec assembly, validation legibility, and the advisory grouper

- **Outcome**: 11/11 groups completed, 13/13 units landed (`state.json`)
- **Scope**: 35 files changed, +4424/-419 lines (`a2098a08..93dadc02`)
- **Cost**: 2665188 tokens (+113432544 cache-read) across 22 session(s) (claude-opus-5=467038, sonnet=2198150) (`manifest.json`)

### g1: deterministic-spec-assembly — state: completed
- **Summary**: Implemented U1 (deterministic plan-section parser) and U2 (deterministic spec assembly replacing write_specs) with zero LLM calls in the grouping-time path.… (`g1`)
- **Verification**: 7/7 pass (`g1`)
- **Surprises**: none recorded (`g1`)
- **Required changes**: none (`g1`)
- **Escalations**: none (`g1`)
- **Tokens**: 391611 tokens (+25759264 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=349153) (`g1`)
- **Elapsed**: 27m (`g1`)

| item | status | evidence |
| --- | --- | --- |
| g1-1 | pass | tests/test_plan_sections.py::TestParsePlanSections::test_one_section_per_task_map_entry_verbatim and test_digest_has_every_summary_line_and_no_unit_bodies |
| g1-2 | pass | test_missing_single_unit_heading_names_it and test_two_missing_unit_headings_reported_together |
| g1-3 | pass | test_byte_stable_across_two_parses and test_task_map_absent_from_digest_and_sections |
| g1-4 | pass | tests/test_grouper_pipeline.py::TestTaskMapRegimes::test_premapped_plan_never_calls_the_llm_runner (StubLlm asserts on any non-mapper schema) plus full run_grouping/GroupingResult round-trip in TestPipeline |
| g1-5 | pass | tests/test_assembler.py::test_spec_contains_member_unit_sections_verbatim and test_relational_header_names_upstream_and_downstream_groups / test_regenerating_after_changed_partition_changes_the_header |
| g1-6 | pass | test_verification_ids_match_pattern_and_cover_every_bullet and test_missing_unit_verification_fails_naming_the_unit |
| g1-7 | pass | test_byte_deterministic_across_two_calls plus test_premapped_output_is_byte_deterministic in test_grouper_pipeline.py |

### g2: cycle-repair-withdrawal — state: completed
- **Summary**: Implemented U10: repair_cycles now withdraws inferred precedence edges (banked as affinity already, never declared ones) on a group-DAG cycle before… (`g2`)
- **Verification**: 2/2 pass (`g2`)
- **Surprises**: none recorded (`g2`)
- **Required changes**: none (`g2`)
- **Escalations**: none (`g2`)
- **Tokens**: 232103 tokens (+7449331 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=189645) (`g2`)
- **Elapsed**: 2h31m (`g2`)

| item | status | evidence |
| --- | --- | --- |
| g2-1 | pass | test_cycle_closed_only_by_an_inferred_edge_withdraws_instead_of_merging: group count unchanged, withdrawal recorded in trace repairs with action=withdraw and edge named |
| g2-2 | pass | test_cycle_of_solely_declared_edges_still_merges_and_never_withdraws: merge+re-split path taken, declared edges never withdrawn |

### g3: advisory-report — state: completed
- **Summary**: Found the group's implementation (advisory.py, cli.py wiring, pipeline.py graph-build seam, test_advisory.py) already substantially written from a prior session. Fixed two… (`g3`)
- **Verification**: 4/4 pass (`g3`)
- **Surprises**: none recorded (`g3`)
- **Required changes**: none (`g3`)
- **Escalations**: none (`g3`)
- **Tokens**: 136576 tokens (+3891409 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=94118) (`g3`)
- **Elapsed**: 41m (`g3`)

| item | status | evidence |
| --- | --- | --- |
| g3-1 | pass | TestOneGraphBuild covers single-build call counting and full per-preset metric+Pareto coverage; confirmed live via CLI smoke test |
| g3-2 | pass | TestCohesionShapes covers disjoint sets, pure chain, and clique fixtures |
| g3-3 | pass | TestPreviewIsolation + live smoke test confirm advisory.json under preview/ and groups.json untouched |
| g3-4 | pass | TestDeterminism (two-run byte-identical) and TestOneGraphBuild.test_zero_llm_calls (raising runner stub) |

### g4: merge-fill-penalty — state: completed
- **Summary**: Added a fill/balance term to merge_small_groups' merge key (orchestrator/grouping/partition.py), a new target_fill_ratio config knob (orchestrator/config.py) with an in-code justified default… (`g4`)
- **Verification**: 2/2 pass (`g4`)
- **Surprise (other)**: A literal reading of the plan's 'candidate merge work lands inside a target band, preferred over one landing at the cap' is mathematically a no-op if implemented as a boolean in/out-of-band bucket ranked above merged_work: bucket-then-value sorting is always identical in outcome to sorting by value alone when the bucket is a threshold on that same value. Implemented as a distance-from-target-fill term instead (genuinely non-redundant, verified with a synthetic fixture). Worth knowing if U12 gets revisited or tuned in the eval harness — the boolean framing in the plan prose doesn't survive contact with the algorithm's per-round global-argmin selection. (`groups/g4/report-g1-r1.json`)
- **Required changes**: none (`g4`)
- **Escalations**: none (`g4`)
- **Tokens**: 436732 tokens (+20296629 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=394274) (`g4`)
- **Elapsed**: 1h13m (`g4`)

| item | status | evidence |
| --- | --- | --- |
| g4-1 | pass | TestFillPenalty.test_fill_term_lowers_group_size_variance_without_breaking_invariants in tests/test_partition.py: synthetic two-hub fixture shows strictly lower pvariance under the new key, no group over cap, acyclic DAG (build_group_dag succeeds). |
| g4-2 | pass | test_fill_term_is_deterministic_across_runs (5 runs, identical); full suite run twice (1444 passed) confirms no invariant regressions across existing slice/budget-cap tests. |

### g5: planning-contract-and-advise-phase — state: completed
- **Summary**: Implemented U5 and U13: docs/orchestrator-task-map.md gained a new 'Pricing a task's node work' section with the exact node_work/cap formula and… (`g5`)
- **Verification**: 4/4 pass (`g5`)
- **Surprises**: none recorded (`g5`)
- **Required changes**: none (`g5`)
- **Escalations**: none (`g5`)
- **Tokens**: 115083 tokens (+997226 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=72625) (`g5`)
- **Elapsed**: 1h15m (`g5`)

| item | status | evidence |
| --- | --- | --- |
| g5-3 | pass | Phase 7 runs --advise after --no-spec, instructs presenting diagnostics, and phrases the choice as a question with proceed-as-one as an explicit option |
| g5-4 | pass | No auto-split/regenerate instruction added; the single 'deepen command' mention only states it doesn't exist yet, not that it's runnable |
| g5-1 | pass | docs/orchestrator-task-map.md states node_work = source_bytes/4.0 × 1.3 + per-file allowance, cap = 200,000 − head, names all multipliers, and points at group --price |
| g5-2 | pass | SKILL.md Phase 2 replaces the unconditional symbols instruction with the dense-codebase trade-off; Units section states Summary: requirement and Verification-bullet convention; template shows Summary: field |

### g6: layered-worker-context — state: completed
- **Summary**: Implemented U3 layered worker context: compile_base_context (orchestrator/grouping/base_context.py) now embeds the U1 plan digest (preamble + per-unit Summary lines + implements/consumes… (`g6`)
- **Verification**: 3/3 pass (`g6`)
- **Surprise (other)**: The U2 assembler's existing upstream/downstream contract labels were derived only from graph.dependencies (depends_on) edges, so a pure implements/consumes tag relationship with no direct dependency edge between the two specific tasks (common: both depend on a shared ancestor rather than on each other) would have produced no contracts line at all. Added a separate tag-matching pass for this rather than modifying the existing DAG-based Upstream/Downstream sections, to avoid breaking g1's R2 test (header must change when the DAG changes). (`groups/g6/report-g1-r1.json`)
- **Required changes**: none (`g6`)
- **Escalations**: none (`g6`)
- **Tokens**: 185196 tokens (+4317051 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=142738) (`g6`)
- **Elapsed**: 1h23m (`g6`)

| item | status | evidence |
| --- | --- | --- |
| g6-1 | pass | test_base_context_has_no_marker_or_heading and test_base_context_carries_digest_not_full_sections verify digest content, no full section bodies, no task-map marker, byte-identical on repeat compile |
| g6-2 | pass | test_cross_group_contract_names_tag_and_task_not_neighbor_section (assembler) verifies contracts line names tag + implementing task id and excludes the neighbor's full section text |
| g6-3 | pass | test_group_spec_plus_base_context_covers_its_own_sections_exactly_once verifies union contains each group's own unit sections exactly once and no other group's bodies |

### g7: speccer-removal — state: completed
- **Summary**: Deleted the grouping-time speccer per U4/ADR 0006 (speccer.py, prompts/speccer.md, the grouping-form model_speccer knob in launch.py/Launch.tsx/types.ts). The tricky part: write_specs and… (`g7`)
- **Verification**: 4/4 pass (`g7`)
- **Surprise (other)**: ADR 0006 (docs/adr/0006-delete-the-grouping-time-speccer.md), referenced by my spec as required reading, does not exist in the repo — proceeded using the plan document's own Decisions section instead, which fully covers the rationale. (`groups/g7/report-g1-r1.json`)
- **Surprise (interface_mismatch)**: The plan/spec's file list implied orchestrator/grouping/speccer.py could be deleted outright, but write_specs()/GroupSpec were still live dependencies of the mid-run rewrite speccer and (via GroupSpec) the deterministic assembler from U2/U3. Resolved by relocating the shared pieces (GroupSpec to model.py, the LLM-calling function inlined/renamed in cli.py) rather than deleting functionality still in use — worth knowing if another unit assumed speccer.py's removal was a pure deletion with no code to preserve. (`groups/g7/report-g1-r1.json`)
- **Required changes**: none (`g7`)
- **Escalations**: none (`g7`)
- **Tokens**: 252304 tokens (+16985843 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=209846) (`g7`)
- **Elapsed**: 1h50m (`g7`)

| item | status | evidence |
| --- | --- | --- |
| g7-1 | pass | grep finds zero references; both files confirmed absent |
| g7-2 | pass | GroupJobBody has no model_speccer field/argv; ExecutionOptions (run) keeps it; test_observatory_launch.py (33 tests) and full UI vitest suite (199 tests) green |
| g7-3 | pass | test_review_loop.py + test_rewrite_observability.py green (100 tests total with test_observatory_launch.py); run-form model_speccer knob and SpeccerCalls viewer untouched |
| g7-4 | pass | docs/orchestrator-grouping.md records commit d57c5cf as the removal commit |

### g8: error-accumulation — state: completed
- **Summary**: Implemented U6 error accumulation: added a shared ErrorAccumulator (orchestrator/grouping/errors.py) used by _check_slice_overflow in pipeline.py (collects every over-cap slice before raising)… (`g8`)
- **Verification**: 3/3 pass (`g8`)
- **Surprises**: none recorded (`g8`)
- **Required changes**: none (`g8`)
- **Escalations**: none (`g8`)
- **Tokens**: 167388 tokens (+3824013 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=124930) (`g8`)
- **Elapsed**: 1h57m (`g8`)

| item | status | evidence |
| --- | --- | --- |
| g8-1 | pass | test_three_over_cap_slices_all_named_in_one_error asserts all three slice labels, sums, and overshoots appear in one GrouperError |
| g8-2 | pass | test_two_unknown_key_tasks_reported_together and test_fixing_unknown_keys_then_reveals_both_size_hints_problems_together verify the two-phase behavior |
| g8-3 | pass | existing tests/test_plan_reader.py and tests/test_grouper_pipeline.py pass unmodified; added test_single_over_cap_slice_wording_is_unchanged asserts exact original message text |

### g9: price-mode — state: completed
- **Summary**: Implemented `group --price <plan>` (U7/C3/R6): a sub-second, zero-codegraph, zero-graph-build pricing mode. Added `parse_task_map_for_pricing` to plan_reader.py (filesystem-only task-map parse, no CodegraphClient,… (`g9`)
- **Verification**: 3/3 pass (`g9`)
- **Surprises**: none recorded (`g9`)
- **Required changes**: none (`g9`)
- **Escalations**: none (`g9`)
- **Tokens**: 301028 tokens (+13273880 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=258570) (`g9`)
- **Elapsed**: 2h45m (`g9`)

| item | status | evidence |
| --- | --- | --- |
| g9-1 | pass | TestOverCapSliceCli.test_exits_nonzero_and_prints_member_detail |
| g9-2 | pass | TestPriceNeverTouchesCodegraph — raising-runner stub never invoked; output states cap approximation |
| g9-3 | pass | TestRealPlanCrossCheck.test_price_is_subsecond_and_matches_the_real_trace against docs/plans/2026-08-26-...-plan.md |

### g10: budget-naming — state: completed
- **Summary**: Implemented U8: `_check_slice_overflow` in orchestrator/grouping/pipeline.py now takes coder_slack_multiplier, computes both the unscaled `node work` and the already-coder-scaled `coder work` per… (`g10`)
- **Verification**: 2/2 pass (`g10`)
- **Surprises**: none recorded (`g10`)
- **Required changes**: none (`g10`)
- **Escalations**: none (`g10`)
- **Tokens**: 129162 tokens (+2101235 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=86704) (`g10`)
- **Elapsed**: 2h0m (`g10`)

| item | status | evidence |
| --- | --- | --- |
| g10-1 | pass | overflow message now includes 'node work', 'coder work', and 'coder_slack_multiplier=<value>' per offending slice; verified in tests/test_grouper_pipeline.py::TestSliceOverflowAccumulation |
| g10-2 | pass | dry-run listing already said 'node work'; fixed the remaining bare 'work fraction of cap' in cli.py's scorecard print to 'node work fraction of cap' |

### g11: partition-diagnostics — state: completed
- **Summary**: Implemented U9: (a) the degenerate-partition GrouperError now reports declared-vs-inferred edge counts and inferred signal kinds for the offending SCC, sourced… (`g11`)
- **Verification**: 2/2 pass (`g11`)
- **Surprises**: none recorded (`g11`)
- **Required changes**: none (`g11`)
- **Escalations**: none (`g11`)
- **Tokens**: 318005 tokens (+14536663 cache-read) across 2 session(s) (claude-opus-5=42458, sonnet=275547) (`g11`)
- **Elapsed**: 2h18m (`g11`)

| item | status | evidence |
| --- | --- | --- |
| g11-1 | pass | test_run10_shape_names_slice_path_and_both_remedies in test_grouper_pipeline.py reproduces u1->u2->u3 with u1/u3 slice-mates through compute_partition and asserts the slice name, full path, both remedies, and absence of 'saturated'/'degenerate' generic wording. |
| g11-2 | pass | test_two_independent_reentrant_slices_reported_together asserts both slice names and both paths appear in one GrouperError ('2 problems found'). |

## Diagrams

### Plan → outcome

```mermaid
flowchart LR
    classDef ok fill:#d1f5d3,stroke:#2f9e44,color:#1a1a1a;
    classDef fail fill:#ffd6d6,stroke:#c92a2a,color:#1a1a1a;
    classDef resolved fill:#fff3bf,stroke:#e8a400,color:#1a1a1a;
    u_u1["u1- plan-sections — deterministic plan parsing- unit sections; summaries; digest"]
    grp_g1["g1- deterministic-spec-assembly"]
    u_u1 --> grp_g1
    u_u2["u2- spec-assembly — assemble group specs deterministically; replacing write_spec"]
    u_u2 --> grp_g1
    u_u3["u3- layered-context — digest in base context; contracts-only neighbors in specs"]
    grp_g6["g6- layered-worker-context"]
    u_u3 --> grp_g6
    u_u4["u4- speccer-removal — delete the grouping-time speccer in one recoverable commit"]
    grp_g7["g7- speccer-removal"]
    u_u4 --> grp_g7
    u_u5["u5- planning-contract-docs — Summary field; conventions; budget formula in the c"]
    grp_g5["g5- planning-contract-and-advise-phase"]
    u_u5 --> grp_g5
    u_u6["u6- error-accumulation — every validation phase reports all its failures (C1/R5)"]
    grp_g8["g8- error-accumulation"]
    u_u6 --> grp_g8
    u_u7["u7- price-mode — `group --price` prints the budget arithmetic sub-second (C3/R6)"]
    grp_g9["g9- price-mode"]
    u_u7 --> grp_g9
    u_u8["u8- budget-naming — one vocabulary for node work vs coder work (C2/R7)"]
    grp_g10["g10- budget-naming"]
    u_u8 --> grp_g10
    u_u9["u9- partition-diagnostics — inferred/declared provenance and slice-re-entry name"]
    grp_g11["g11- partition-diagnostics"]
    u_u9 --> grp_g11
    u_u10["u10- cycle-repair-withdrawal — group-DAG repair withdraws inferred edges before "]
    grp_g2["g2- cycle-repair-withdrawal"]
    u_u10 --> grp_g2
    u_u11["u11- advisory-report — `group --advise`- one graph; all granularities; cohesion "]
    grp_g3["g3- advisory-report"]
    u_u11 --> grp_g3
    u_u12["u12- fill-penalty — merge key prefers balanced fills over cap-filling (R19b expe"]
    grp_g4["g4- merge-fill-penalty"]
    u_u12 --> grp_g4
    u_u13["u13- plan-skill-advise-phase — /orchestrator-plan consults the advisory and asks"]
    u_u13 --> grp_g5
    grp_g1:::ok
    grp_g2:::ok
    grp_g3:::ok
    grp_g4:::ok
    grp_g5:::ok
    grp_g6:::ok
    grp_g7:::ok
    grp_g8:::ok
    grp_g9:::ok
    grp_g10:::ok
    grp_g11:::ok
```

## ADR delta

- **ADR delta**: no ADR changes (`a2098a08..93dadc02`)

## Postmortem

### Impact

- **Impact**: every unit landed despite the trouble below (`r20260828-220035`)

### Timeline

- **session_start**: orchestrator at 2026-08-28T20:01:43.533025+00:00 (`g1`)
- **session_start**: orchestrator at 2026-08-28T20:01:43.533025+00:00 (`g2`)
- **session_start**: orchestrator at 2026-08-28T20:01:43.533025+00:00 (`g3`)
- **session_start**: orchestrator at 2026-08-28T20:01:43.533025+00:00 (`g4`)
- **session_start**: orchestrator at 2026-08-28T20:01:43.533025+00:00 (`g5`)
- **session_start**: orchestrator at 2026-08-28T20:01:43.533025+00:00 (`g6`)

### Root-cause candidates

- **Surprise (g4, other)**: A literal reading of the plan's 'candidate merge work lands inside a target band, preferred over one landing at the cap' is mathematically a no-op if implemented as a boolean in/out-of-band bucket ranked above merged_work: bucket-then-value sorting is always identical in outcome to sorting by value alone when the bucket is a threshold on that same value. Implemented as a distance-from-target-fill term instead (genuinely non-redundant, verified with a synthetic fixture). Worth knowing if U12 gets revisited or tuned in the eval harness — the boolean framing in the plan prose doesn't survive contact with the algorithm's per-round global-argmin selection. (`groups/g4/report-g1-r1.json`)
- **Surprise (g6, other)**: The U2 assembler's existing upstream/downstream contract labels were derived only from graph.dependencies (depends_on) edges, so a pure implements/consumes tag relationship with no direct dependency edge between the two specific tasks (common: both depend on a shared ancestor rather than on each other) would have produced no contracts line at all. Added a separate tag-matching pass for this rather than modifying the existing DAG-based Upstream/Downstream sections, to avoid breaking g1's R2 test (header must change when the DAG changes). (`groups/g6/report-g1-r1.json`)
- **Surprise (g7, other)**: ADR 0006 (docs/adr/0006-delete-the-grouping-time-speccer.md), referenced by my spec as required reading, does not exist in the repo — proceeded using the plan document's own Decisions section instead, which fully covers the rationale. (`groups/g7/report-g1-r1.json`)
- **Surprise (g7, interface_mismatch)**: The plan/spec's file list implied orchestrator/grouping/speccer.py could be deleted outright, but write_specs()/GroupSpec were still live dependencies of the mid-run rewrite speccer and (via GroupSpec) the deterministic assembler from U2/U3. Resolved by relocating the shared pieces (GroupSpec to model.py, the LLM-calling function inlined/renamed in cli.py) rather than deleting functionality still in use — worth knowing if another unit assumed speccer.py's removal was a pure deletion with no code to preserve. (`groups/g7/report-g1-r1.json`)

### Follow-ups

- **Follow-ups**: none open (`r20260828-220035`)
