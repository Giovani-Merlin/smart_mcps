# Orchestrator → Infinity Skills ingestion — deferred work (post-v1)

v1 shipped (2026-08-27): `smart-mcps-orchestrate export <run_id>` writes a
framework-agnostic `<run_dir>/ingest.json` (`schema_version: 1`,
`orchestrator/execution/export.py`), and Infinity Skills gained
`infskills ingest-run <bundle>` (`src/infinity_skills/extract/run_bundle.py`) —
synthetic run session + boundary lane, base/group sessions as `sub_agent`
children, `--fork-session` shared-prefix dedup by message uuid, artifacts and
escalations as `framework:*` timeline events. Validated end-to-end against
drummAI `r20260820-213134` (13 groups, 30 sessions, all transcripts resolved).

Deferred, in rough priority order:

## 1. Dedicated orchestrator/framework tab in Infinity Skills

The v1 run view is the synthetic run session rendered by the existing Session
Replay tree. The contract's `framework` field already anticipates
multi-framework routing, and the `sessions` table now carries `run_id` /
`group_id` / `group_name` / `run_role` / `generation` — everything a dedicated
tab needs (run list → group board → session drill-in, DAG from `depends_on`).
Build it once the synthetic run session is confirmed useful in practice; don't
speculate on layout before that.

## 2. Structured artifacts as summarizer *input*

Reports/verdicts/surprises/denials are timeline events only (`meta_kind = framework:<kind>:<status>` rows). Feeding them into `summarize_session` prompts
would seed summaries with ground truth instead of inference (a coder session's
summary should open with its report's own status/summary), but per-data-type
prompt work is its own project — the summary prompt-tuning loop
(`eval/summary_prompt_tuning.py`) is the right harness for it.

## 3. Cross-run mining

The actual goal: "what do I keep messing up — git, paths, repeated tests".
Key mining by `group_name` / `denial_kind` / surprise `kind` across runs,
mirroring `skills/mine.py`'s `error_signature` bucketing. The denial kinds are
already attributed per artifact (`classify_denial` runs at export), and
surprises carry `affected_groups`. Needs ≥ a handful of ingested runs to be
worth tuning.

## 4. Retrieval scoping by run/group

Clone the `project` scoping pattern (`retrieval/anchors.py:179-198`) for
`run_id` / `group_id`, so "why did g4 fail" retrieves within that run's
sessions instead of the whole corpus.

## 5. Auto-export at `finish` time

`finish_run` could call `export_run` after teardown so every completed run has
its bundle without an operator step. Cheap; deferred only until the contract has
survived a few real consumers unchanged (bumping `schema_version` on a file
written automatically into every run dir is churn).

## Known v1 limits worth remembering

- The base session's `content_key` (base-context sha256) enables cross-run
  summary reuse, but only the copy-on-ingest form (`_reuse_base_summary`); the
  summarizer itself doesn't know about it.
- Artifact timeline events land at the *end* of the owning session's timeline,
  not at their true intra-session round boundary (artifacts carry no timestamp
  in the contract; round boundaries within one transcript aren't marked).
- A group with sessions but whose artifact matches no session by
  (role, generation) attaches the event to the group's last session.
- `ingest-run` is additive and idempotent, but a *renamed* run id re-ingests as
  a new run (session ids collide correctly; the synthetic `run:<id>` row does
  not).
