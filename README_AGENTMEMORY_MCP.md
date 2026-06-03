# AgentMemory MCP Proxy — Architecture & Improvement Notes

## Design Philosophy

The MCP is a **router and response shaper**, not a search engine. All relevance ranking is delegated to the agentmemory engine. The proxy does three things:

1. **Route intent** — map agent intent (find, execute, save, plan) to the right combination of agentmemory API endpoints
2. **Compose** — run parallel API calls and fuse responses into one structured packet
3. **Shape** — compact formatters trim noise before returning to the agent's context window

The proxy never reimplements what the engine already does (BM25, vector scoring, graph traversal). The only client-side filtering is a simple set-intersection to follow observation hits into memory stores that have no native search index.

---

## The agentmemory Engine — What It Actually Indexes

Understanding what is and isn't indexed in the engine is the foundation of the whole proxy design.

| Store                   | Engine index                          | Score field                      | How to reach it                          |
| ----------------------- | ------------------------------------- | -------------------------------- | ---------------------------------------- |
| `CompressedObservation` | ✅ BM25 + vector (HNSW cosine)        | `combinedScore` (RRF fusion)     | `POST /smart-search` or `POST /search`   |
| `Lesson`                | ❌ inline term overlap only           | `confidence × overlap × recency` | bundled in `POST /smart-search` response |
| `Memory` (all types)    | ❌ none — `GET /memories` ignores `q` | `strength` (decay only)          | list + client set-intersection           |
| `SemanticMemory`        | ❌ none                               | `strength`                       | list + filter by `sourceSessionIds`      |
| `ProceduralMemory`      | ❌ none                               | `strength`                       | list + filter                            |
| `Insight`               | ❌ none                               | `confidence`                     | `GET /insights?minConfidence=`           |
| `Crystal`               | ❌ none                               | none                             | `GET /crystals`                          |
| `GraphNode/Edge`        | ❌ graph traversal only               | `weight` on edges                | `POST /graph/query`                      |

**The key implication:** `CompressedObservation` is the only store with a real scored search index. Memories, insights, and crystals are reached by _following links from observations_. Every consolidation path traces back to `sourceObservationIds` on memories and `sourceSessionIds` on semantic memories — this is how the proxy bridges scored hits into unindexed stores.

### BM25 + Vector Fusion (observations)

`POST /search` and `POST /smart-search` both use the same `SearchIndex` (BM25 term-frequency) and `VectorIndex` (cosine HNSW). The fusion is RRF-style: `combinedScore` is derived from BM25 rank + vector rank merged into one float, pre-sorted descending. Every `CompressedObservation` is indexed on: `title + subtitle + narrative + facts + concepts + files + type`.

`POST /smart-search` extends `/search` with:

- **Graph stream** — attaches `graphContext` to results when the knowledge graph has edges connecting the observation to related concepts, files, or decisions
- **Bundled lesson recall** — runs `mem::lesson-recall` in parallel and returns `lessons[]` alongside observation hits

### Lesson Scoring

`mem::lesson-recall` does not use BM25. It scores lessons with: `confidence × (matchCount / terms.length) × recencyBoost` where `matchCount` is a plain whitespace-split term overlap against the lesson's text. Better than stopword matching but still keyword-level — it doesn't handle synonyms, stemming, or semantic similarity.

### `expandIds` Mode

When `POST /smart-search` receives `expandIds`, the query path is **skipped entirely**. It resolves each observation ID directly from KV storage (using `sessionId` as a fast-path hint), records access for retention scoring, and returns full `CompressedObservation` objects. No BM25, no vector embedding, no scoring overhead. This is the ideal path when `sourceMemoryIds` are known.

---

## Memory Link-Following Pattern

Because `GET /memories` ignores its `q` parameter server-side, the proxy uses a link-following pattern instead of custom scoring:

```
POST /smart-search  →  top-5 observations  →  extract:
  - observation IDs  →  match memory.sourceObservationIds
  - session IDs      →  match semantic/procedural memory.sourceSessionIds
  - concepts[]       →  set-intersection with memory.concepts
  - files[]          →  set-intersection with memory.files
```

