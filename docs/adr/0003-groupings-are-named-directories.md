# A grouping is a named directory, selected explicitly at run time

`group` wrote `.orchestrator/groups.json` and `.orchestrator/base-context.md`,
and `run` read exactly those paths, so grouping a second plan destroyed the
first grouping with no record — the loss ADR 0002 papers over with a per-run
snapshot, and the reason the Observatory's front end had no stable way to name
what it was rendering. A grouping is therefore a self-contained directory,
`.orchestrator/groupings/<name>/` (`groups.json`, `base-context.md`,
`grouping-trace.json`), written by `group --name <tag>` and chosen by
`run --grouping <tag>`; `run` auto-selects only when exactly one grouping
exists and otherwise errors listing the candidates, because "use the newest"
is the implicitness this change exists to remove. The alternatives — flat
suffixed filenames, or keying groupings by plan path — either leave the
per-grouping artifact set implicit as sidecars accumulate, or make it
impossible to hold two alternative groupings of the same plan side by side.
The costs are a migration (nothing writes the top-level `groups.json` any
more; a stale one is reported rather than consumed) and one extra directory
copy per run, since ADR 0002's premise survives naming: `group --name <same>`
still rewrites a finished run's history unless the run snapshots what it used.

**Amendment 2026-09-25.** `run --plan <path>` selects the grouping whose
`groups.json` records that plan (the run-driver holds the plan path, not the
grouping name). It is a lookup, not a key: two groupings built from the same
plan still error and ask for `--grouping <name>`, so the alternative-groupings
premise above is untouched.
