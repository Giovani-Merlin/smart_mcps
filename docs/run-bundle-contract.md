# Run Bundle v2 (`ingest/` schema_version 2)

The contract a Framework Adapter (Infinity Skills or any other consumer)
implements against without reading exporter code. `orchestrate export <run_id>`
writes this package; a consumer reads only what is documented here, never
Claude Code jsonl directly.

Writers: [`orchestrator/execution/export.py`](../orchestrator/execution/export.py)
(the `ingest.json` manifest) and
[`orchestrator/execution/transcript_events.py`](../orchestrator/execution/transcript_events.py)
(the neutral event parser). Readers: framework adapters. Change this document
only with both sides in view — it mirrors the writer/reader pact in
[`docs/orchestrator-task-map.md`](orchestrator-task-map.md).

## Versioning rule

`schema_version` is the first field read. **v2 only — there is no v1 fallback**
(v1's single-file `ingest.json` writer was deleted in the same change that
introduced this contract; nothing consumes it). Additive fields (a new
optional key, a new enum value) are free and do not bump the version. Any
change that removes a field, changes a field's type, or changes the meaning of
an existing value is breaking and mints `schema_version = 3` with its own
contract document — this document is never silently reinterpreted for a
future version.

## Package layout

```
<run_dir>/ingest/
├── ingest.json              # the manifest (below)
└── events/
    └── <session_id>.jsonl.gz   # one file per session that has a transcript
```

`events/<session_id>.jsonl.gz` is a gzip-compressed file of newline-delimited
JSON, one `NeutralEvent` object per line, in transcript order. A session with
no resolvable transcript (see `transcript_missing`) has no file under
`events/` at all — its `ExportSession.events_path` is `null`, not a path to a
missing or empty file.

## `ingest.json` manifest

Top-level (`RunExport`):

| field            | type                        | null-tolerance                                                                         |
| ---------------- | --------------------------- | -------------------------------------------------------------------------------------- |
| `schema_version` | int                         | always `2`, always present                                                             |
| `framework`      | string                      | always `"smart-mcps-orchestrator"`                                                     |
| `run_id`         | string                      | always present                                                                         |
| `repo_root`      | string                      | always present (absolute path at export time)                                          |
| `project`        | string                      | always present                                                                         |
| `plan`           | `ExportPlan`                | always present (an object; its own fields may be empty)                                |
| `grouping`       | `ExportGrouping` \| null    | null when the run has no grouping metadata at all                                      |
| `created_at`     | string (ISO 8601) \| null   | null on manifests that predate the field                                               |
| `base_context`   | `ExportBaseContext` \| null | null when the run has no `base-context.md` (pre-ADR-0007 runs, or the file is missing) |
| `groups`         | `ExportGroup[]`             | always present; empty only if the run truly has none                                   |

`ExportPlan`:

| field  | type           | null-tolerance                                                 |
| ------ | -------------- | -------------------------------------------------------------- |
| `path` | string         | `""` when the run has no recorded plan path                    |
| `text` | string \| null | null when `path` is empty or the file no longer exists on disk |

`ExportGrouping`:

| field         | type           | null-tolerance                                                                     |
| ------------- | -------------- | ---------------------------------------------------------------------------------- |
| `name`        | string \| null | null when the run's grouping has no name                                           |
| `granularity` | string \| null | null when `grouping-trace.json` is absent or has no `config.partition.granularity` |

`ExportBaseContext` (present only when `base-context.md` exists in the run
directory):

| field      | type                                                |
| ---------- | --------------------------------------------------- |
| `path`     | string, relative to `run_dir` (`"base-context.md"`) |
| `sha256`   | string, hex digest of the file's raw bytes          |
| `char_len` | int, `len()` of the UTF-8-decoded text              |

### `ExportGroup` (one per group)

