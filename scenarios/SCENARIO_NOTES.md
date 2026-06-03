# agentmemory Scenario Notes

**Tested:** 2026-06-03 against agentmemory v0.9.26  
**Project identifier in use:** `smart_mcps`  
**NotebookLM thread:** `c403c279-cf28-4cb3-87c0-77544b251939`

All 4 scenarios run clean. This document is a reference for the next session.

---

## Scenarios

| File | What it demonstrates | Status |
|------|----------------------|--------|
| `retrieve_information_basic.py` | 6 query types in workflow order | ✅ runs |
| `retrieve_information_progressive_disclosure.py` | Create → Search → Expand → Timeline → Inverse → Cleanup | ✅ runs |
| `retrieve_related_sessions.py` | Semantic session discovery (not recency) | ✅ runs |
| `retrieve_related_memories.py` | Graph-based memory retrieval + keyword fallback | ✅ runs (graph empty) |

---

## Critical gotchas (things that will trip you up)

### 1. PROJECT must match your session's project identifier

`_client.py` has `PROJECT = "smart_mcps"`.
This must match the `project` field of your active session, or all project-scoped
queries (profile, lessons, insights, memories) return 0 results.

Check your active project: `GET /agentmemory/sessions` → look at `session.project`.

### 2. /observe response key is `observationId`

```python
resp = call("POST", "/agentmemory/observe", body={...})
obs_id = resp.get("observationId")   # CORRECT
# NOT: resp.get("obsId") / resp.get("id") / resp.get("observation").get("id")
```

### 3. expandIds requires objects, not strings

```python
# CORRECT
{"expandIds": [{"obsId": "obs_XXX", "sessionId": "sess-YYY"}], "limit": 20}

# WRONG — returns 0 results
{"expandIds": ["obs_XXX"], "limit": 20}
```

### 4. expandIds only works on compressed obs (async after /observe)

`POST /observe` stores a RAW observation. Compression runs asynchronously.
Until compression completes, the obs ID won't resolve via `expandIds`.
Freshly-created obs are NOT immediately searchable in BM25/vector either.

Use obs IDs from smart-search results (already-compressed obs) for expandIds.

### 5. /observations returns RAW shape (not CompressedObservation)

```
GET /observations?sessionId=...
→ [{hookType, id, raw, sessionId, timestamp, toolInput, toolName, toolOutput}]
```

No `type`, `title`, `narrative`, `facts`. Those exist only in compressed obs.

### 6. Profile response is nested

```python
resp = call("GET", "/agentmemory/profile", params={"project": PROJECT})
profile = resp.get("profile") or {}     # nested under 'profile' key
reason = resp.get("reason")             # "no_sessions" if project not found
top_concepts = profile.get("topConcepts") or []  # [{concept, frequency}] not strings
top_files = profile.get("topFiles") or []         # [{file, frequency}] not strings
```

### 7. lessons/search params must be numbers

```python
# CORRECT
{"query": "...", "project": "...", "minConfidence": 0.3, "limit": 5}

# WRONG (was using MCP wrapper which required strings)
{"query": "...", "minConfidence": "0.3", "limit": "5"}
```

### 8. graph/query param is maxDepth (not depth)

```python
# CORRECT
{"query": "...", "project": "...", "maxDepth": 2}

# WRONG
{"query": "...", "project": "...", "depth": 2}
```

---

## Verified response shapes

### Compact search results (both /search and /smart-search)
```json
{"obsId": "obs_XXX", "score": 0.016, "sessionId": "...", "timestamp": "...", "title": "...", "type": "..."}
```
- Field is `obsId` (not `id`)
- `score` (not `combinedScore`)
- `/search` score is BM25 raw (~19.65), `/smart-search` is RRF-normalized (~0.016)

### expandIds results
```json
{"obsId": "obs_XXX", "observation": {"id": "...", "narrative": "...", "facts": [...], "concepts": [...], "files": [...], "type": "...", "title": "..."}, "sessionId": "..."}
```
Content nested under `observation` key.

