# The run DAG is snapshotted into the run directory, not read from the shared file

`groups.json` is written to `<repo>/.orchestrator/groups.json` and overwritten by
every subsequent `group` invocation, so a run's DAG does not survive the next
planning cycle — a post-mortem view built from the shared file silently renders
some *other* run's group graph. We therefore copy `groups.json` into
`runs/<run_id>/groups.json` at run start (`_cmd_run`), make that copy the
authoritative source for readers, and fall back to the shared file with a
`stale_dag` flag only for runs recorded before this change. The alternatives —
always reading the shared file behind a "may be wrong" banner, or having the
Observatory copy the file on first read — either leave the post-mortem
requirement permanently unreliable or make a strictly-read-only observer mutate
the run directory while still capturing the wrong DAG for pre-existing runs. The
cost is one extra small write per run and a run-directory layout that now has a
fourth top-level artifact.
