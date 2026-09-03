---
date: 2026-09-03
topic: run-ingestion-v2-and-framework-abstraction
---

# Run ingestion v2 + framework abstraction — Requirements

## Summary

Make orchestrator runs first-class, fully-enriched citizens of Infinity Skills,
and make Infinity Skills' ingestion and visualization pluggable per framework.
Two tracks: **smart_mcps** exports a v2 Run Bundle that matches the post-ADR-0007
reality (fresh workers, no base session) and carries everything the analysis side
needs (plan, specs, rewrite history, base-context strip markers); **infinity-skills**
gains a Framework Adapter abstraction (mapping-only ingester + registry + golden
fixtures), runs every ingested bundle through the *full* enrichment pipeline, and
gets a dedicated orchestrator tab. The end goal this enables (but does not build)
is wave 3: an eval dataset and automatic failure analysis computed over the
compressed corpus instead of raw jsonl.

## Problem Frame

- The v1 ingestion (2026-08-27) proved the contract idea but has two structural
  gaps found this session:
  - **Run-ingested observations get zero semantic enrichment.** Run
    `r20260820-213134`'s 1,762 obs have no `semantic_obs_type`, `intent_label`,
    `title`, no episodes — only `ingest-run` + `summarize` ever ran. Normal
    project sessions have ~10k enriched obs. The replay looks "superficial"
    because the enrichment stage was skipped, not because data was lost.
  - **ADR 0007 broke the dedup premise.** Workers now start fresh: no base
    session, and every worker transcript repeats the ~60KB base context with
    *new* uuids. The v1 uuid-dedup does nothing for any run launched after
    2026-08-29.
- The v1 contract predates several run features the analysis side needs:
  spec rewrites (the surprises/rewrite view has nothing to render), the plan
  document and assembled group specs (the wave-3 dataset seed), and the
  fresh-worker launch shape.
- Infinity Skills has exactly one ingester hard-wired to that v1 contract; a
  future framework (the planned research pipeline) would copy-paste it. The
  Perplexity prior-art pass (OTel GenAI, OpenInference/Phoenix) confirms the
  target pattern: small fixed canonical core + attributes-bag extensibility +
  per-source mapping adapters + contract tests; the known failure modes are
  over-rigid schemas, unversioned contracts, and adapters that interpret
  instead of map.
- Verified non-issue: the suspected "split doesn't write to docs/plans/" bug
  does not reproduce — `orchestrate split` writes next to the input plan, and
  drummAI's `score-corpus-part{1,2}` plans are in `docs/plans/` as expected.
  No requirement.

## Key Decisions

- **Per-framework contract + shared mapping API (Approach A).** Each framework
  exports its own rich, versioned Run Bundle; a mapping-only Framework Adapter
  inside Infinity Skills normalizes it to the fixed corpus. Rejected: one
  universal agent-run schema (loses framework structure, starves the framework
  tabs — the "just store JSON" failure mode's cousin).
- **Contract v2 only, no v1 support.** The base-session semantics genuinely
  changed (ADR 0007); v1 was a beginning. Old runs are re-exported as v2 if
  ever needed; the adapter refuses unknown versions loudly.
- **The bundle is a self-contained, harness-agnostic package.** The exporter
  parses its own harness's transcripts (Claude Code jsonl today, Codex or
  another harness tomorrow) into neutral events at export time; Infinity
  Skills ingests parsed events and never learns the raw format. Package layout:
  `<run_dir>/ingest/` — a small human-readable `ingest.json` manifest +
  `events/<session_id>.jsonl.gz` per session. Events are **uncapped** (full
  tool inputs/outputs); excerpting is the ingester's job. Rejected: pointing
  at raw jsonl paths (binds every consumer to the Claude Code format), one
  giant JSON file (unwieldy at 50–200MB).
