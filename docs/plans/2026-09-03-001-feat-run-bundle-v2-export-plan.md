---
title: Run Bundle v2 — self-contained package export
type: feat
date: 2026-09-03
origin: docs/brainstorms/2026-09-03-run-ingestion-v2-and-framework-abstraction-requirements.md
---

# Run Bundle v2 — self-contained package export

## Objective

Track S of the origin doc: replace the v1 `ingest.json` single-file export with
the v2 self-contained package (R2), validated against a real post-0007 run
(R3), plus the concise lifecycle doc (R1). After this plan, `smart-mcps-orchestrate export <run_id>` writes `<run_dir>/ingest/` — a manifest plus parsed,
harness-neutral, base-context-stripped event files — and no consumer ever needs
to read Claude Code jsonl. Track I (the Infinity Skills adapter, R4–R15) is a
separate plan in the infinity-skills repo consuming this contract.

## What we already know (resolved context)

- **v1 exporter exists**: `orchestrator/execution/export.py` (~370 lines,
  pydantic models `RunExport`/`ExportGroup`/`ExportSession`/`ExportArtifact`/
  `ExportEscalation`, `SCHEMA_VERSION = 1`), CLI wiring in `orchestrator/cli.py`
  (`export` subparser at ~line 428, `_cmd_export` at ~line 2867), tests in
  `tests/test_export.py` (10 tests, fixture run dirs). v2 **replaces** v1 —
  no dual support; rework these files, don't add parallel ones.
- **Worker first prompt** is exactly `f"{base_context}\n\n{prompt}"`
  (`SessionRunner.start_worker`, `orchestrator/execution/sessions.py:460`) —
  so a worker transcript's first user message begins with the byte-exact base
  context, and stripping is a verified prefix match, never a heuristic.
- **Base context text** is at `<run_dir>/base-context.md` (snapshot taken at
  run start). Post-ADR-0007 runs have `manifest.base_session_id = None` and
  spawn no base session at all.
- **Rewrite history source**: `_persist_rewritten_spec`
  (`orchestrator/execution/review.py:1381`) writes
  `groups/<gid>/spec-gen<N>.json` — the full rewritten `Group` model at
  generation N. The original spec is the group's immutable entry in the run
  dir's `groups.json`. `latest_spec_gen` / `effective_group`
  (`orchestrator/execution/manifest.py:259-282`) already read these back.
- **Escalations**: `request-<id>.json` / `response-<id>.json` pairs exist both
  under `<run_dir>/escalations/` and (newer runs, e.g. r20260902-132128 g3)
  under `groups/<gid>/` — the v2 collector must glob both locations.
- **Artifacts**: `groups/<gid>/report-g<G>-r<R>.json` and
  `verdict-g<G>-r<R>.json` (`_ARTIFACT_RE` in export.py); `heartbeat.json`,
  `preflight-*`, `provisioning.json` are skipped. Denial kinds come from
  `orchestrator/execution/denial.py:classify_denial`, derived at export.
- **Transcript location**: `~/.claude/projects/<slug>/<session_id>.jsonl`;
  v1's `_resolve_transcript` (recorded path, else glob by session id) stays
  the discovery mechanism — v2 just parses what it finds instead of pointing
  at it.
- **Claude Code jsonl shape** (as parsed by infinity-skills today): one JSON
  object per line with `type` (user/assistant), `uuid`, `timestamp`,
  `message.content` (list of `{type: text|tool_use|tool_result, ...}` blocks);
  tool results reference `tool_use_id`. The neutral event schema must be
  derivable from this without importing anything from infinity-skills.
- **Reference run for validation**: `.orchestrator/runs/r20260902-132128` —
  post-0007 (base_session_id None), 5 groups, sessions
  g1(coder,reviewer) g2(coder×3 rounds) g3(coder gen1+gen2, 3 escalation
  pairs in the group dir) g4(coder,reviewer) g5(coder). It already contains a
  stale v1 `ingest.json`, which the v2 export leaves in place (different
  path: `ingest/`).
- **Landlock never restricts reads** (`orchestrator/execution/confinement.py`
  — only write-class rights enter `handled_access_fs`), so a worker's
  verification commands may read this repo's real run dir by absolute path;
  writes must land inside the worktree (use `--out`).
- **`smart-mcps-orchestrate` CLI** is installed in the dev venv (`uv run smart-mcps-orchestrate ...` from the repo root works in a worktree after
  provisioning).
