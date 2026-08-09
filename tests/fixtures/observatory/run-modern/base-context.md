# Base context

## Repo conventions (CLAUDE.md)

# smart-mcps

A Claude Code plugin that installs skills and session hooks for codegraph, NotebookLM, and Perplexity.

## Plugin structure

This is a **Claude Code plugin repository**. Hooks and skills are distributed to plugin consumers via auto-discovered files:

| Path                         | Purpose                                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| `hooks/hooks.json`           | Plugin-level hook registrations (uses `${CLAUDE_PLUGIN_ROOT}`)                       |
| `hooks/scripts/`             | Hook implementation scripts (`.py` and `.sh`)                                        |
| `skills/`                    | Skill definitions                                                                    |
| `agents/`                    | Subagent definitions (auto-discovered; `.claude/agents` symlinks here for local dev) |
| `codegraph_mcp/`             | FastMCP proxy exposing 6 trimmed codegraph tools (`smart-mcps-codegraph`)            |
| `.mcp.json`                  | MCP server registration — serves plugin consumers **and** local dev (see below)      |
| `.claude-plugin/plugin.json` | Plugin identity (name, version)                                                      |
| `.claude/settings.json`      | Project-level hooks for local development (uses `$CLAUDE_PROJECT_DIR`)               |

### Adding hooks — always register in both places

Every hook script must be wired up in **two** files:

1. **`hooks/hooks.json`** — so plugin consumers receive it. Uses `${CLAUDE_PLUGIN_ROOT}`.
2. **`.claude/settings.json`** — so it runs locally during development. Uses `$CLAUDE_PROJECT_DIR`.

Matchers are **case-sensitive** and PascalCase in **both** files (e.g. `"Edit|Write|MultiEdit"`, `"Bash"`) — a lowercase matcher silently matches nothing. This applies to every event, including `SessionStart` (codegraph reindex, formatter bootstrap), not just `PostToolUse`.

Registering in only one place means either plugin consumers or local dev is broken. Always do both.

### MCP servers — one file, not two

The dual-registration rule above does **not** apply to MCP servers: `.claude/settings.json` has no `mcpServers` key (it only *approves* servers via `enabledMcpjsonServers`). A single `.mcp.json` at the repo root covers both audiences, because Claude Code reads it twice:

- **Plugin consumers** — auto-discovered as the plugin's `./.mcp.json`; tools resolve as `mcp__plugin_smart-mcps_codegraph__<tool>`.
- **Local dev** — read as the project-scoped `.mcp.json`; tools resolve as `mcp__codegraph__<tool>`.

