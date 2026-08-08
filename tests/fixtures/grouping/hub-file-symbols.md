# fix: four units co-editing a hub file, each declaring symbols

Regression fixture for `docs/orchestrator-grouping.md` limitation 4 — the shape
that collapsed the 2026-07-29 correctness plan into one degenerate group.

Unlike every other fixture in this register, **this one declares `symbols`**, so
it is the only one that exercises the codegraph-derived edge layer at all. It is
served by the cassette in `tests/fixtures/codegraph_hub/` rather than by the
no-symbols stub runner.

The topology, minimised from the real failure: four units each own one module
*and* co-edit the shared entry point `app/cli.py`. Every unit's symbol is
referenced from `app/cli.py`, so `owners_of` resolves each unit's callers and
impact results to **every other unit** via file ownership. Read as precedence,
that is a near-complete digraph — one SCC, no acyclic partition but the
degenerate single group. Read as coupling (correct), it is four strongly
affine units with a single declared ordering edge.

The declared `depends_on` layer is deliberately sparse and acyclic: `verify`
comes after `merge`, and nothing else is ordered. The grouper must not invent
ordering beyond it.

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: merge-unit
    description: merge integrity checks
    files: [app/merge.py, app/cli.py]
    symbols: [Merger]
  - task_id: gate-unit
    description: failure gate
    files: [app/gate.py, app/cli.py]
    symbols: [Gate]
  - task_id: report-unit
    description: typed denial reporting
    files: [app/report.py, app/cli.py]
    symbols: [Reporter]
  - task_id: verify-unit
    description: end-to-end verification
    files: [app/verify.py, app/cli.py]
    symbols: [Verifier]
    depends_on: [merge-unit]
```