- **The `report` subcommand (ADR 0008) is NOT on `main` yet** — it lives on
  the unmerged branch `orchestrator/run-r20260902-132128` (PR #7, open).
  Sequencing preference: merge PR #7 before running this plan (it touches
  `orchestrator/cli.py`, which U3 also edits); if it is not merged, U1's doc
  marks `report` as pending and U3 expects a later conflict on `cli.py`
  when PR #7 lands.
- Lifecycle stages for the flow doc, with owners: `/orchestrator-brainstorm` →
  `/orchestrator-plan` (task map, `docs/orchestrator-task-map.md`) →
  `/orchestrator-deepen` → `split` (mechanical, `plan_edit.py`) → `group`
  (deterministic Spec Assembly, speccer deleted — ADR 0006) → `run`
  (`scheduler.py` serial cap, `review.py` generations/rounds/merge-gate,
  mid-run rewrite speccer on failure, surprises via `SurpriseBoard`) →
  `resolve`/`finish` (`finish.py`, ADR 0004) → `report` (ADR 0008) →
  `export` (this plan). LLM call sites: planning skills (interactive), worker
  sessions, rewrite speccer, report one-pager slots — everything else is
  deterministic.

## Decisions

- **The exporter owns transcript parsing.** smart_mcps parses Claude Code
  jsonl into neutral events at export time; the bundle is self-contained and
  harness-agnostic. Rejected: shipping raw jsonl paths (binds every consumer
  to the harness format; the ingester would need a per-harness parser
  forever).
- **Package directory, not a single file.** `<run_dir>/ingest/` with a small
  `ingest.json` manifest and `events/<session_id>.jsonl.gz` per session —
  streamable, inspectable, one session re-exportable. Rejected: one giant
  JSON (50–200MB, unwieldy, no partial read); tar archive (no per-session
  streaming).
- **Events are uncapped.** Full tool inputs and outputs travel in the bundle;
  excerpting/capping is the ingester's job (its raw table keeps what the UI
  truncates). Rejected: export-time caps (repeats the "too superficial"
  mistake at the contract layer, and the cap would be unrecoverable).
- **Strip is dedup, not erasure.** The base-context prefix is omitted from a
  worker's event stream only when it byte-matches, and the manifest records
  per session that (and how much) was stripped, so the consumer can render a
  "shared base context" reference at the head of the timeline. A non-matching
  first message is exported whole with `base_context_stripped: false` — never
  a partial or fuzzy strip.
- **v2 only, v1 deleted.** The schema version jumps to 2 and the v1 writer is
  removed in the same change. Rejected: dual emission (nothing consumes v1;
  the origin explicitly writes off pre-2026-08-30 runs).
- **No new ADR.** The reversible-cost test fails: the contract is versioned
  precisely so it can change, and ADR 0007 already records the launch-shape
  decision this contract mirrors.

## Units

### U1. flow-doc — the concise orchestrator lifecycle document

- **Summary**: [docs] `docs/orchestrator-flow.md` documents the full
  brainstorm→plan→deepen→split→group→run→resolve/finish→report→export
  lifecycle, naming where LLMs run and which ADR owns each design turn.
- **Goal**: A newcomer (or the owner after a month away) reads one short doc
  and knows what each stage does, which CLI subcommand or skill drives it,
  where the only LLM call sites are, and which ADR to read for any "why".
  Kept deliberately short (target ≤ 150 lines) so it stays maintained.