Hence `"--project", "${CLAUDE_PLUGIN_ROOT:-.}"`. `${CLAUDE_PLUGIN_ROOT}` is set only for consumers; locally it falls back to `.` (the server's cwd is the project root). Two constraints worth knowing before editing that line:

- **`${CLAUDE_PROJECT_DIR}` is not available in `.mcp.json`** — only in hooks. Using it yields a "Missing environment variables" warning.
- **Defaults don't nest.** `${VAR:-default}` works, but `${A:-${B}}` does not expand `B` — it silently passes a literal, which `uv` may accept when the console script is already on PATH, hiding the bug locally while breaking for consumers.

## Never keep working notes in the `/tmp` scratchpad

The session scratchpad under `/tmp` is **wiped when the Claude Code process restarts** — not
just at the end of a session. Anything written there is gone without warning.

This has already cost real work: on 2026-07-29 a restart destroyed the accumulated findings
and deferred-fix list for two sessions of orchestrator debugging, plus a patch backup of a
group's stranded work.

Use `/tmp` only for genuinely throwaway intermediates. Anything another session would want —
findings, deferred-fix lists, handoff notes, backups of uncommitted work — goes somewhere
durable:

| what                                  | where                                                                               |
| ------------------------------------- | ----------------------------------------------------------------------------------- |
| Run findings, deferred fixes, backups | `.orchestrator/` — gitignored, survives restarts, never collides with a group merge |
| Facts worth recalling across sessions | the auto-memory dir (`~/.claude/projects/<project>/memory/`)                        |
| Anything the repo should carry        | `docs/` — committed                                                                 |

Write it durably **as you go**, not at the end. A restart gives no notice.

## Codebase architecture (codegraph)

Project Structure (82 files):

├── codegraph_mcp
│   ├── __init__.py (python, 1 symbols)
│   └── server.py (python, 22 symbols)
├── hooks
│   └── scripts
│       ├── lint_after_edit.py (python, 12 symbols)
│       └── save_research.py (python, 14 symbols)
├── orchestrator
│   ├── execution
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── escalation.py (python, 22 symbols)
│   │   ├── manifest.py (python, 42 symbols)
│   │   ├── merge.py (python, 14 symbols)
│   │   ├── prompting.py (python, 16 symbols)
│   │   ├── review.py (python, 60 symbols)
│   │   ├── scheduler.py (python, 63 symbols)
│   │   ├── sessions.py (python, 47 symbols)
│   │   └── worktrees.py (python, 23 symbols)
│   ├── grouping
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── base_context.py (python, 5 symbols)
│   │   ├── estimator.py (python, 14 symbols)
│   │   ├── graphing.py (python, 42 symbols)
│   │   ├── llm_record.py (python, 14 symbols)
│   │   ├── llm.py (python, 31 symbols)
│   │   ├── mapper.py (python, 11 symbols)
│   │   ├── partition.py (python, 60 symbols)
│   │   ├── pipeline.py (python, 35 symbols)
│   │   ├── plan_reader.py (python, 20 symbols)
│   │   ├── scorecard.py (python, 10 symbols)
│   │   ├── speccer.py (python, 12 symbols)
│   │   └── trace.py (python, 44 symbols)
│   ├── observatory
│   │   ├── __init__.py (python, 1 symbols)
│   │   ├── app.py (python, 19 symbols)
│   │   ├── artifacts.py (python, 11 symbols)
│   │   ├── escalations.py (python, 14 symbols)
│   │   ├── events.py (python, 24 symbols)
│   │   ├── registry.py (python, 13 symbols)
│   │   ├── runs.py (python, 27 symbols)
│   │   └── transcripts.py (python, 19 symbols)
│   ├── prompts
│   │   └── __init__.py (python, 3 symbols)
│   ├── __init__.py (python, 1 symbols)
│   ├── cli.py (python, 63 symbols)
│   ├── config.py (python, 16 symbols)
│   └── model.py (python, 27 symbols)
├── pplx
│   ├── __init__.py (python, 1 symbols)
│   └── cli.py (python, 25 symbols)
├── tests
│   ├── fake_claude.py (python, 21 symbols)
│   ├── regenerate_golden_partitions.py (python, 12 symbols)
│   ├── test_cli.py (python, 129 symbols)
│   ├── test_codegraph_server.py (python, 27 symbols)
│   ├── test_e2e_faults.py (python, 24 symbols)
│   ├── test_e2e_stub.py (python, 44 symbols)
│   ├── test_escalation.py (python, 53 symbols)
│   ├── test_estimator.py (python, 23 symbols)
│   ├── test_golden_partitions.py (python, 16 symbols)
│   ├── test_graphing.py (python, 62 symbols)
│   ├── test_grouper_pipeline.py (python, 109 symbols)
│   ├── test_grouping_fixtures.py (python, 60 symbols)
│   ├── test_grouping_llm.py (python, 16 symbols)
│   ├── test_grouping_trace.py (python, 44 symbols)
│   ├── test_lint_after_edit.py (python, 27 symbols)
│   ├── test_llm_record.py (python, 24 symbols)
│   ├── test_llm.py (python, 9 symbols)
│   ├── test_manifest_snapshot.py (python, 4 symbols)
│   ├── test_merge.py (python, 26 symbols)
│   ├── test_model.py (python, 29 symbols)
│   ├── test_observatory_api.py (python, 49 symbols)
│   ├── test_observatory_escalations.py (python, 30 symbols)
│   ├── test_observatory_events.py (python, 23 symbols)
│   ├── test_observatory_transcripts.py (python, 37 symbols)
│   ├── test_partition.py (python, 81 symbols)
│   ├── test_plan_reader.py (python, 42 symbols)
│   ├── test_review_loop.py (python, 90 symbols)
│   ├── test_scheduler.py (python, 98 symbols)
│   ├── test_scorecard.py (python, 21 symbols)
│   └── test_sessions.py (python, 65 symbols)
└── ui
    ├── src
    │   ├── components
    │   │   ├── EscalationPanel.tsx (tsx, 16 symbols)
    │   │   ├── EventLog.tsx (tsx, 6 symbols)
    │   │   ├── GroupBoard.tsx (tsx, 10 symbols)
    │   │   ├── GroupDrillIn.tsx (tsx, 18 symbols)
    │   │   └── ProjectRunSwitcher.tsx (tsx, 6 symbols)
    │   ├── api.ts (typescript, 21 symbols)
    │   ├── App.tsx (tsx, 9 symbols)
    │   ├── main.tsx (tsx, 5 symbols)
    │   ├── types.ts (typescript, 28 symbols)
    │   └── useRunStream.ts (typescript, 8 symbols)
    └── vite.config.ts (typescript, 3 symbols)

## Plan document (2026-08-08-observatory-grouping-provenance-and-attempt-history.md)

# Observatory: grouping provenance, attempt history, and cost accounting

Date: 2026-08-08
Branches: `feat/multiagent-orchestrator` (PR branch, instrumentation) → `feat/observatory` (UI + API)
Evidence dossiers: `/tmp/compound-engineering/ce-ideate/ob5a9c31/` (7 files; ephemeral — key findings are inlined below)

## What the operator asked for

1. A tab that explains, deeply, how the plan currently being viewed was grouped — every input the DAG
   construction consumed, and the *story* of the grouping agent: what it was asked, what it thought,
   what it answered.
2. On-disk folder paths shown and copyable everywhere, so the operator can go read the raw artifacts.
3. Every agent session attempt visible per group — not just the latest — colour-coded, clickable.
4. Estimate-vs-actual sizing: what the grouper predicted a group would cost vs what it actually cost,
   in total and split per coder and per reviewer, with cached / cache-written / uncached token classes.
5. The speccer session (the LLM end-pass of the grouper) treated as first-class alongside the mapper.
6. General frontend quality: the current UI "is not so great."

## Three findings that reshape the request

### F1 — The attempt history already exists; the *board* hides it

`manifest.json`'s per-group `sessions` list is append-only (`record_session` → `.append()`), and
`GroupDrillIn.tsx:378-389` already renders every retired session with its `retirement_reason`.
Verified on disk: run `r20260726-grouping` group g2 holds 4 session entries across 2 generations with
gen-1's retirement reason (`"context tokens 7618531 exceeded limit 120000"`) intact.

So this is not a data-recovery problem. `GroupBoard` shows a single generation number and no session
list, so nothing on the board hints that earlier attempts existed — you must drill in per group to
find out. The fix is board-level signal plus a dedicated grid, not new persistence.

One genuine loss does exist: `state.json`'s `GroupRunState` is single-valued last-writer-wins. Real
runs on disk show `"state": "completed"` with a stale `failure` string still attached. It cannot
represent "failed once, then succeeded." **The UI must treat `manifest.json` as ground truth for what
attempts existed, and `state.json` as authoritative only for current state.**

### F2 — The grouping agent leaves no trace at all

`claude_json_runner` (`grouping/llm.py:72-105`) invokes `claude -p --json-schema` with **no
`--session-id`**, reads `envelope["result"]`, and discards session id, usage, and thinking. No
transcript jsonl is ever written. `_save_failure` (`llm.py:139-146`) saves only the last raw text on
final failure — intermediate retries vanish at the `last_raw` overwrite (`llm.py:122`).

`SessionRunner._call` (`execution/sessions.py:260-295`) already does this correctly for worker
sessions. The grouping path simply never adopted the pattern.

Consequence: requirement 1 is impossible today, and there is no folder to satisfy requirement 2 with.
This needs orchestrator changes, decided to land on the PR branch.

### F3 — Edge provenance is destroyed at write time

`graphing.py` computes distinct edge signals — shared-file overlap, codegraph callers/callees/impact,
declared `depends_on`, semantic `implements`/`consumes` tags, prose fallback — then **sums them into
one opaque weight** in `TaskGraph.affinity`/`.dependencies`. The per-signal breakdown, the
declared-vs-inferred flag, and the symbols that justified an edge never reach disk; they survive only
as prose inside flag strings. Withdrawn edges keep only a flag string (`graphing.py:337-338`).

`grouping-trace.json` is the closest existing "why" record — stage-by-stage partition snapshots,
merge/split/repair decisions, hub-role ratios, slice atoms — but nothing joins a specific affinity
edge to the Louvain decision it caused. `trace.py` and `scorecard.py` are both write-only and inert.

### F4 (blocking) — The merge crashes the Observatory

`RunPaths.groups_path` was removed from `manifest.py` on `feat/multiagent-orchestrator`. Two
observatory call sites use it: `observatory/runs.py:189` and `observatory/events.py:115`. After the
merge, `/api/runs/{id}/snapshot` **and** `/api/events/run` both raise `AttributeError` for every run.
This must be fixed in the merge commit itself.

Silent (non-crashing) drift the observatory has no concept of: `RunManifest.grouping` /
`.escalation`, `SessionEntry.last_context_tokens`, `GroupState.INTERRUPTED` and `RESOLVED`,
`CoderReport.permission_denied`, `EscalationKind.GROUP_RESOLVE`, and the entire named-groupings
system under `.orchestrator/groupings/<name>/`. `ui/src/types.ts` is missing the same enum members,
so they would render unstyled today.

## Decisions taken

| Decision                    | Choice                                                                                       | Rationale                                                                                                  |
| --------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Where instrumentation lands | A scoped commit on `feat/multiagent-orchestrator`                                            | The code belongs with the orchestrator; keep the diff tight and additive with an explicit not-touched list |
| Frontend ambition           | Tab shell + real router + CSS token layer; reuse existing components                         | The measured problem is `var(--)` count = 0 and no routing, not component quality                          |
| DAG rendering               | `@xyflow/react` + `@dagrejs/dagre` (maintained fork), lazy-loaded on the Grouping route only | ~10-100 nodes; deterministic layered layout so shared links and screenshots match                          |
| Attempt history layout      | Airflow Grid pattern — rows = groups, columns = generations, coloured cells                  | Explicitly rejected LangSmith's implicit nested-retry rows                                                 |
| Edge → evidence             | OpenLineage/Marquez pattern: every edge one click from the record that produced it           | Directly serves "why is this edge here"                                                                    |
| Transcript rendering        | assistant-ui / Zed collapsed-by-default cards with identifying one-liners                    | Skip virtualization below ~500 turns                                                                       |

Non-goals carried forward unchanged from the 2026-07-21 brainstorm: launching / resuming / aborting
runs from the UI, editing plans or config from the UI, auth / multi-user / TLS, token-level streaming.

Fixed terminology, used verbatim in the UI: **run → group → generation → session → round**, plus
`stale_dag`, `live_pids` (display-only, never a liveness check), `EscalationKind`,
`HumanAction` = `answer|skip|abort`.

## Frontend design

Full document: `design-frontend.md`. Key decisions:

**Information architecture.** Five tabs inside a run — Board (reuses `GroupBoard`), History (attempt
grid), Grouping (new; the primary ask), Escalations (`EscalationPanel` verbatim), Log (`EventLog`
promoted to full height) — plus a `/p/:project` run index that fixes today's auto-jump-to-newest dead
end. The session viewer is route-addressable but deliberately not a tab.

`react-router-dom` v6 with `createBrowserRouter`. Path segments identify objects; query params
identify view state (`?group=`, `?stage=`, `?edge=`, `?seq=`). **Blocking prerequisite: the backend
needs an SPA fallback route** — `StaticFiles` at `/` currently 404s deep links on refresh. HashRouter
is the fallback if that's refused.

**The Grouping tab is useful before any instrumentation ships.** Stages, Louvain communities,
merge/split/repair rationale, hub roles, slice atoms, the scorecard and the task-level graph are all
already sitting in `grouping-trace.json` v1 and rendered nowhere. Do not sequence this tab behind the
instrumentation workstream.

Its highest-value interaction is scrubbing a pipeline stepper (`louvain → lift → split → merge → repair → renumber`) and watching nodes recolour — derived by diffing `trace.stages`. That is the
closest thing to a stored answer for "why is task X in group Y."

**`PathChip` is an app-wide primitive, not a one-off.** Every file-backed panel header carries exactly
one: click-to-copy, middle-ellipsised. Plus a per-route "paths" drawer with copy-all. Display and copy
only — no file-serving from the chip itself.

**The two-amber collision is resolved by fiat: amber means "needs the operator's attention."**
Escalation = solid amber; inferred-stall = hatched amber. `running` / `reviewing` / `rewriting` /
`merging` collapse to one blue hue differentiated by glyph and label, not four hues.

**"Stalled" is an inference and the UI says so.** No `GroupState` for it exists and none should be
added (R7's no-timeout decision stands). The cell reads *"no activity for 23m"* — a fact — never
"hung" — a claim. Computed as `state ∈ ACTIVE_STATES && no run-dir writes > 15min && no pending escalation`, rendered as an overlay with a `?`, never as a state colour, and it must never consult
`live_pids`.

**The likeliest correctness bug in the whole tab**, called out for fixture-testing against real
`r20260726-grouping` data: when state is `completed`/`resolved` with a non-null `failure`, render a
"stale failure text" chip with an explanation — never a failure.

`GroupState → colour` lives in one `status.ts` map with an exhaustive `never` guard so adding a state
fails the build. `ui/` has no test runner at all today; vitest + testing-library are added, because
Airflow's own history shows attempt-history and status-colour components are the first surfaces to rot.

Seven special cases are specified concretely: breaker retirement, usage-limit outage,
escalation-blocked (orthogonal to state), superseded-by-respawn, self-verify groups with zero reviewer
sessions, interrupted-as-resumable, and round-atomic bookkeeping loss. Graceful degradation is
specified at four independent levels, each naming the missing artifact and showing a `PathChip` to
where it was looked for — because the operator's next move is to go look on disk.

## Instrumentation design

Full document: `design-instrumentation.md`. Key decisions:

**P1 — grouper call records.** `call_llm_json` gains an optional `LlmCallRecorder` seam, matching the
inert-observation contract `TraceRecorder` already uses. `claude_json_runner` mints a uuid and passes
`--session-id` (already in `REQUIRED_CLI_FLAGS`, `sessions.py:34-40`). A `_normalize` shim keeps
`JsonRunner = Callable[[str, dict], str]` intact so **zero existing test stubs churn** — the key move
for a branch that was meant to be frozen.

Artifacts land in `.orchestrator/groupings/<name>/llm/`: a `calls.json` index plus per-attempt
request/envelope/raw files, **one record per attempt including failed and repaired ones**. Schema
borrows `gen_ai.*` naming and `status:{code}` from the OpenTelemetry GenAI conventions with no OTel
dependency. Correlation id is `grouping_run_id`, written to `llm/calls.json` and to `provenance` in
`grouping-trace.json`. `groups.json` is deliberately **not** touched — a timestamped id there would
break `serialize_grouping`'s determinism contract; joinability comes from `produced_group_ids` /
`produced_task_ids` on the call record instead.

Blocker the scouts missed: `snapshot_grouping` (`manifest.py:100-103`) copies only `is_file()`
entries, so a nested `llm/` directory would be silently dropped from every run snapshot. Needs
`copytree(dirs_exist_ok=True)`.

Mapper and speccer are both covered — same seam, same artifact layout, distinguished by
`gen_ai.operation.name`.

**P2 — edge provenance.** `_EdgeAccumulator` gains two ledgers mirroring its two weight maps exactly,
plus structured `withdrawn` edges. Arithmetic is untouched; nothing new is ever read back.

Recommendation: **a new sidecar `edge-provenance.json`, not an extension of `grouping-trace.json`** —
the trace is byte-stable by contract, is the artifact operators diff, and its `input_graph` is
post-cycle-drop so it structurally cannot hold withdrawn edges. It does not need to pass through
`partition.py`: `TaskGraph` gains one `provenance: object | None = None` field (no new import,
preserving the import-purity test) and the group-level rollup is computed in the pipeline at write
time. Size: ~20 KB at 8 tasks, ~55 KB at 24, ~140 KB at 40; a saturated graph would reach ~1.1 MB, so
`max_contributions_per_edge = 20` with honest counters ships from day one. Correctness is one
exhaustive test: `Σ contribution.scaled_weight == graph.affinity[pair]` for every pair.

**P3 — no hung state.** Emit evidence rather than a state: transcript mtime (free, already recorded,
nobody reads it) plus a `heartbeat.json` written by an inert daemon thread carrying `round_started_at`.
Ground truth = heartbeat age, transcript mtime, round start. Inference = "stalled", computed in the UI
and never persisted — persisting it would create a de facto state that future code branches on.

**Round-atomic bookkeeping is out of scope here.** Fixing it means writing a provisional artifact
before the blocking call and reconciling after, which changes the review loop's round-numbering
invariant. The heartbeat plus `started_at` deliver the observability 80% ("started 54m ago, 0 rounds
completed") without touching control flow.

**Path exposure.** `/paths` returns display-only strings (zero risk). `/file?root=<key>&path=<rel>`
uses a server-side root **key**, never a client-supplied path; rejects `..` and absolute paths early,
then gates on `resolve()` + `is_relative_to` — which is what actually defeats a symlink pointing out
of the run directory.

**Transcript parser.** Add `thinking` / `redacted_thinking` to `RENDERABLE` (`transcripts.py:34`) —
this is the single biggest blocker to the operator's ask, since "what the agent thought" is literally
the thing currently being filtered out, and grouping calls run at 10k thinking tokens so it is most of
the content. Add per-event `usage` from assistant rows, plus `?after_seq=` (today the 3s poll
re-downloads a whole 342-turn transcript per tick).

## Cost accounting: estimate vs actual

This requirement arrived after the design agents ran, so it is specified here.

**The prediction side already persists.** `estimate_group_tokens` (`estimator.py:19-29`) computes a
per-group figure and the pipeline writes it as `estimated_tokens` on every group in `groups.json`
(`pipeline.py:461,514`) — confirmed present in all three groupings on disk. `difficulty` and
`intensity` are alongside it, which matters because `intensity` determines whether a group gets zero,
one, or two reviewer sessions and therefore what "actual" should even be compared against.

**The actual side is lossy, and this is the fix the requirement needs.** `RoundUsage`
(`sessions.py:71-74`) correctly parses all four token classes from the envelope — `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`. Then `SessionUsage.add`
(`sessions.py:119-121`) folds `cache_creation` into `total_input_tokens` and **discards
`cache_read_input_tokens` entirely**, keeping only `last_context_tokens`. Only that last figure reaches
`manifest.json`.

So cached-vs-written-vs-uncached is computed on every round and thrown away one line later. Note also
the `iterations[-1]` correction at `sessions.py:97-104`: the envelope's top-level `usage` sums every
turn, which previously produced a 50x-inflated context reading. Any new cumulative field must sum
per-round `RoundUsage` values, **not** re-read the envelope total.

Required changes, all additive and defaulted:

- `SessionUsage` keeps four cumulative counters rather than two: `total_input_tokens` (uncached input
  only), `total_output_tokens`, `total_cache_read_tokens`, `total_cache_creation_tokens`. Preserve
  `last_context_tokens` unchanged — the circuit breaker reads it and must not shift behaviour.
- `SessionEntry` gains the four counters plus `role` (coder / reviewer), `model`, `started_at`,
  `ended_at`, `rounds_completed`, `turns`, and `outcome`. All written on **existing** saves —
  `_persist_coder_usage` already runs once per round — so zero extra disk writes.
- Per-round history is worth keeping for the sparkline described below; a `rounds: [RoundUsage]` list
  on `SessionEntry` is the cheapest place for it.

The view. Per group: `estimated_tokens` as a reference line against actual totals, broken out by role,
with each bar segmented into uncached input / cache-written / cache-read / output. Cache reads should
be visually de-emphasised — they are the cheap class, and a run whose bar is mostly cache-read is
*healthy*, not expensive. A run-level rollup gives estimator calibration: predicted vs actual across
all groups, which is the number that tells you whether `bytes_per_token` and `slack_multiplier` are
tuned. Historical runs lacking the new fields degrade to showing the estimate alone with an explicit
"actuals not recorded for this run" note and a `PathChip` to the manifest.

Worth stating plainly: the estimator predicts *context occupancy for one coder*, while the actual
totals are *cumulative spend across every round and role*. These are different quantities and the UI
must not present them as a naive ratio. The honest comparison is `estimated_tokens` vs
`last_context_tokens` for the coder session (same quantity, prediction vs outcome); cumulative spend
is a separate, adjacent panel.

## Sequenced plan

**Phase A — on `feat/multiagent-orchestrator`, before the PR.** All additive, all behind `None`
seams, each with an explicit not-touched list for the reviewer.

| #   | Unit                                                     | Status              | Files                                                                                                        |
| --- | -------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------ |
| A1  | `LlmCallRecorder` seam + grouper call records            | **shipped** e7866c0 | `grouping/llm.py`, `llm_record.py`, `mapper.py`, `speccer.py`, `pipeline.py`, `cli.py`, `execution/manifest.py` |
| A3  | `SessionEntry` + `SessionUsage` token-class split        | **shipped** e7866c0 | `execution/sessions.py`, `review.py`, `model.py`                                                               |
| A2  | Edge-provenance ledgers + `edge-provenance.json` sidecar | not started         | `grouping/graphing.py`, `pipeline.py`                                                                          |

A1 and A3 landed together in `e7866c0`, with 608 tests green. What that commit does **not** touch, for
the reviewer: partition arithmetic, edge weights, review-loop control flow, the breaker's
`last_context_tokens` input, and `groups.json` — a timestamped id there would break
`serialize_grouping`'s determinism contract, so the call-to-output join lives on the record's side.
Recording is inert and best-effort: an audit write cannot fail a grouping, and a CLI that rejects
`--session-id` falls back to the previous argv.

**A1 still needs a live verification.** The `--session-id` + `--json-schema` pairing is covered only by
mocked-subprocess tests. The first real `group` run should confirm that
`.orchestrator/groupings/<name>/llm/calls.json` carries a non-null `claude.transcript_path`. Null
transcript paths with absent session ids is the signal that the degrade path fired and the pairing is
unsupported on the installed CLI.

A2 is deliberately deferred: it is the largest of the three, the only one touching `graphing.py`'s
arithmetic surface, and the Grouping tab ships without it against existing `grouping-trace.json` data.

**Phase B — the merge.** `main` → `feat/multiagent-orchestrator` → `feat/observatory`, with the F4
crash fix (`run_groups_path()` shared helper covering both `runs.py:189` and `events.py:115`, plus a
`RunPaths` attribute-audit guard test) and fixture regeneration in the merge commit. Nothing else is
testable until the stale fixtures are rebuilt.

**Phase C — on `feat/observatory`, six independent commits.** Drift repair (named groupings via a
`dag_source` resolution step keeping `stale_dag` semantics verbatim; `manifest.escalation`, which is
the operator's worst-rated blind spot; the missing enum members in both Python and `types.ts`); the
new `observatory/grouping.py` router; transcript parser fixes; the path/file API; the heartbeat; then
the UI work. The UI splits further: router + token layer + tab shell first, then the Grouping tab
(shippable against existing `grouping-trace.json` data), then the attempt grid, then the cost panel.

## Ranked risks

1. **The frozen-branch objection.** Phase A touches orchestrator core on a PR the operator wanted
   unchanged. Mitigation is the four-line invariant: every seam defaults to `None`, no arithmetic
   changes, no existing test stub churns, and nothing new is ever read back into a decision.
2. **`--session-id` with `--json-schema` is untested in combination.** If the CLI rejects the pair,
   A1's degrade path must leave `group` working exactly as today.
3. **Fixture staleness blocks all of Phase C.** Regenerate in the merge commit or nothing downstream
   can be verified.
4. **SPA fallback route.** Without it, deep-linkable URLs — the whole point of adding a router — 404
   on refresh. Confirm early; HashRouter is the fallback.
5. **Estimator comparison is easy to present dishonestly.** Prediction is per-coder context occupancy;
   actuals are cumulative multi-role spend. Keep them in separate panels.
6. **Saturated-graph sidecar size.** Capped, with counters that admit truncation rather than hiding it.

## Open questions

- Cross-run comparison in the attempt grid — recommend deferring.
- Log as a tab vs a persistent rail.
- Is `intensity` reliably present in every `groups.json` on disk? It drives the expected reviewer-
  session count and therefore the self-verify degradation path.
- Does showing a copyable `resume` command on interrupted groups sit too close to the run-control
  non-goal? Recommend showing it as copyable text, not a button.
- Whether to write the edge-provenance sidecar under `--no-spec` too. Recommend yes — that is the
  debugging mode.