Memories that share any of these fields with the top-scored observations **inherit their relevance by proxy** — they exist because of the same events your query is most similar to. The result is sorted by `memory.strength` (the engine's decay-weighted durability score) as a secondary rank.

This is a deliberate simplification. It is not IDF-weighted, not stemmed, not semantic. It is a transparent, maintainable workaround for the current engine limitation.

---

## The 7 MCP Tools

### 1. `memory_find`

**Agent intent:** "What do we already know about X?"

**Engine endpoints called (parallel):**

- `POST /agentmemory/smart-search` — BM25+vector+graph on observations; lessons bundled
- `POST /agentmemory/enrich` — only when `files=` provided; file-graph cross-session context
- `GET /agentmemory/insights` + `GET /agentmemory/crystals` — only when `depth="deep"`
- `GET /agentmemory/memories` — always; used for link-following, not direct scoring

**Client-side logic:** set-intersection only (`_follow_memories`). No stopwords, no IDF.

**Replaces:** `memory_smart_search` + `memory_enrich` (two tools → one)

**Flow:**

```
query or files
  ├── query → POST /smart-search  ──────────────────────────┐
  ├── files → POST /enrich (parallel)  ─────────────────────┤
  ├── always → GET /memories (parallel)  ──────────────────┐│
  │                                                         ││
  └── fuse:                                                ││
        observations (scored by engine)  ←─────────────────┘│
        memories (linked via set-intersection)  ←────────────┘
        lessons (bundled from smart-search)
        enriched_file_context / bug_candidates (if files)
        insights / crystals (if depth=deep)
```

**To improve:**

- `GET /memories` ignores `q` server-side — all memory filtering is client-side. When agentmemory adds `POST /memories/search` with a real vector index, replace the link-following pattern with engine-native memory search; `memory_find` becomes 2 parallel calls instead of 3.
- Lesson scoring is simple term overlap, not BM25. When agentmemory adds a lesson vector index it will improve automatically — no proxy change needed.
- `depth="deep"` adds insights + crystals but they have no query-relevance score, only `confidence`/`strength`. A future improvement: pass top concepts from observation hits as a filter to `GET /insights?concept=` if agentmemory adds concept filtering.
- The graph stream in `/smart-search` is only meaningful when `GRAPH_EXTRACTION_ENABLED=true` and consolidation has run. Currently no detection for cold graph — `graphContext` simply won't appear on results when cold; no error is raised.

---

### 2. `memory_task_context`

**Agent intent:** "Give me everything I need to execute this specific action."

**Engine endpoints called (parallel after action load):**

- `GET /agentmemory/actions` — resolve action record and `sourceMemoryIds`
- `POST /agentmemory/smart-search` with `expandIds` — if `sourceMemoryIds` present (direct KV fetch, no vector overhead)
- `POST /agentmemory/smart-search` with `query` — fallback when no `sourceMemoryIds`
- `POST /agentmemory/enrich` — parallel, only when `files=` provided
- `GET /agentmemory/lessons` — always parallel
- `GET /agentmemory/memories` — parallel, for link-following

**The `expandIds` path is the richest:** direct KV resolution, full `CompressedObservation` with `facts[]`, `narrative`, `concepts[]`, `files[]`, no scoring round-trip. This is why `memory_update_task(operation="create")` auto-links memories — it populates `sourceMemoryIds` so this path is available when the action is later executed.

**Flow:**

```
action_id provided?
  ├── yes → GET /actions → find by ID → read sourceMemoryIds
  │          ├── sourceMemoryIds present → POST /smart-search expandIds  ──┐
  │          └── absent               → POST /smart-search query         ──┤
  └── no (task= text) ──────────────────────────────────────────────────────┤
                                                                            │
files= provided? → POST /enrich (parallel)  ──────────────────────────────┐│
GET /lessons (always parallel)  ──────────────────────────────────────────┘│
GET /memories (always parallel, for link-following)  ──────────────────────┘
  │
  └── fuse into: {objective, action, relevant_observations, memories,
                  lessons, enriched_file_context?, bug_candidates?}
```

**To improve:**

- `GET /agentmemory/actions` returns all actions, filtered client-side by ID. If agentmemory adds `GET /actions/:id`, replace the list+filter with a direct fetch.
- When `sourceMemoryIds` is empty but the action has `concepts[]` or `files[]` on its record, those could be passed as filters to `/enrich` or as seed for a smarter query. Currently falls back to plain title+description query.
- Lessons are fetched unconditionally. When agentmemory adds lesson filtering by concept or file, narrow the fetch to concepts from the action's observations.

---

### 3. `memory_save`

**Agent intent:** "Record this non-obvious decision permanently."

**Engine endpoint:** `POST /agentmemory/remember`

**Flow:** validate → normalize type → POST /remember → return saved memory metadata

**Notes:**

- The `title` param is prepended to `content` before saving because the `/remember` endpoint field mapping varies by server version. When agentmemory stabilizes a top-level `title` field, split them back.
- `agent_id` maps to `agentId` — used for role traceability in future filtered searches.

**To improve:**

- No deduplication: saving the same decision twice creates two memory records. Future: run `memory_find(query=title)` before saving and surface an "already exists" warning if a highly-scored memory with the same title exists.
- No `evolve` / `supersedes` path exposed. When a worker updates an existing architectural decision, it should call `POST /agentmemory/evolve` or `POST /agentmemory/relations` with `type=supersedes` to maintain the memory version chain. Currently not wired — just creates a new record.
- No `ttlDays` param. Useful for ephemeral constraints (e.g. "freeze merges until Thursday") that shouldn't persist forever. Could be added as an optional param mapped to the `/remember` body.

---

### 4. `memory_next`

**Agent intent:** "What should I work on next?"

**Engine endpoints called:**

- `GET /agentmemory/frontier` — unblocked actions sorted by priority × recency
- Per action (parallel via `asyncio.gather`): `POST /smart-search` (expandIds or query) + `GET /memories`

**Key design:** enrichment is engine-native. The old `memory_frontier` used `GET /memories` + client-side stopword scoring. `memory_next` uses `expandIds` when `sourceMemoryIds` exist (direct KV, no scoring overhead), or falls back to a query-based `smart-search` on the action title. Either way, relevance comes from the engine.

**Replaces:** `memory_frontier`

**Flow:**

```
GET /frontier → actions[]
  │
  └── for each action (parallel asyncio.gather):
        sourceMemoryIds present?
          ├── yes → POST /smart-search expandIds  ──┐
          └── no  → POST /smart-search query       ──┤
                                                     │
        GET /memories (parallel)  ──────────────────┘
          │
          └── _follow_memories (set-intersection)
                │
                └── action["context"] = {observations, memories}

return enriched actions[] — each has context field ready for workers
```

**To improve:**

- Enrichment makes 2 parallel HTTP calls per frontier action (smart-search + memories). If frontier returns 5 actions, that's 10 parallel calls. Acceptable now; if the engine adds a batch expand endpoint, consolidate.
- No `GET /agentmemory/next` call (the "recommendation heuristic" endpoint). If agentmemory evolves `/next` to return a smarter priority order beyond raw priority×recency, wire it as a pre-filter before returning the frontier.
- Enrichment failure is silently swallowed (`except: pass`) — the action is still returned without context. Consider adding an `enrichment_failed: true` flag so orchestrators can decide whether to retry.

---

### 5. `memory_update_task`

**Agent intent:** "Create / advance / close this action."

**Engine endpoints called:**

- `create` → `POST /agentmemory/smart-search` (auto-link) + `GET /memories` + `POST /agentmemory/actions`
- `update/complete/block/cancel` → `POST /agentmemory/actions/update`

**Auto-link on create (engine-native):** runs `POST /smart-search` on the action title+description, takes the top-4 observations, follows their `sourceObservationIds` to memories via set-intersection, attaches top-3 memory IDs as `sourceMemoryIds`. This populates the link chain that `memory_next` and `memory_task_context` rely on for `expandIds` expansion later.

**Replaces:** `memory_action_create` + `memory_action_update` (two tools → one `operation=` dispatch)

**Why `POST /actions/update` not `PATCH /actions/:id`:** the server does not implement `PATCH` on the individual action endpoint — it returns 404. The correct mutation path is `POST /actions/update` with `actionId` in the body.

**To improve:**

- Auto-link currently attaches memories by set-intersection, same limitation as `_follow_memories`. When agentmemory adds `POST /memories/search`, replace with a proper vector search on the action title.
- `block` operation sets `status=blocked`. The engine treats `blocked` as "waiting on DAG dependencies" and manages it automatically. Manually setting `blocked` may interfere with the dependency resolver — currently undocumented behavior. Investigate whether agent-set `blocked` conflicts with system-managed `blocked`.
- No `POST /agentmemory/relations` or `POST /agentmemory/evolve` call on create. When an action explicitly supersedes or depends on a memory, that relation should be recorded. Currently the proxy has no way to express this.

---

### 6. `memory_sessions_find`

**Agent intent:** "Find prior sessions where we worked on X — not by UUID."

**Engine endpoints called (parallel):**

- `POST /agentmemory/search` — BM25+vector on observations, returns `sessionId` anchors
- `GET /agentmemory/sessions` — full session list with metadata
- `GET /agentmemory/crystals` — session narrative summaries (matched post-join)
- `POST /agentmemory/timeline` — only when `include_timeline=True`

**Approach:** semantic session search doesn't exist natively in agentmemory. The proxy uses observation search as a proxy — observations are indexed (BM25+vector), so a query returns the observations most semantically similar to the query, each carrying a `sessionId`. Grouping by `sessionId` and joining metadata reconstructs "sessions about X" from the bottom up.

**Flow:**

```
POST /search (query) ──────────────────────────┐
GET /sessions        ──────────────────────────┤  (parallel)
                                               │
extract unique sessionIds from search results  │
join session records by ID  ←──────────────────┘
  │
  └── match crystals by sessionId (post-join)
      optionally: POST /timeline on earliest anchor

return: [{sessionId, summary, firstPrompt, startedAt, matched_observation, crystals?}]
```

**To improve:**

- Observation search is the wrong granularity for session search. An observation about "auth token" could belong to a session where auth was a side effect, not the focus. Crystal narratives would be a far better index because they capture the session's overall purpose. When agentmemory adds crystal narrative search (`POST /crystals/search`), replace step 1 with that — far more accurate session recovery.
- `GET /sessions?limit=100` fetches all sessions for the join. On long-running projects this could be 500+ sessions. Pagination not yet implemented — add a `since=` anchor if the session list grows large.
- The timeline call anchors on the earliest matched session's `startedAt`. If sessions span months, the timeline window may be too narrow. Expose `include_timeline` as a separate time-window param or let the caller pass an explicit anchor.

---

### 7. `memory_profile`

**Agent intent:** "Give me a stable project snapshot before I start or plan."

**Engine endpoints called (parallel):**

- `GET /agentmemory/profile` — top concepts, top files, recent activity, session count
- `GET /agentmemory/lessons` — distilled patterns (if `include_lessons=True`)
- `GET /agentmemory/insights` — highest-level synthesis (if `include_insights=True`)
- `GET /agentmemory/frontier` — active task pressure (if `include_frontier=True`)

All calls are parallel. This is the intended session-start tool — call it once at the top of a new conversation to orient before planning.

**To improve:**

- `GET /profile` is recency-sorted, not query-sorted. If the project has many concepts and files, the profile may surface recently-touched areas instead of architecturally important ones. When agentmemory adds importance weighting to the profile endpoint, it will improve automatically — no proxy change needed.
- Lessons and insights are fetched unconditionally with `minConfidence=0.1`. Very early in a project's life (< 5 sessions), these will always be empty and add latency for nothing. Could gate on `sessionCount > 5` from the profile response before fetching them.
- No `GET /agentmemory/slots` call. Slots are pinned key-value facts (user preferences, project conventions) that agentmemory may add as a persistent injection mechanism. When stable, add `GET /slots` to the profile response as a `pinned_rules` section.

---

### 8. `memory_graph_query` (advanced)

**Not in the standard agent flow.** Use only when you need explicit structural causality chains that `memory_find` and `memory_task_context` don't surface — e.g. "which errors have been caused by changes to SAMSegmentor across all sessions."

**Engine endpoint:** `POST /agentmemory/graph/query`

**Returns:** neighborhood or path query over the knowledge graph — nodes and edges with `weight`.

**To improve:**

- The graph is cold until `GRAPH_EXTRACTION_ENABLED=true` and at least one consolidation pass has run. Cold graph returns empty `nodes[]` with no error. Should return `{"warning": "graph is cold — run consolidation first"}` instead of empty results that look like "no connections found."
- Response is capped at 20 nodes / 30 edges to avoid context flooding. This cap is arbitrary. A future improvement: add a `summary_only` mode that returns node type counts and top edge weights without full payloads.

---

## agentmemory API Endpoints Used — Reference Table

| MCP tool               | agentmemory endpoint          | Method | Role                                          |
| ---------------------- | ----------------------------- | ------ | --------------------------------------------- |
| `memory_find`          | `/agentmemory/smart-search`   | POST   | Primary scored recall (BM25+vector+graph)     |
| `memory_find`          | `/agentmemory/enrich`         | POST   | File-scoped cross-session context             |
| `memory_find`          | `/agentmemory/memories`       | GET    | Link-following into unindexed memory store    |
| `memory_find`          | `/agentmemory/insights`       | GET    | High-level synthesis (depth=deep only)        |
| `memory_find`          | `/agentmemory/crystals`       | GET    | Session narratives (depth=deep only)          |
| `memory_task_context`  | `/agentmemory/actions`        | GET    | Resolve action record + sourceMemoryIds       |
| `memory_task_context`  | `/agentmemory/smart-search`   | POST   | expandIds or query                            |
| `memory_task_context`  | `/agentmemory/enrich`         | POST   | File context (when files= provided)           |
| `memory_task_context`  | `/agentmemory/lessons`        | GET    | Known pitfalls                                |
| `memory_task_context`  | `/agentmemory/memories`       | GET    | Link-following                                |
| `memory_save`          | `/agentmemory/remember`       | POST   | Write curated memory                          |
| `memory_next`          | `/agentmemory/frontier`       | GET    | Unblocked ready actions                       |
| `memory_next`          | `/agentmemory/smart-search`   | POST   | Per-action enrichment (expandIds or query)    |
| `memory_next`          | `/agentmemory/memories`       | GET    | Per-action link-following                     |
| `memory_update_task`   | `/agentmemory/smart-search`   | POST   | Auto-link on create                           |
| `memory_update_task`   | `/agentmemory/memories`       | GET    | Auto-link source resolution                   |
| `memory_update_task`   | `/agentmemory/actions`        | POST   | Create action                                 |
| `memory_update_task`   | `/agentmemory/actions/update` | POST   | Mutate action (not PATCH — server 404s)       |
| `memory_sessions_find` | `/agentmemory/search`         | POST   | Observation BM25+vector for sessionId anchors |
| `memory_sessions_find` | `/agentmemory/sessions`       | GET    | Session metadata join                         |
| `memory_sessions_find` | `/agentmemory/crystals`       | GET    | Session narratives                            |
| `memory_sessions_find` | `/agentmemory/timeline`       | POST   | Temporal reconstruction (optional)            |
| `memory_profile`       | `/agentmemory/profile`        | GET    | Top concepts, files, activity                 |
| `memory_profile`       | `/agentmemory/lessons`        | GET    | Distilled patterns                            |
| `memory_profile`       | `/agentmemory/insights`       | GET    | Synthesized knowledge                         |
| `memory_profile`       | `/agentmemory/frontier`       | GET    | Active task pressure                          |
| `memory_graph_query`   | `/agentmemory/graph/query`    | POST   | Structural graph traversal                    |

**Not used (internal agentmemory endpoints):**

| Endpoint                               | Why not exposed                                                             |
| -------------------------------------- | --------------------------------------------------------------------------- |
| `POST /agentmemory/session/start`      | Handled by `agentmemory_session_start.py` hook — not an MCP tool            |
| `GET /agentmemory/export`              | Admin/debug — not part of agent workflow                                    |
| `POST /agentmemory/evolve`             | Not yet wired — needed for memory versioning (see `memory_save` to-improve) |
| `POST /agentmemory/relations`          | Not yet wired — needed for explicit memory relationships                    |
| `GET /agentmemory/semantic`            | Reachable via `_follow_memories`; no separate tool needed yet               |
| `GET /agentmemory/procedural`          | Same — no separate tool needed yet                                          |
| `GET /agentmemory/observations`        | Raw session data; use `smart-search` expandIds instead                      |
| `POST /agentmemory/timeline`           | Exposed internally via `memory_sessions_find(include_timeline=True)`        |
| `POST /agentmemory/actions/{id}/lease` | Relevant only if multiple concurrent orchestrators run                      |

---

## Removed Client-Side Logic

The following was deleted and must not return:

```python
# DELETED — do not re-add
_STOPWORDS = frozenset("a an and are as at be by ...".split())
_MIN_MEMORY_SCORE = 0.1
def _score_memory(m: dict, query: str) -> float: ...
def _score_and_filter_memories(memories, query, limit) -> list: ...
```

These existed because `GET /memories` ignores the `q` param. The replacement is `_follow_memories` (set-intersection via `sourceObservationIds` / concepts / files). It is intentionally less sophisticated — transparency and maintainability over false precision.

---

## Session Start Hook

`mcp/agentmemory_session_start.py` is a separate file and is **not part of the MCP proxy**. It is a Claude Code `SessionStart` hook that:

1. Reads the `SessionStart` stdin payload (cwd, sessionId)
2. Normalises the project path via `AGENTMEMORY_PROJECT_CANONICAL` (devcontainer path mapping)
3. POSTs to `POST /agentmemory/session/start` to register the session
4. Prints any returned `context` string to stdout — this becomes the `<agentmemory-context>` block injected at the top of each session

It is registered in `~/.claude/settings.json` and runs before any agent sees the conversation. Keep it separate from the proxy — it is a hook, not a tool.

---

## Watching for agentmemory Engine Improvements

The proxy is designed to become simpler as the engine matures. Watch these upstream changes:

| Engine gap today               | What to watch for                           | Impact on proxy                                                                                      |
| ------------------------------ | ------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `GET /memories` ignores `q`    | `POST /memories/search` with vector index   | Replace `_follow_memories` entirely; `memory_find` becomes 2 parallel calls instead of 3             |
| Lesson scoring is term overlap | Lesson vector index added                   | Automatic improvement — no proxy change                                                              |
| No crystal narrative search    | `POST /crystals/search` endpoint            | `memory_sessions_find` step 1 switches from observation search to crystal search — far more accurate |
| No single-record action fetch  | `GET /actions/:id` endpoint                 | `memory_task_context` action load becomes one direct call                                            |
| No importance-weighted profile | Profile endpoint gains importance scoring   | `memory_profile` stops surfacing only recently-touched areas                                         |
| Graph cold with no signal      | Cold-detection in graph API response        | `memory_graph_query` can emit a clear warning instead of silent empty results                        |
| No memory dedup                | `PUT /memories/:id` or deduplicate-on-write | `memory_save` can prevent duplicate records natively                                                 |
