Orchestrator run `r20260828-220035` — Deterministic spec assembly, validation legibility, and the advisory grouper

## Motivation

Ship waves 1a–1c of the grouper-speccer-flow requirements so that after this
run the grouper is the well-working core of the planning→run flow:

- **Requirements**: R1, R5, R9, R22 (`docs/plans/2026-08-28-001-feat-deterministic-grouper-advisory-plan.md`)

## Changes

- [g1] deterministic-spec-assembly (`g1`)
- [g2] cycle-repair-withdrawal (`g2`)
- [g3] advisory-report (`g3`)
- [g4] merge-fill-penalty (`g4`)
- [g5] planning-contract-and-advise-phase (`g5`)
- [g6] layered-worker-context (`g6`)
- [g7] speccer-removal (`g7`)
- [g8] error-accumulation (`g8`)
- [g9] price-mode (`g9`)
- [g10] budget-naming (`g10`)
- [g11] partition-diagnostics (`g11`)

## Risks

- **Surprise (g4)**: A literal reading of the plan's 'candidate merge work lands inside a target band, preferred over one landing at the cap' is mathematically a no-op if implemented as a boolean in/out-of-band bucket ranked above merged_work: bucket-then-value sorting is always identical in outcome to sorting by value alone when the bucket is a threshold on that same value. Implemented as a distance-from-target-fill term instead (genuinely non-redundant, verified with a synthetic fixture). Worth knowing if U12 gets revisited or tuned in the eval harness — the boolean framing in the plan prose doesn't survive contact with the algorithm's per-round global-argmin selection. (`groups/g4/report-g1-r1.json`)
- **Surprise (g6)**: The U2 assembler's existing upstream/downstream contract labels were derived only from graph.dependencies (depends_on) edges, so a pure implements/consumes tag relationship with no direct dependency edge between the two specific tasks (common: both depend on a shared ancestor rather than on each other) would have produced no contracts line at all. Added a separate tag-matching pass for this rather than modifying the existing DAG-based Upstream/Downstream sections, to avoid breaking g1's R2 test (header must change when the DAG changes). (`groups/g6/report-g1-r1.json`)
- **Surprise (g7)**: ADR 0006 (docs/adr/0006-delete-the-grouping-time-speccer.md), referenced by my spec as required reading, does not exist in the repo — proceeded using the plan document's own Decisions section instead, which fully covers the rationale. (`groups/g7/report-g1-r1.json`)
- **Surprise (g7)**: The plan/spec's file list implied orchestrator/grouping/speccer.py could be deleted outright, but write_specs()/GroupSpec were still live dependencies of the mid-run rewrite speccer and (via GroupSpec) the deterministic assembler from U2/U3. Resolved by relocating the shared pieces (GroupSpec to model.py, the LLM-calling function inlined/renamed in cli.py) rather than deleting functionality still in use — worth knowing if another unit assumed speccer.py's removal was a pure deletion with no code to preserve. (`groups/g7/report-g1-r1.json`)

## Testing

- **g1**: 1432 test(s), 0 failure(s), 0 error(s) (`groups/g1/preflight-junit.xml`)
- **g2**: 1475 test(s), 0 failure(s), 0 error(s) (`groups/g2/preflight-junit.xml`)
- **g3**: 1445 test(s), 0 failure(s), 0 error(s) (`groups/g3/preflight-junit.xml`)
- **g4**: 1449 test(s), 0 failure(s), 0 error(s) (`groups/g4/preflight-junit.xml`)
- **g5**: 1449 test(s), 0 failure(s), 0 error(s) (`groups/g5/preflight-junit.xml`)
- **g6**: 1453 test(s), 0 failure(s), 0 error(s) (`groups/g6/preflight-junit.xml`)
- **g7**: 1453 test(s), 0 failure(s), 0 error(s) (`groups/g7/preflight-junit.xml`)
- **g8**: 1459 test(s), 0 failure(s), 0 error(s) (`groups/g8/preflight-junit.xml`)
- **g9**: 1482 test(s), 0 failure(s), 0 error(s) (`groups/g9/preflight-junit.xml`)
- **g10**: 1459 test(s), 0 failure(s), 0 error(s) (`groups/g10/preflight-junit.xml`)
- **g11**: 1471 test(s), 0 failure(s), 0 error(s) (`groups/g11/preflight-junit.xml`)

## Handoff

- **Report**: the full run record (`docs/runs/r20260828-220035/`)

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
