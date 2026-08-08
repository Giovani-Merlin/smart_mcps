# Golden partition baselines (plan U5)

One `<fixture-name>.json` per fixture in the register
(`tests/test_grouping_fixtures.py::ALL_FIXTURES`), each holding that fixture's
partition, group count, and per-group work against the budget cap at default
settings. `tests/test_golden_partitions.py` recomputes every fixture's
partition and fails, printing both sides, if it differs from its committed
baseline — the three drift directions a byte-stability test (which only
compares two runs of the *same* code) cannot see: a fixture grouping into a
different number of groups, a task moving from one group to another, or a
group's summed work crossing the budget cap.

## Regenerating

A deliberate behaviour change (e.g. a partitioner fix) should update these
baselines as a **reviewable diff**, not silently. Regenerate all of them with:

```
uv run python tests/regenerate_golden_partitions.py
```

Then re-run `uv run pytest tests/test_golden_partitions.py` and review the
resulting diff under this directory before committing.