- **Files**: `docs/orchestrator-flow.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: —
- **Verification**:
  - Every CLI subcommand the doc presents as currently available exists in
    the CLI.
    Run: `uv run smart-mcps-orchestrate --help`
    Pass: the doc names at least `group`, `run`, `resume`, `finish`,
    `export`, `split`, and each subcommand the doc presents as available
    appears in the help output. The `report` stage (ADR 0008) is described
    but explicitly marked as landing with PR #7 if `report` is still absent
    from the help output — the doc must not claim it is available when it
    is not.
  - The doc names each lifecycle stage's driver (skill or subcommand) and
    cites ADRs 0006, 0007, and 0008 at the stages they govern.
    Run: `grep -c "adr/000[678]" docs/orchestrator-flow.md`
    Pass: count ≥ 3, and a read-through finds brainstorm, plan, deepen,
    split, group, run, resolve, finish, report, and export each covered
    (report as a stage, availability-caveated as above).
- **Edge cases**: —
- **Non-goals / must-not**: —

### U2. event-parser — Claude Code jsonl → neutral events with base-context strip

- **Summary**: [export] New `orchestrator/execution/transcript_events.py`
  parses a Claude Code transcript into harness-neutral events (role,
  timestamp, text, tool name/input/output uncapped, error flag, event id) and
  can strip a byte-exact base-context prefix from the first user message,
  reporting what it stripped.
- **Goal**: `parse_transcript(path, *, strip_prefix: str | None = None) -> ParsedTranscript` where `ParsedTranscript` carries `events: list[NeutralEvent]` and `strip: StripResult` (`applied: bool`,
  `char_len: int`, `sha256: str` when applied). `NeutralEvent` is a pydantic
  model: `event_id` (source uuid), `role` (`user|assistant|tool`),
  `timestamp`, `text`, `tool_name`, `tool_use_id`, `tool_input` (JSON,
  uncapped), `tool_output` (uncapped), `is_error`. Tool results pair to their
  `tool_use_id`. `strip_prefix` matches only when the first user message's
  text starts with the exact string (the launch composes
  `base_context + "\n\n" + prompt`, sessions.py:460); on match the prefix
  (plus the joining blank line) is removed from that event's text, on
  mismatch nothing is altered and `applied` is False. Serialization helper
  writes/reads `events/<session_id>.jsonl.gz` (one event per line, gzip).
  Zero imports from infinity-skills; stdlib + pydantic only.
- **Files**: `orchestrator/execution/transcript_events.py` *(new, large)*,
  `tests/test_transcript_events.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: bundle-v2