| field           | type                 | null-tolerance                                                                                                                                                            |
| --------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`            | string               | always present                                                                                                                                                            |
| `name`          | string               | `""` if unnamed                                                                                                                                                           |
| `summary`       | string               | `""` if none                                                                                                                                                              |
| `final_state`   | string               | `"pending"` if the group never ran                                                                                                                                        |
| `failure`       | string \| null       | null when there is no failure, **and** null when a recorded failure string is stale (see `stale_failure`) — never a failure exported alongside a successful `final_state` |
| `stale_failure` | bool                 | true when a failure string was on disk but `final_state` is not a failure state; the flag exists precisely so a stale failure is never silently dropped without a trace   |
| `depends_on`    | string[]             | `[]` if none                                                                                                                                                              |
| `spec`          | object \| null       | the assembled spec from the run's `groups.json`; null only if the group id is absent from that snapshot (not observed in practice)                                        |
| `rewrites`      | `ExportRewrite[]`    | `[]` if the group was never rewritten                                                                                                                                     |
| `sessions`      | `ExportSession[]`    | `[]` if the group never launched a worker                                                                                                                                 |
| `artifacts`     | `ExportArtifact[]`   | `[]` if none                                                                                                                                                              |
| `escalations`   | `ExportEscalation[]` | `[]` if none                                                                                                                                                              |

Synthetic `role: "orchestrator"` session rows the live snapshot injects for
board display (rewrite/base rows) are **not** exported as sessions — their
content is already carried by `rewrites`.

### `ExportSession` (one per real coder/reviewer session)

| field                     | type                      | null-tolerance                                                                                                                                                                       |
| ------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `session_id`              | string                    | always present                                                                                                                                                                       |
| `role`                    | string                    | `"coder"` \| `"reviewer"`                                                                                                                                                            |
| `generation`              | int                       | defaults to `1`                                                                                                                                                                      |
| `name`                    | string                    | `""` if unnamed                                                                                                                                                                      |
| `transcript_missing`      | bool                      | true when no transcript file could be resolved (recorded path stale/absent, and no `*/<session_id>.jsonl` glob match under the transcript root)                                      |
| `events_path`             | string \| null            | relative to the package directory (`events/<session_id>.jsonl.gz`); null exactly when `transcript_missing` is true, or when the export ran without `events_dir` (a facts-only build) |
| `events_count`            | int                       | `0` when `events_path` is null                                                                                                                                                       |
| `base_context_stripped`   | bool                      | see [Strip semantics](#strip-semantics) below                                                                                                                                        |
| `started_at` / `ended_at` | string (ISO 8601) \| null | null if not recorded                                                                                                                                                                 |
| `rounds_completed`        | int                       | `0` if none                                                                                                                                                                          |
| `retirement_reason`       | string \| null            | null if the session never retired                                                                                                                                                    |
| `model`                   | string \| null            | null if not recorded                                                                                                                                                                 |
| `tokens`                  | `ExportTokens`            | all-zero means "not recorded", not "zero spent" — see below                                                                                                                          |

`ExportTokens` (`input`, `output`, `cache_read`, `cache_creation`, all int):
this is the one place the contract deliberately uses `0` instead of `null`
for an absent value, because the manifest itself already treats `0` as
"not recorded" for these counters — inventing a `null` here would just move
the ambiguity, not remove it.

### `ExportArtifact` (one per `report-g<G>-r<R>.json` / `verdict-g<G>-r<R>.json`)

| field                  | type                          | null-tolerance                                                                                                                                         |
| ---------------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `kind`                 | string                        | `"coder_report"` \| `"reviewer_verdict"` \| `"other"` (filename didn't match the round pattern)                                                        |
| `generation` / `round` | int \| null                   | null when `kind` is `"other"`                                                                                                                          |
| `path`                 | string, relative to `run_dir` | always present                                                                                                                                         |
| `status`               | string \| null                | null if the artifact JSON has no `status`                                                                                                              |
| `surprises`            | `ExportSurprise[]`            | `[]` if none                                                                                                                                           |
| `denial_kind`          | string \| null                | present only when `status == "permission_denied"`, classified the same way the live review loop does; null otherwise                                   |
| `denied_command`       | string                        | `""` if none                                                                                                                                           |
| `required_changes`     | string[]                      | `[]` if none                                                                                                                                           |
| `error`                | string \| null                | non-null when the artifact file was unreadable or half-written; the artifact is still listed (a round happened) even though its content is unavailable |

`heartbeat.json` and `spec-gen<N>.json` files are never listed as artifacts —
the former is per-group bookkeeping, the latter is exposed instead as
`ExportRewrite`.

### `ExportEscalation`

| field               | type                                  | null-tolerance                           |
| ------------------- | ------------------------------------- | ---------------------------------------- |
| `id`                | string                                | always present                           |
| `kind`              | string                                | `""` if none                             |
| `generation`        | int \| null                           | null if the request didn't record one    |
| `prompt`            | string                                | `""` if none                             |
| `created_at`        | string \| null                        | null if not recorded                     |
| `request_path`      | string, relative to `run_dir`         | always present                           |
| `response_path`     | string \| null, relative to `run_dir` | null if no response has been written yet |
| `action` / `answer` | string \| null / string               | null / `""` when there is no response    |

Escalations are collected from **both** `<run_dir>/escalations/` and every
`<run_dir>/groups/<gid>/` directory (newer runs write pairs in the group
directory); an id seen under the run-level directory first wins on a
collision.

### `ExportRewrite` (one per `spec-gen<N>.json`)

| field                  | type                                                         |
| ---------------------- | ------------------------------------------------------------ |
| `generation`           | int                                                          |
| `spec`                 | object — the full rewritten `Group` model at that generation |
| `triggering_surprises` | `ExportSurprise[]`                                           |
| `escalation_ids`       | string[]                                                     |

There is no field anywhere that names what caused a given rewrite directly.
`triggering_surprises` and `escalation_ids` are a **best-effort bucketing by
generation**: every surprise or escalation recorded strictly after the
previous generation's snapshot (exclusive) up to and including this
generation (inclusive) is attributed to it. Treat this as an inference, not a
recorded causal link.

## `NeutralEvent` schema

One object per line in `events/<session_id>.jsonl.gz`, in transcript order:

| field         | type             | meaning                                                                                                                                                    |
| ------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_id`    | string           | the source jsonl record's `uuid`; a record with multiple content blocks (e.g. text + tool_use) gets one event per block, suffixed `#<index>` for index > 0 |
| `role`        | string           | `"user"` \| `"assistant"` \| `"tool"` — a `tool_result` block always becomes role `"tool"` regardless of which jsonl record type it appeared under         |
| `timestamp`   | string \| null   | the source record's timestamp, verbatim                                                                                                                    |
| `text`        | string \| null   | present for a text or thinking block; null for tool_use/tool_result events                                                                                 |
| `tool_name`   | string \| null   | present only on a `tool_use` event                                                                                                                         |
| `tool_use_id` | string \| null   | present on a `tool_use` event (the id it will be matched by) and on the corresponding `tool_result` event                                                  |
| `tool_input`  | any JSON \| null | the tool_use block's input, **uncapped** — full fidelity, no truncation at export time                                                                     |
| `tool_output` | string \| null   | the tool_result block's content flattened to text, **uncapped**                                                                                            |
| `is_error`    | bool             | the tool_result block's `is_error`, defaulting false                                                                                                       |

