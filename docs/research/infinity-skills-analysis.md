# infinity-skills — ingestion & analysis-readiness report

Repo: `/home/gbm1996/wksp/infinity-skills`. Purpose (per `README.md`, `CLAUDE.md`): an offline
memory/skill-mining engine over Claude Code session transcripts — extract → enrich → graph →
retrieve → mine skills → eval. This report focuses on what matters for a **new** orchestrator
(Python launches `claude --session-id <uuid>` workers, one per "execution group", tracked via a
run manifest `run → groups → session UUIDs`, each group carrying `id` / `name` / `summary` / `spec`)
to emit data that infinity-skills (or an extension of it) can ingest, join, and mine "procedural
behaviours" from across many orchestrated runs.

All findings below are grounded in the actual source (file:line) and in a live inspection of
`~/.claude/projects/**` on this machine (the same transcript tree infinity-skills reads).

______________________________________________________________________

## 1. Ingestion pipeline

### File discovery

- Root: `settings.transcripts_root = Path.home() / ".claude" / "projects"`
  (`src/infinity_skills/config.py:23`), overridable via `INFSKILLS_TRANSCRIPTS_ROOT`.
- Main-session discovery is a flat two-level glob: `root.glob("*/*.jsonl")` — run twice,
  independently, in the two extractor entry points: `extract_corpus`
  (`src/infinity_skills/extract/observations.py:533`) and the ingest-once `run_extract`
  (`extract/observations.py:1036`). `path.parent.name` is the **raw encoded project directory**
  (Claude Code's own cwd-encoding, e.g. `-home-gbm1996-wksp-infinity-skills`) and `path.stem` is the
  **session id** (`extract/observations.py:537`, `:1041`) — the transcript filename IS the session
  UUID, so "join to observations" is direct by construction (`extract/transcripts.py:1-8` docstring).
- **Project allowlist** (the single ingest choke point): `settings.ingest_projects = ["infinity-skills", "gionodes"]` (`config.py:33`). A transcript is ingested only if its raw project-dir name
  **substring-matches** one of these (`observations.py:535`, `:1038`); an empty allowlist means
  "ingest everything." `canonical_project()` (`observations.py:493-505`) then maps the matched raw
  dir name to the short logical label — this is how e.g. `…-wksp-infinity-skills` and
  `…-infinity-skills-frontend` both collapse to `infinity-skills`. **Confirmed live**: this machine's
  `~/.claude/projects/` has a real worktree directory,
  `-home-gbm1996-wksp-infinity-skills--claude-worktrees-feat-replay-goal-grouping`, sitting alongside
  the main `-home-gbm1996-wksp-infinity-skills` — because the worktree path still contains the
  substring `infinity-skills`, it passes the allowlist and canonicalizes to the same project. This
  matters a great deal for the new orchestrator — see §6.
- Subagents live in a **separate** directory, not inline in the parent `.jsonl` with an
  `isSidechain` flag: `<project_dir>/<session_id>/subagents/agent-<id>.jsonl` plus a sibling
  `agent-<id>.meta.json` (`_subagent_files` / `_read_agent_meta`, `observations.py:978-998`). Verified
  live: this exact layout, plus a `tool-results/` sibling directory infinity-skills does not read.
  `agent-<id>.meta.json` carries `{agentType, description, toolUseId}` — `toolUseId` is the parent
  Task/Agent `tool_use` id that spawned it, the boundary↔sub-session link
  (`observations.py:987-998`, wired in `run_extract` at `:1067-1082`).

### Schema/model per JSONL line

- `Event` (`extract/transcripts.py:42-68`) is the line-level model: only `"assistant"`/`"user"` line
  `type`s are read (`_MESSAGE_LINE_TYPES`, `:30`); only four content-block kinds are emitted —
  `thinking` / `text` / `tool_use` / `tool_result` (`_KIND_BY_BLOCK`, `:31-37`). Everything else
  (`queue-operation`, `ai-title`, `file-history-snapshot`, `summary`, `mode`, …) is skipped.
- **Deduplication by `uuid`** (`_events_from_file`, `:125-171`) — resume/branch/compaction re-logs the
  same message record later in the file; first occurrence wins (test-covered, flagged in
  `CLAUDE.md` Known traps).
- **Fields actually captured**: `role`, `kind`, `ts` (line `timestamp`), `tool_name`, `files` (from
  `tool_use.input.file_path`/`path`/`notebook_path`), `tool_input`, `is_error`, `text`,
  `tool_use_id`, `is_meta`, `is_compact_summary` (`Event` fields, `:48-68`).
- **Fields present in the raw JSONL but NEVER read anywhere in the codebase**: `cwd`, `gitBranch`,
  `isSidechain`, `version`, `userType`, `parentUuid`. Confirmed two ways: (a) `grep -rniE "cwd|gitbranch|isSidechain"` over `src/` returns zero real hits (only an unrelated docstring use
  of the word "cwd"); (b) a live parse of a real transcript line on this machine shows the line
  actually carries `cwd=/home/gbm1996/wksp/infinity-skills`, `gitBranch=feat/session-summary-judge-loop`,
  `isSidechain=False` — none of which reach `Event`, `ActionObservation`, or the `sessions` table.
  **This is the single biggest gap for the new orchestrator's join needs** — see §2 and §6.

### Subagent / sidechain handling

- A `tool_use` named `Agent` or `Task` (`SUBAGENT_TOOLS`, `observations.py:56`) becomes an
  `EpisodeBoundary` / `observation_boundary` row, not a regular action — the action stream is
  deliberately not polluted by orchestration tool calls (`observations.py:20`).
- Each subagent's own `.jsonl` is parsed as a **first-class sub-session** with id
  `f"{session_id}.agent.{agent_id}"` (`run_extract`, `observations.py:1063`), written to `corpus.db`'s
  `sessions` table with `kind="sub_agent"`, `parent_session_id`, `spawned_by_obs_id` (the parent
  boundary obs), and `agent_type` (`observations.py:1075-1086`, `corpus_store.py:44,427-437`). The
  parent boundary row is back-patched with `spawned_session_id` once the child is known
  (`observations.py:1070-1071`).
- This nested parent→children structure is exactly what the session-replay UI walks
  (`server/routers/sessions.py:306-321` `_load_session_tree`, recursing on
  `spawned_session_id`) — i.e. infinity-skills already has a working **multi-session tree**
  join, just scoped to "one Claude session's own subagents," not "N independent CLI processes
  in one orchestrated run."

### Identifiers extracted

- **Session id** = transcript filename stem (implicit primary key everywhere: obs ids, goal ids,
  episode ids are all `{prefix}:{session_id}:{n_or_tool_use_id}`).
- **cwd**: NOT extracted (see above) — the only cwd-adjacent signal is the raw *project directory
  name* (`path.parent.name`), a coarse, ingest-time-only string, canonicalized via substring match.
- **git branch**: NOT extracted anywhere.
- **Timestamps**: per-line `timestamp` (ISO-8601) → `Event.ts` → observation `timestamp`; session
  `started_at` = min observation timestamp (`observations.py:1009-1011`, `:1097`). No end time is
  stored.
- **Subagent identity**: `agentType` / `description` / `toolUseId` from the `.meta.json` sidecar
  (real Claude Code subagent metadata) — this is the closest existing analogue to a "task name" per
  sub-run, and it already flows into the summarizer as a preamble (`enrich/summarize_session.py:458-486`
  `subagent_goal`, prepending `agent_type` before the delegated goal text).

### Where parsed data lands

- **`data/corpus.db`** (SQLite, `extract/corpus_store.py`) is the single source of truth (ADR 0006 +
  0008; `CLAUDE.md` Known traps). Three families of tables: `sessions` (one row per session or
  sub-agent, `SCHEMA` at `corpus_store.py:47-56`), `observations` (every typed observation, `obs_type`
  ∈ `user_instruction` / `system_directive` / `assistant_thought` / `assistant_text` / `action` /
  `observation_boundary`, `:57-100`), `observation_raw` (linked full-text overflow, `:101-106`). A
  second layer of "folded enrich/mine" tables (`episodes`, `signatures`, `causal_edges`,
  `skill_candidates`, `session_summaries`, `concept_aliases`, `concept_constraints`, `artifacts`,
  `:110-165`) is entirely regenerated by later pipeline stages except `concept_constraints`, which is
  learned and survives `clear()` (`corpus_store.py:410-425`).
- Extraction runs **exactly once** per `.jsonl` (`run_extract`, `observations.py:1014-1121`); every
  downstream stage (enrich, graph, mine, serve) reads `corpus.db`, never the raw transcript again
  (ADR 0008; `events_from_db`, `observations.py:844-897`, reconstructs `Event`s from the DB for the
  handful of offline readers that still want event shape).
- A legacy in-memory `ObservationSet` / `observations.json`-shaped dict (`extract/observations.py:344-368`,
  `payload()` at `:562-611`) is still built transiently for a goal-noise ledger and tests, but is
  never written to disk in the current substrate (ADR 0008/0010 retired the JSON dual-write).

______________________________________________________________________

## 2. Join/linking model

- **Primary key**: `sessions.id` (= transcript filename stem = the Claude Code session UUID).
  Every observation row carries `session_id` as a foreign key (`corpus_store.py` `observations.session_id`,
  indexed at `:99`), and `sessions.parent_session_id` / `spawned_by_obs_id` form the ONLY existing
  cross-session edge: subagent → parent (`corpus_store.py:601-607` `child_sessions`).
- **Project** (`sessions.project`) is the only other grouping key, and it is explicitly
  **display/filter-only, never a mining key** (ADR 0007, `CLAUDE.md` Known traps: "`project` is
  canonicalized (`canonical_project`) and display/filter-only — not a mining key"). It scopes the FE
  project picker and `RetrievalPipeline.query(project=...)` (`retrieval/pipeline.py:57-66`,
  `retrieval/anchors.py:179-198` `node_project_map`), nothing more.
- **There is no "run" or "task-group" entity anywhere in the schema.** The closest UI concept —
  `RunGroup` / `groupTimeline.ts` (`frontend/src/components/groupTimeline.ts:1-151`) — is a
  **client-side-only, synthetic** grouping over one session's own timeline (adjacency-partitioning
  nodes by `spawned_session_id` to collapse repeated subagent runs in the replay view); the backend
  never sends a `RunGroup`/`GoalGroup` (explicit in the file's own header comment, `:12`). It has
  nothing to do with joining independent CLI processes across a multi-session orchestration.
- **Session "title"** is auto-derived, not externally supplied: the first genuine `marker_kind=='goal'`
  user prompt (`_first_goal_text`, `observations.py:1001-1006`; `sessions.title` populated at
  `observations.py:1098`); this is also literally the text fed to the per-session summarizer as
  `goal` (`enrich/summarize_session.py:349-353` `goal_by_session`) and to the graph's `session` node
  text (`graph/build.py:513-518,527`). **This is the one channel the pipeline already treats as "what
  is this session's identity/objective," and it is populated purely from prompt content** — no cwd,
  no branch, no external id ever factors in.
- **Natural extension point for a run-manifest join**: `CorpusStore.upsert_session()`
  (`corpus_store.py:427-446`) already takes an arbitrary session dict with a fixed column list; adding
  columns (`run_id`, `group_id`, `group_name`) here is a one-line schema change
  (`SCHEMA`/`_OBS_MIGRATIONS` pattern already exists for exactly this kind of additive column,
  `corpus_store.py:174-180`) and `upsert_session`'s column list would need the same three names added
  at `:428-437`. Absent a code change, the **only** channel that survives ingestion unmodified today is
  the first-prompt text (because it becomes `sessions.title` + the summarizer's `goal` + the graph's
  session-node `text`) — which is exactly why "embed group id/name/summary in the first prompt in a
  predictable delimited block" (§6) is the correct near-term join mechanism without waiting on a
  schema change.

______________________________________________________________________

## 3. Auto skill generation

- Skills are mined **exclusively from repeated fail→fix chains keyed by `error_signature`**, not
  from task/procedure identity. Pipeline: `causal_edges` (kind=`FIXES`, confidence ≥
  `skill_fix_conf_floor`) are bucketed by `signature` (`skills/mine.py:98-113` `fixes_by_sig` /
  `cluster_edges`), then a bucket only becomes a `SkillCandidate` if it has ≥ `skill_min_fixes` edges
  from ≥ `skill_min_sessions` **distinct sessions** (`skills/mine.py:120-121`,
  `config.py:192-193`). So the cross-session signal the miner needs is: the *same* normalized
  error/operation pair recurring across ≥2 sessions.
- Each candidate's evidence window is the **verbatim chronological episode** around the error/fix
  (`_window_for`, `skills/mine.py:50-68`, preferring the real episode boundary over a ±1 slice), fed
  to DeepSeek with a hard grounding rule: every step must cite an observation id that appears in the
  shown evidence, or it is discarded (`_grounded_steps`, `skills/distill.py:109-126`) and re-audited
  by a second "skeptical auditor" LLM pass (`ground_check`, `skills/distill.py:210-262`) — anti-
  fabrication is a first-class design concern here (`CLAUDE.md` "Distiller CITATION-WASHES" trap).
- Trigger matching at injection time is **structured, not semantic**: `error_sig` / `tool` /
  `file_glob` / `input_prefix`, AND-combined (`skills/trigger_index.py:114-172`) — this is a
  deterministic SQLite lookup on the hook path (import-hygiene enforced, `trigger_index.py:1-11`), no
  LLM call at match time.
- **What input structure makes this mining work well, generalized to "procedural behaviours across
  runs" (the new use case)**: the current signal space is narrow (error text pattern), but the
  underlying mechanism — bucket by a stable key, require ≥N distinct sessions, keep the verbatim
  evidence window, ground every generalization back to cited observation ids — generalizes directly
  to a **group-name-keyed** bucket instead of (or alongside) an error-signature-keyed one: if a
  worker session's `active_goal_id`/title/summary can be traced back to a stable external group
  `name` (per §2's join gap), the same "≥N sessions with the same name showed the same tool-sequence
  pattern" clustering becomes possible. Today nothing plays that role because there is no field that
  is stable across sessions except `project` (too coarse) and free-text titles (unstable, LLM-
  paraphrased per session). A literal, parseable group name is the missing input.
- `action_type` / `error_signature` derivation (`enrich/error_sigs.py:22-86`) is a two-level scheme:
  a rule-based `operation:error_class` primary key (works identically online/offline) plus Drain3
  masking as metadata only — worth knowing if the new pipeline wants an analogous
  "cross-run-stable procedure key" rather than reusing this file verbatim.

______________________________________________________________________

## 4. Memory retrieval

- Indexed corpus = graph nodes whose `node_type` is in `RETRIEVABLE_TYPES = ("obs", "summary", "lesson", "crystal")` (`graph/build.py:48`), each carrying a `text` field built from
  title+narrative+local_intent+facts (`_action_text`, `graph/build.py:413-424`) or
  title+narrative+key_decisions for summaries (`build_graph_from_observations`,
  `graph/build.py:598-608`). `concept`/`file`/`symbol`/`session`/`err` nodes are **conduits** — they
  connect retrievable nodes but are excluded from results (`CONDUIT_TYPES`, `graph/build.py:50`,
  `corpus_from_graph`, `retrieval/anchors.py:159-176`).
- Query path (`retrieval/pipeline.py:77-110`): intent classification → paraphrase expansion → hybrid
  BM25+dense anchor selection with Reciprocal Rank Fusion + a cross-encoder relevance floor
  (`retrieval/anchors.py:63-144`) → intent-routed Personalized PageRank over the typed edge graph
  (`retrieval/ppr.py`, edge weight presets by intent) → cross-encoder rerank → token-budgeted
  assembly (`retrieval/assemble.py`).
- **Project scoping is the one metadata dimension retrieval already supports as a first-class query
  parameter**: `RetrievalPipeline.query(project=...)` builds a **scoped anchor view** whose BM25 index
  is re-tokenized over just that project's nodes and whose dense matrix is sliced (no re-encode) from
  the parent's already-encoded matrix (`retrieval/pipeline.py:57-66`, `retrieval/anchors.py:146-156`
  `scoped`). `node_project_map` (`retrieval/anchors.py:179-198`) resolves an obs/summary/crystal
  node's project by walking its `PART_OF_SESSION`/`SUMMARIZES` edge back to its session's `project`
  attribute — i.e. **project is the only metadata dimension that propagates through the graph for
  scoping**, everything else (importance, is_marker, is_error, action_type, intent_label) is a node
  attribute usable for ranking/filtering logic but has no equivalent "scope the whole retrieval to
  this value" convenience today.
- Metadata that measurably improves retrieval quality in the current design: `importance` (1-10,
  Generative-Agents-style poignancy, feeds PPR teleport weighting and hub damping — ADR 0007),
  `is_marker` (down-weights bookkeeping actions from ever seeding anchors/PPR — ADR 0007), and typed
  `concepts`/`symbols`/`files` (drive `MENTIONS_CONCEPT`/`MENTIONS_SYMBOL`/`TOUCHES_FILE` edges used
  for graph expansion). A hypothetical `group_id` metadata field, if added, would most naturally slot
  in exactly where `project` sits today — a session-node attribute inherited by its obs/summary nodes
  and exposed as a `RetrievalPipeline.query(group=...)` scoping parameter, mirroring `_anchors_for`
  (`retrieval/pipeline.py:57-66`).

______________________________________________________________________

## 5. Session visualization

- **Session Replay** (`frontend/src/views/SessionReplay.tsx`, backed by
  `server/routers/sessions.py`): a nested, time-ordered timeline built **entirely from `corpus.db`**
  (`merge_observation_timeline`, `sessions.py:237-284`) — one node per typed observation
  (`action`/`assistant_thought`/`assistant_text`/`user_instruction`/`system_directive`/
  `observation_boundary`), with a subagent boundary recursively nesting its spawned sub-session's own
  timeline as `children` (`sessions.py:260-264`) and, if summarized, its `session_summaries` row
  inlined under the boundary (`sessions.py:212-215`). Session-list rows show `id`, `title`, `project`,
  `started_at`, `obs_count`, and a `summary_title`/`summary_snippet` when available
  (`_list_sessions_transcript`, `sessions.py:329-352`).
- Frontend-only `groupTimeline.ts` (`frontend/src/components/groupTimeline.ts`) layers a further
  client-side collapse: partition by adjacent `goal` markers, then by adjacent `run-key`
  (= `spawned_session_id` for boundary nodes, else a sentinel) so repeated subagent runs collapse
  into one `RunGroup` with a "show more"/"show all" affordance (`RunGroupRows.tsx:46-105`) — driven
  entirely by fields already on the timeline node (`kind`, `spawned_session_id`, `obs_id`, `children`).
- **Graph Explorer** (`frontend/src/views/GraphExplorer.tsx`, `server/routers/graph.py`): ego-graph
  neighborhoods, typed subgraphs, and the FIXES evidence subgraph behind a mined signature, rendered
  as Cytoscape.js elements (`{data:{id,label,node_type}}` / `{data:{source,target,edge_type,weight}}`,
  `graph.py:38-52`). Node-detail drill-down (`node_detail`, `graph.py:314-417`) joins straight back to
  `corpus.db` for an `obs` node (title/narrative/facts/files/concepts/symbols/importance/confidence/
  the precise tool call) and to `session_summaries` for `summary`/`session` nodes.
- Fields that drive the visuals, concretely: `timestamp` (ordering), `node_type`/`obs_type`
  (badge/icon), `is_error`/`soft_error` (red flag), `is_marker` (a dimmed "marker" chip, ADR 0007 §5),
  `importance`/`confidence` (weight/opacity signals), `project` (the FE picker + Query scope),
  `title`/`narrative`/`facts` (the legible body). None of these fields currently distinguish "which
  orchestrated run/group this session belongs to" — that would have to ride on `project` (too coarse,
  shared across all sessions of a repo) or be read out of the title/goal text.

______________________________________________________________________

## 6. Concrete recommendations for the new orchestrator

Each recommendation is grounded in a specific ingestion behavior above, so infinity-skills (as-is or
lightly extended) can join and mine the orchestrator's sessions with minimal/no code changes.

1. **Embed a predictable, delimited identity block as the first user message of every worker
   session**, e.g.:

   ```
   <run-manifest run_id="r-2026-07-14-abc" group_id="g3" group_name="fix-flaky-auth-test">
   <summary>...tldr...</summary>
   </run-manifest>
   <spec>...full task detail...</spec>
   ```

   This is the *only* channel guaranteed to survive ingestion unmodified today: the first genuine
   `marker_kind=='goal'` prompt becomes `sessions.title` (`observations.py:1001-1006`,`:1098`), the
   summarizer's `goal` input (`enrich/summarize_session.py:349-353`), and the graph session-node
   `text` (`graph/build.py:513-518`). Anything after `run_id="..."` in a parseable tag is trivially
   regex-extractable by a downstream join step even with zero infinity-skills code changes; a plain
   free-text mention would not be. Keep `summary` short (it is what several downstream consumers
   truncate to — session title caps at 120 chars, `observations.py:1084`,`:1098`) and put the full
   `spec` after it so a length cap never truncates the identity block itself.

1. **Register a plain, parseable session display name via whatever mechanism your launcher exposes**
   (e.g. terminal title / tmux pane name / process title) but do NOT rely on it reaching
   infinity-skills — nothing in the ingestion path reads any display-name channel outside the JSONL
   content itself (`Event` only reads `role`/`kind`/`ts`/`tool_name`/`files`/`tool_input`/`is_error`/
   `text`/`tool_use_id`/`is_meta`/`is_compact_summary`, `extract/transcripts.py:48-68`). Treat #1's
   in-prompt block as the actual name-carrying channel; a display name is operator convenience only.

1. **Write the run manifest to a stable, discoverable path outside `~/.claude/projects/`** — e.g.
   `<repo>/.orchestrator/runs/<run_id>.json` mapping `run → groups → session_uuid`. infinity-skills
   has no manifest reader today, but the join is trivial for a future ingestion step: manifest
   `session_uuid` values are exactly `corpus.db`'s `sessions.id` primary key (`corpus_store.py:47`),
   so a post-extract enrichment pass can `UPDATE sessions SET run_id=?, group_id=?, group_name=? WHERE id=?` using `CorpusStore.upsert_session()`'s existing upsert pattern (`corpus_store.py:427-446`) —
   add the three columns to `SCHEMA`/`_OBS_MIGRATIONS` (`corpus_store.py:46-56`,`:174-180`, the exact
   pattern already used for post-ship columns like `goal_boundary`) rather than only relying on #1's
   in-prompt fallback.

1. **Run every worker inside a worktree whose path retains the base repo directory name as a
   substring** — do not put worktrees under an unrelated temp path (e.g. `/tmp/run-<hash>/w1`).
   `extract_corpus`/`run_extract` only ingest a transcript if `path.parent.name` (the raw encoded cwd)
   substring-matches an entry in `settings.ingest_projects` (`observations.py:535`,`:1038`,
   `config.py:33`); a worktree path that doesn't retain the repo name is **silently excluded from
   ingestion entirely** — not mis-labeled, just dropped. This machine's own dev workflow already
   proves the safe pattern works: `.claude/worktrees/<branch-slug>/` (created by this repo's own
   `ce-worktree`/`EnterWorktree` flow) produces a project dir
   `-home-…-infinity-skills--claude-worktrees-feat-…`, which still contains `infinity-skills` and
   therefore folds into the same canonical project via `canonical_project()` (`observations.py:493-505`).
   Mirror that: nest orchestrator worktrees under `<repo>/.worktrees/<group_id>-<name>/` or similar,
   never under a path with no relation to the repo name. If cross-repo runs are possible, either add
   each repo's own allowlist substring or set `ingest_projects = []` (ingest everything) — but note an
   empty allowlist skips `canonical_project`'s folding too (`observations.py:502-505`), so differently
   -pathed worktrees of the *same* logical project would then show up as *different* raw `project`
   labels; either keep the substring convention, or extend `canonical_project` with an explicit
   alias table.

1. **Emit a stable, machine-parseable status/report JSON as the LAST assistant text block**, not
   only a prose wrap-up — e.g. a fenced block:

   ```
   <run-report status="done" group_id="g3">{"files_touched": [...], "outcome": "..."}</run-report>
   ```

   infinity-skills has no existing "final status" field (a session has no end time, no exit/outcome
   column in `sessions` — `corpus_store.py:47-56`), and the summarizer must currently *infer*
   `key_decisions`/`files_modified` from the whole action stream (`enrich/summarize_session.py:40-55`
   `SUMMARY_SYSTEM`). A structured final block gives any future ingestion/summary pass a
   ground-truth outcome to check against or seed from, instead of purely LLM inference — directly
   useful the same way `agent_type` (captured from `.meta.json`, `observations.py:987-998`) already
   feeds `subagent_goal`'s preamble (`enrich/summarize_session.py:458-486`) as a non-inferred, typed
   signal.

1. **Do not depend on `cwd` or `gitBranch` reaching infinity-skills** — confirmed zero read sites for
   either field anywhere in `src/` (grep across `extract/`, `enrich/`, `graph/`, `retrieval/`,
   `skills/`, `server/`), even though the raw transcript line always carries both (verified live:
   `cwd=/home/gbm1996/wksp/infinity-skills`, `gitBranch=feat/session-summary-judge-loop` on a real
   line). If per-worker git-branch identity ever needs to reach infinity-skills' data model (e.g. to
   tell two workers on different branches of the same repo apart), it currently has to ride in the
   in-prompt block (#1) or be added as a two-line extension to `Event`
   (`extract/transcripts.py:42-68`) + `ActionObservation`/`sessions` row — a small, well-precedented
   change (mirrors how `is_meta`/`is_compact_summary` already ride line-level flags onto every
   `Event`, `transcripts.py:161-164`).

1. **Keep the group `summary` short and put it where the session's own first goal marker would
   naturally be** (not buried mid-spec) — the pipeline privileges the FIRST genuine
   `marker_kind=='goal'` prompt specifically (`_classify_prompt`/`_goal_drop_reason`,
   `observations.py:371-377`,`:129-136`); a prompt that *looks* like an injected wrapper (leading
   `<command-...>`, `<system-reminder>`, etc., `_INJECTED_PREFIXES`, `observations.py:85-96`) is
   silently dropped as noise, not kept as a goal. If the identity block in #1 is itself wrapped in a
   tag pattern resembling those prefixes, it risks being classified as harness noise rather than a
   genuine goal — verify the exact tag names chosen for #1 don't collide with
   `_INJECTED_PREFIXES`/`_CAVEAT_RE`/`_CONTINUED_RE` (`observations.py:85-103`).

1. **One session UUID per worker process, pre-assigned via `--session-id`, is exactly the assumption
   the pipeline already relies on** — `session_id = path.stem` is treated as authoritative everywhere
   (obs/goal/episode ids are all namespaced under it). No change needed here; just confirm the
   orchestrator never reuses a UUID across two different groups/runs (this would silently merge their
   observations under one `sessions` row via `INSERT OR REPLACE` semantics,
   `corpus_store.py:453-457`, `upsert_session` at `:427-446`).

1. **Write per-group logs somewhere infinity-skills' allowlist/glob pattern won't try to ingest as a
   transcript** — `extract_corpus` globs `root.glob("*/*.jsonl")` unconditionally under
   `transcripts_root` (`observations.py:533`,`:1036`); if per-group orchestrator logs are ever placed
   under `~/.claude/projects/<anything>/*.jsonl` they will be parsed as (malformed, mostly-empty)
   transcripts. Keep orchestrator-level logs under the run-manifest path from #3, not under
   `transcripts_root`.

1. **If/when procedural-behaviour mining across runs is built, key candidate buckets on the injected
   `group_name` (from #1/#3) the same way `skills/mine.py` keys buckets on `error_signature`**
   (`mine_candidates`, `skills/mine.py:98-121`): require ≥N distinct sessions sharing the same
   `group_name` before promoting a pattern, keep the verbatim chronological evidence window per
   episode (`_window_for`, `skills/mine.py:50-68`), and reuse the existing anti-fabrication
   generate-then-verify seam (`distill`/`ground_check`, `skills/distill.py:129-262`) rather than
   inventing a new grounding mechanism — it is the part of this pipeline most directly reusable
   for "did group X's workers consistently do Y" mining, since it already solves "cite the exact
   observation ids a generalization is allowed to draw from."

1. **Expect `project` scoping, not a run/group scoping, if you plug straight into today's
   retrieval API** — `RetrievalPipeline.query(project=...)` (`retrieval/pipeline.py:57-66`) is the
   only first-class scoping dimension that exists; a `group`/`run` equivalent would need the same
   "session attribute inherited by its obs/summary nodes via `PART_OF_SESSION`/`SUMMARIZES`" pattern
   `node_project_map` already implements (`retrieval/anchors.py:179-198`) — straightforward to clone
   once `sessions.group_id` exists (recommendation #3), but not present today.

1. **Don't expect subagents spawned by an orchestrated worker to auto-join across workers** —
   infinity-skills' only cross-session edge is parent→child via `Agent`/`Task` tool calls within ONE
   Claude Code process (`SUBAGENT_TOOLS`, `observations.py:56`; `parent_session_id`/
   `spawned_by_obs_id`, `corpus_store.py:47-53`). Two independent `claude --session-id` worker
   processes launched by the orchestrator have **no** structural edge between them today — the
   run-manifest join (recommendations #1/#3) is not a nice-to-have, it is the *only* way two
   orchestrated worker sessions become related in infinity-skills' data model at all.