### /search full format results
```json
{"observation": {"id": "...", "narrative": "...", "facts": [...], ...}, "score": 19.65, "sessionId": "..."}
```
Same nested structure.

### Profile response
```json
{"profile": {"topConcepts": [{"concept": "...", "frequency": N}], "topFiles": [{"file": "...", "frequency": N}], "conventions": []}, "reason": null}
```

### Session object
```json
{"id": "...", "project": "smart_mcps", "cwd": "...", "startedAt": "...", "status": "active", "observationCount": 22, "summary": "keep on", "firstPrompt": "keep on", "tags": []}
```
`summary` = LLM-generated title (only set if CONSOLIDATION_ENABLED + LLM configured).
Use `firstPrompt` as fallback.

### POST /forget response
Returns integer count of removed records: `{"removed": 5}` or similar.

### DELETE /governance/memories response
Returns count: `{"deleted": 1}` or similar. DELETE with JSON body works in httpx.

### POST /lessons response
```json
{"lesson": {"id": "lsn_XXX", "content": "...", "confidence": 0.6, ...}}
```

### POST /observe response
```json
{"observationId": "obs_XXX"}
```

### GET /crystals response
```json
{"crystals": [], "success": true}
```
Shape when populated: `{id, narrative, keyOutcomes: [], filesAffected: [], lessons: [], sourceActionIds: [], sessionId?, project?, createdAt}`.

---

## What's disabled/empty on this instance

| Feature | Status | How to enable |
|---------|--------|---------------|
| Graph extraction | ❌ empty | `GRAPH_EXTRACTION_ENABLED=true` + restart + POST /consolidate |
| Crystals | ❌ empty | POST /agentmemory/crystals/auto |
| Profile topConcepts/topFiles | ✅ working | Was broken because PROJECT was wrong |
| Insights | ❌ empty | POST /agentmemory/insights/search returns 0 — no insights generated yet |
| Lessons | Only test lessons | Run scenarios to accumulate real lessons |

---

## Still unknown / needs live testing

### 1. GraphNode IDs vs Memory IDs

The `get_related_memories` pattern in `retrieve_related_memories.py` assumes
that node IDs returned by `graph/query` match the `id` field in `GET /memories`.
This hasn't been verified because the graph is cold. Test after enabling graph extraction.

### 2. Crystal sessionId filter

`GET /crystals` was tested — it only accepts `?project=` param.
Filter by `sessionId` must be done client-side. Need to verify whether
`sessionId` is always populated on Crystal objects.

### 3. Consolidation timing

`POST /agentmemory/consolidate` timed out in testing (>30s). It may need to run
as a background job. Check if there's a `dryRun` param or a progress endpoint.

### 4. includeLessons in smart-search

Confirmed the param name is `includeLessons` (camelCase). Bundled lessons count was 0
because there are no project-matching lessons yet. Should work once lessons are seeded.

### 5. Lessons accumulate across test runs

Each PD scenario run creates one lesson that can't be deleted.
After many runs, the lessons store will grow. Run `mem::lesson-decay-sweep`
or wait for automatic decay. There's no cleanup for this in the scenario.

---

## How to pick up

```bash
systemctl --user start agentmemory

# Run all 4 scenarios
python scenarios/retrieve_information_basic.py
python scenarios/retrieve_information_progressive_disclosure.py
python scenarios/retrieve_related_sessions.py
python scenarios/retrieve_related_memories.py

# Enable graph extraction (then restart agentmemory):
# export GRAPH_EXTRACTION_ENABLED=true
# systemctl --user restart agentmemory
# python3 -c "import sys; sys.path.insert(0,'scenarios'); from _client import call; call('POST', '/agentmemory/consolidate', body={'tier':'episodic'})"
# Then re-run retrieve_related_memories.py — nodes should appear
```

## Next tasks

1. Verify `get_related_memories` graph→memory ID join after enabling graph extraction
2. Trigger `POST /crystals/auto` and verify Crystal shape + `retrieve_related_sessions.py` phase 3
3. Build scenario for `POST /agentmemory/context` (recency-based briefer, different from /search pattern)
4. Build scenario for lesson lifecycle: save → strengthen → watch decay