- **Base-context dedup moves to export time (Base-Context Strip) — and it is
  dedup, not erasure.** Only the exporter knows the exact base-context bytes;
  it omits the prefix from each worker's parsed events and ships the base
  context once, keyed by sha256. Every worker session must still *show* the
  base context as its first input in the replay (a reference node linking to
  the shared blob) — without that the record lies about what the worker knew.
  Rejected: ingest-time content matching (heuristic, per-framework logic) and
  no-dedup (60KB × N workers of identical boilerplate in every session).
- **Full pipeline by default, one command.** `ingest-run` (or its wrapper)
  chains ingest → semantic enrichment → episodes → summarize → graph rebuild,
  with flags to skip stages. The enrichment gap must be impossible to hit
  silently again.
- **Fixed vs pluggable boundary.** Pluggable per framework: the contract, the
  adapter (bundle→rows mapping), summarizer prompt overrides (profiles with
  defaults, never logic forks), and an optional UI tab. Fixed: corpus schema,
  enrichment/clustering/mining algorithms, the replay engine.
- **Framework-specific data goes in a metadata bag, not core columns.** The
  existing `run_id`/`group_id`/`group_name`/`run_role`/`generation` columns are
  the last framework-flavored core additions; further detail lands in
  `meta_json`. (OTel span-plus-attributes pattern.)
- **Golden fixtures are the extension mechanism.** A new framework is added by
  an AI session following a template: contract doc + adapter subclass + golden
  fixture + contract test. The fixture test is the acceptance gate.
- **Framework tabs are pure views** over the canonical corpus + metadata — no
  per-framework data pipeline in the frontend or server.
- **Plan + specs travel in the bundle now** (wave-3 seed): cheap to carry,
  enables dataset queries later without resurrecting run dirs.
- **Everything serial.** No parallel-run visualization or ingestion concerns in
  scope; the orchestrator runs serial by default and the views assume it.

## Requirements

### Track S — smart_mcps

- R1. **Concise flow doc.** `docs/orchestrator-flow.md`: the lifecycle
  brainstorm → plan → deepen → split → group (deterministic Spec Assembly, no
  speccer) → run (serial scheduler, generations, review rounds, merge gate,
  mid-run rewrite speccer on failure) → resolve/finish → report → export, with
  who-calls-an-LLM-where and pointers to the owning ADRs. Short enough to stay
  maintained; updated when an ADR lands.
- R2. **Run Bundle contract v2 — self-contained package.** `orchestrate export` writes `<run_dir>/ingest/`: `ingest.json` manifest
  (`schema_version: 2`) + `events/<session_id>.jsonl.gz` of parsed,
  harness-neutral events (role, timestamp, text, tool name/input/output
  uncapped, error flag, event id) — no consumer ever touches raw Claude Code
  jsonl. Manifest carries: (a) fresh-worker shape — no base session; base
  context as one blob file + sha256; (b) Base-Context Strip applied in each
  worker's event stream (prefix omitted, strip recorded per session so the
  ingester can render the reference); (c) the plan document and each group's
  assembled spec (with grouping metadata: partition name, granularity); (d)
  rewrite history per group — the `spec-gen<N>.json` snapshots vs the
  original `groups.json` entry, with the triggering surprises/escalations;
  (e) the v1 join content (sessions, artifacts with status/surprises/
  denial_kind, escalations, `transcript_missing`). v1 emission is deleted.
- R3. **v2 validated against real post-0007 runs.** Export `r20260902-132128`
  (5 groups, smart_mcps); assert package shape, event parse fidelity against
  the source transcripts, strip applied with the base-context reference
  recorded, rewrite history where rewrites happened, and nulls (never
  invented values) where data is absent. No legacy fork-run support — runs
  before 2026-08-30 are out of scope.

### Track I — infinity-skills

- R4. **Metadata bag columns.** Additive migration: `meta_json` on `sessions`
  and `observations` (default `''`), written only by adapters; core columns
  frozen for framework-specific data from here on.
- R5. **Framework Adapter abstraction.** An abstract mapping-only ingester
  (session rows, observation rows, boundaries, artifacts — separate narrow
  methods), a registry keyed by the bundle's `framework` field, and a manifest
  per adapter (framework name, supported schema_version range, declared
  capabilities). Unknown framework or version fails fast with a named error.
  The orchestrator adapter is ported to this API as the first subclass,
  consuming contract v2 (including strip markers and rewrite history); the v1
  code path and uuid-dedup are deleted.
