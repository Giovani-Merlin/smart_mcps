# Plan-time discrete labels as the grouping semantic layer, not embeddings

The grouping pipeline needs semantic signals codegraph cannot provide — greenfield
plans have no code at graph time, and no reference edge connects a TS
`fetch("/api/x")` to its Python route. We add that layer as **plan-time discrete
labels** (slice must-links entering Louvain via node contraction, `implements`/
`consumes` route tags as clamp-normalized affinity, `depends_on` as ordering-only
edges) written by the planning session into a versioned task map
(`docs/orchestrator-task-map.md`), rather than symbol-name embeddings (CoCoder's
cosine proxy, which we deliberately dropped for edit plans — see
docs/research/design-deviations.md) or LLM-proposed group boundaries (granularity
drifts and isn't byte-stable). Labels are stable, human-reviewed, and
deterministic to consume; the trade-off is that plans without a task map get no
semantic layer at all and fall back to the LLM mapper.