- **Implements / Consumes**: implements `NeutralEvent`
- **Verification**:
  - Unit tests cover: text/tool_use/tool_result block mapping, tool-result
    pairing by `tool_use_id`, error results setting `is_error`, strip applied
    on exact prefix, strip refused on near-miss (one byte differs), empty and
    malformed lines skipped without raising, gzip round-trip equality.
    Run: `uv run pytest tests/test_transcript_events.py -q`
    Pass: all tests green.
  - A real transcript parses with full fidelity (external oracle: the live
    session store, readable from any worktree).
    Run: a test marked with the existing live tier (or a verification
    script) that globs one real `*.jsonl` under `~/.claude/projects/` with ≥
    50 lines, parses it, and compares.
    Pass: every source line whose `type` is user/assistant yields ≥ 1 event;
    every `tool_use` id in the source appears as an event's `tool_use_id`;
    no event text is truncated (longest `tool_output` equals the source
    block's length).
- **Edge cases**: —
- **Non-goals / must-not**: —

### U3. package-writer — v2 export writes `<run_dir>/ingest/`

- **Summary**: [export] `orchestrate export` reworked to write the v2
  package: `ingest/ingest.json` manifest (schema_version 2 — groups/sessions
  join, artifacts, escalations from both locations, plan doc + assembled
  specs + grouping metadata, rewrite history from `spec-gen<N>.json`,
  base-context blob + per-session strip record) plus
  `ingest/events/<session_id>.jsonl.gz` parsed via the neutral-event parser;
  the v1 single-file writer is deleted.
- **Goal**: `export_run` produces `<run_dir>/ingest/` (or `--out DIR`):
  `ingest.json` with `schema_version: 2`, no `base_session` key, and —
  new over v1 — `base_context: {path: "base-context.md", sha256, char_len}`;
  per session `events_path` (relative), `events_count`,
  `base_context_stripped: bool`, `transcript_missing` retained; per group
  `spec` (the assembled spec from the run dir's `groups.json` entry) and
  `rewrites: [{generation, spec, triggering_surprises, escalation_ids}]`
  built from `groups/<gid>/spec-gen<N>.json` (empty list when none);
  top-level `plan: {path, text}` (the plan doc read via `manifest.plan_path`)
  and `grouping: {name, granularity}` where recorded (null where not);
  escalation pairs collected from both `<run_dir>/escalations/` and
  `groups/<gid>/`. Old-run tolerance stays: absent data is null, never an
  invented value. v1 code paths (`SCHEMA_VERSION = 1` writer, transcript
  path-pointer output) are removed; `tests/test_export.py` is rewritten
  against v2.
- **Files**: `orchestrator/execution/export.py`, `orchestrator/cli.py`,
  `tests/test_export.py`
- **Symbols**: —
- **Depends-on**: U2
- **Slice**: bundle-v2
- **Implements / Consumes**: implements `ingest/v2`; consumes `NeutralEvent`
- **Verification**:
  - Fixture-run unit tests cover: package layout (manifest + one
    `.jsonl.gz` per session with a transcript), strip applied to a worker
    whose transcript opens with the fixture base context and recorded as
    `base_context_stripped: true`, a non-matching transcript exported whole
    with the flag false, rewrite history assembled from two `spec-gen*.json`
    files in order, escalations found in both directory locations, missing
    transcript ⇒ no events file + `transcript_missing: true`, stale failure
    still normalized to null, re-export overwriting the package idempotently.
    Run: `uv run pytest tests/test_export.py -q`
    Pass: all tests green.
  - The real reference run exports end-to-end (external oracle: a genuine
    run dir this repo already contains).
    Run: `uv run smart-mcps-orchestrate export r20260902-132128 --repo /home/gbm1996/wksp/smart_mcps --out ./tmp-ingest`
    Pass: exit 0; `./tmp-ingest/ingest.json` has `schema_version: 2`, 5
    groups, no `base_session` key, `base_context.sha256` equal to
    `sha256sum` of the run dir's `base-context.md`; an events file exists
    for every session whose `transcript_missing` is false; g3 lists 3
    escalation pairs; every `base_context_stripped: true` session's first
    event does not contain the string of `base-context.md`'s first line.
  - `--help` and the CLI surface reflect v2 only.
    Run: `uv run smart-mcps-orchestrate export --help`
    Pass: help text describes the package/directory output; no flag or text
    references schema v1 behaviour.
- **Edge cases**: —
- **Non-goals / must-not**: —

### U4. contract-doc — the Run Bundle v2 specification

- **Summary**: [docs] `docs/run-bundle-contract.md` specifies the v2 package
  (layout, manifest fields, neutral-event schema, strip semantics,
  versioning rule) as the normative document both the exporter and any
  Framework Adapter cite.
- **Goal**: The document a consumer implements against without reading
  exporter code: package layout tree, every manifest field with type and
  null-tolerance semantics, the `NeutralEvent` fields, the strip contract
  (byte-exact prefix, dedup-not-erasure, the consumer's obligation to render
  a base-context reference), and the versioning rule (v2 only; incompatible
  change mints v3). Mirrors the writer of `docs/orchestrator-task-map.md`:
  "writers: export.py; readers: framework adapters; change only with both in
  view."
- **Files**: `docs/run-bundle-contract.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U3
- **Slice**: —
- **Implements / Consumes**: consumes `ingest/v2`
- **Verification**:
  - The doc agrees with the implementation (external oracle: the real
    exported package from U3's verification).
    Run: `uv run smart-mcps-orchestrate export r20260902-132128 --repo /home/gbm1996/wksp/smart_mcps --out ./tmp-ingest2 && uv run python -c "import json; print(sorted(json.load(open('./tmp-ingest2/ingest.json'))))"`
    Pass: every top-level manifest key printed is documented in
    `docs/run-bundle-contract.md`, and every field the doc marks required is
    present in the output.
  - The doc states the strip semantics and the consumer's render obligation.
    Run: `grep -in "dedup" docs/run-bundle-contract.md`
    Pass: the strip section says the prefix is byte-exact, records
    `base_context_stripped` per session, and obliges consumers to show a
    base-context reference in any timeline rendering.
- **Edge cases**: —
- **Non-goals / must-not**: —

## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-flow-doc
    description: Write docs/orchestrator-flow.md — the concise lifecycle document with LLM call sites and ADR pointers
    slice: null
    files:
      - docs/orchestrator-flow.md
    size_hints:
      docs/orchestrator-flow.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-event-parser
    description: Parse Claude Code jsonl into harness-neutral events with byte-exact base-context strip and gzip serialization
    slice: bundle-v2
    files:
      - orchestrator/execution/transcript_events.py
      - tests/test_transcript_events.py
    size_hints:
      orchestrator/execution/transcript_events.py: large
      tests/test_transcript_events.py: medium
    symbols: []
    depends_on: []
    implements: ["NeutralEvent"]
    consumes: []
  - task_id: u3-package-writer
    description: Rework orchestrate export to write the v2 self-contained package directory and delete the v1 writer
    slice: bundle-v2
    files:
      - orchestrator/execution/export.py
      - orchestrator/cli.py
      - tests/test_export.py
    symbols: []
    depends_on: [u2-event-parser]
    implements: ["ingest/v2"]
    consumes: ["NeutralEvent"]
  - task_id: u4-contract-doc
    description: Write docs/run-bundle-contract.md — the normative v2 package specification for exporter and adapters
    slice: null
    files:
      - docs/run-bundle-contract.md
    size_hints:
      docs/run-bundle-contract.md: medium
    symbols: []
    depends_on: [u3-package-writer]
    implements: []
    consumes: ["ingest/v2"]
```