Excerpting or capping payload size for display is the consumer's
responsibility (its own raw table should keep what its UI truncates) — the
contract never caps at export time, since a cap applied here would be
unrecoverable for any downstream reader. Block types the parser does not
recognize (e.g. `image`) are silently skipped; the contract only promises
role/text/tool fidelity, not full multimodal fidelity. Empty or malformed
jsonl lines (a torn write at the tail of a live transcript) are skipped
without aborting the parse.

## Strip semantics

Every worker's first prompt is exactly `f"{base_context}\n\n{prompt}"`
(`SessionRunner.start_worker`), so a worker transcript's first user message
begins, byte-for-byte, with the shared base-context text plus the `\n\n`
separator.

- The strip check is a **byte-exact prefix match** against the run's
  `base-context.md` text plus `"\n\n"` — never a fuzzy, normalized, or
  partial match.
- This is **dedup, not erasure**: the base context is stored once, in full,
  as `base_context.text`-equivalent content at `<run_dir>/base-context.md`
  (referenced by the manifest's `base_context` block, with its `sha256` and
  `char_len`); it is only *omitted from the per-session event stream* when
  the match succeeds, so the full text is never lost from the bundle.
- `ExportSession.base_context_stripped` records, per session, whether the
  strip was applied. When `true`, that session's first event in
  `events/<session_id>.jsonl.gz` has already had the prefix removed. When
  `false` — either the first message didn't match (never a partial strip in
  that case; the message is exported whole) or the run has no
  `base-context.md` at all — the event carries the full, unmodified text.
- **Consumer obligation**: any timeline rendering of a session whose
  `base_context_stripped` is `true` must show a reference to the shared base
  context (e.g. a collapsed "shared base context" header) at the head of
  that session's timeline, so the reader is never misled into thinking the
  session's first turn started mid-conversation with no setup.

## Tolerance rules (deliberate, load-bearing)

These are not bugs to fix — a consumer should expect and handle them:

- `transcript_path` recorded on an old manifest may be stale (a usage-limit
  retry adopts a new session id) or simply absent (older manifests predate
  the field). The exporter re-resolves by globbing
  `<transcript_root>/*/<session_id>.jsonl` before giving up and setting
  `transcript_missing`.
- A `failure` string attached to a group whose `final_state` is not a
  failure is exported as `null` with `stale_failure: true`, never as a
  failure alongside a successful state.
- Fields absent from an old manifest or artifact file export as `null`,
  never an invented zero or empty-but-meaningful value — except the token
  counters (see `ExportTokens` above), which use `0` to mean "not recorded"
  because the manifest itself already overloads `0` that way.
- `surprises.json` is consumed run state, not export history — surprises are
  mined from the `report`/`verdict` artifacts instead, and a rewrite's
  triggering surprises/escalations are inferred by generation bucketing (see
  `ExportRewrite` above), never read from a field that names the cause
  directly, because no such field exists.

## Producing and reading a bundle

Produce: `smart-mcps-orchestrate export <run_id> --repo <repo_root> [--out <dir>]`
writes the package to `<run_dir>/ingest/` (or `--out` if given).

Read: parse `ingest.json` as `RunExport`, then stream-decompress each
`ExportSession.events_path` (when non-null) as gzip'd newline-delimited
`NeutralEvent` JSON. A consumer never needs `repo_root` to exist on its own
machine and never opens a Claude Code `.jsonl` file directly — everything it
needs is inside `ingest/`.