- R6. **Golden fixtures + contract tests + template.** Per adapter: at least
  one golden bundle fixture (happy path + an edge case: missing transcript,
  rewrite, denial) with expected corpus rows asserted. A short
  "adding a framework" doc + code template (typed method signatures, id/
  timestamp normalization helpers in the base class) so a single AI session can
  add a framework and be gated by the fixture test.
- R7. **Summarizer prompt profiles.** Per-framework prompt override snippets
  with the current prompts as default; the summarization algorithm itself stays
  shared. The orchestrator profile teaches the summarizer the run vocabulary
  (groups, generations, verdicts, surprises) — it does not feed artifacts as
  ground-truth input (still deferred, see Non-Goals).
- R8. **Full pipeline, one command.** Ingesting a bundle runs ingest →
  semantic enrichment → episodes → summarize → graph rebuild by default, stage
  skip flags available; cost is accepted. The UI or CLI flags sessions whose
  enrichment is missing. (No backfill of old runs — see R13.)
- R9. **Framework tab plumbing.** A frontend registry for optional
  per-framework tabs, each a pure view over server routes that read only the
  canonical corpus + metadata.
- R10. **Orchestrator tab v1.** Phased inside one tab: (a) run list → group
  board (runs table: id, plan, groups, outcome, tokens; per-run board of
  groups with state, generations, rounds) — the skeleton; (b) artifacts
  drill-in (coder reports and reviewer verdicts rendered structured, linking
  into session replay); (c) surprises & rewrites view (surprises with affected
  groups, spec-rewrite before/after, escalations with answers). All from R2
  data mapped by R5.
- R11. **Cross-run failure signals (last phase).** Aggregations across runs:
  denial kinds, error signatures, retirement reasons, surprise kinds by group
  name. Built only after ≥3 runs are ingested under v2; explicitly the wave-3
  on-ramp, kept read-only and simple.
- R12. **Plan + specs stored as run artifacts.** The bundle's plan document and
  group specs land in the corpus `artifacts` table tied to the run, queryable
  for later dataset building.
- R13. **Purge pre-2026-08-30 run data.** The v1-ingested run
  `r20260820-213134` (31 sessions, 1,762 un-enriched obs) is deleted from the
  corpus, and the v1 ingest path (uuid-dedup, jsonl-parsing run ingester) is
  removed with it. Nothing before 2026-08-30 matters — the orchestrator
  changed too much.
- R14. **infinity-skills becomes an orchestrator target.** `.orchestrator/ config.toml` created (serial, sonnet workers, `[workspace] data_dirs`
  covering `data/`), codegraph synced — so the Track I plan itself runs via
  `/orchestrator-run`, and that run becomes v2 ingestion material.
- R15. **Base-context visibility in replay.** Each worker session's timeline
  opens with a reference node ("shared base context", linking to the one
  ingested blob) so the strip never hides what the worker actually received.

## Non-Goals

- The research-pipeline framework itself — the abstraction must make it a
  one-session job later, but nothing research-specific is built now.
- Wave 3: the eval harness, dataset construction, automatic failure analysis.
  R11/R12 only seed it.
- Feeding artifacts (reports/verdicts) into summarizer prompts as ground truth
  — stays deferred per `docs/todos/orchestrator-infinity-skills-ingestion.md`.
- Any parallel-execution visualization or ingestion semantics.
- Backwards compatibility with contract v1 or with the fork-session (`--fork-base`)
  launch path's uuid dedup.
- A split-path change in `orchestrate split` (suspected bug did not reproduce).
- Changes to clustering/mining algorithms or the corpus core schema beyond R4.

## Open Questions

None — resolved during planning: the old run is purged (R13), not backfilled.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-09-03-run-ingestion-v2-and-framework-abstraction-requirements.md`
(likely as two plans, one per repo — Track S first, since Track I's adapter
consumes the v2 contract).
