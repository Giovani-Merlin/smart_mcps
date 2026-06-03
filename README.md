# AgentMemory MCP Proxy

A lean FastMCP proxy over the agentmemory REST API, exposing 11 tools optimised for
the orchestrator → explorer → worker multi-agent pipeline.

**Design principle:** the MCP is a router and response shaper, not a search engine.
All relevance ranking is delegated to the agentmemory engine (BM25+vector via
`/smart-search`, file-graph via `/enrich`). The proxy's only client-side logic is
`_follow_memories` — a simple set-intersection that links scored observations to their
unindexed curated memories.

---

## Two Memory Stores

| Store                | Tool                 | Endpoint         | Semantics                                                                                                                                    |
| -------------------- | -------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **Curated memories** | `memory_save`        | `POST /remember` | Immutable after save. No native query index — GET `/memories?q=` is ignored server-side. Reached by link-following from scored observations. |
| **Lessons**          | `memory_lesson_save` | `POST /lessons`  | Confidence-scored (0–1). Auto-strengthen when the same insight is re-saved. Dedicated recall endpoint.                                       |

Always choose the right store:

- `memory_save`: one-off facts, decisions, constraints found during a task.
- `memory_lesson_save`: patterns that repeat, gotchas that recur, wisdom that accumulates.

---

## Tool Reference

| Tool                     | When to use                                                               | Underlying endpoint                                                      |
| ------------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `memory_find`            | General semantic recall — default retrieval tool                          | `POST /smart-search` + `GET /memories` + `POST /enrich`                  |
| `memory_task_context`    | Full context packet before executing one action                           | `GET /actions` + `POST /smart-search` + `GET /memories` + `GET /lessons` |
| `memory_save`            | Save curated memory (one-off, immutable)                                  | `POST /remember`                                                         |
| `memory_lesson_save`     | Save confidence-scored lesson (accumulates strength)                      | `POST /lessons`                                                          |
| `memory_next`            | Enriched frontier for orchestrators — each action pre-loaded with context | `GET /frontier` (nested envelope)                                        |
| `memory_update_task`     | Create / update / complete / block / cancel actions                       | `POST /actions` or `POST /actions/update`                                |
| `memory_sessions_find`   | Recover prior sessions by topic (not UUID)                                | `POST /smart-search` + `GET /sessions`                                   |
| `memory_profile`         | Project snapshot at session start or before planning                      | `GET /profile` + `GET /lessons` + `GET /insights` + `GET /frontier`      |
| `memory_session_context` | Get full `<agentmemory-context>` XML block on demand                      | `POST /session/start`                                                    |
| `memory_crystallize`     | Compress completed action chains into crystal digest via LLM              | `POST /mcp/call` (`mem::crystallize`)                                    |
| `memory_graph_query`     | Structural causality/dependency traversal (**not in standard flow**)      | `POST /graph/query`                                                      |

---

## Agent Flow

```
Session start
  └─ memory_profile(project=...)          # snapshot: concepts, files, lessons, frontier

Orchestrator
  └─ memory_next(project=...)             # enriched frontier — each action has context field
  └─ memory_update_task(operation="create", ...)  # add new actions
  └─ memory_update_task(operation="complete", ...)

Explorer (before coding)
  └─ memory_find(query=..., files=...)    # observations + linked memories + lessons
  └─ memory_task_context(action_id=...)   # full packet if action_id known

Worker (after implementing)
  └─ memory_save(...)                     # one-off decisions, constraints found
  └─ memory_lesson_save(...)              # recurring patterns, gotchas

End of sprint
  └─ memory_crystallize(action_ids=...)   # compress completed chain into crystal
```

### When `memory_next` beats `memory_task_context`

If the action came from `memory_next`, the `context` field is **already populated** —
pass it directly to workers without calling `memory_task_context`. Only call
`memory_task_context` when you have an `action_id` but no pre-loaded context.

---

## Known API Quirks

| Quirk                                                                                 | Impact                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /memories?q=` is ignored server-side                                             | Use `POST /smart-search` for text search; `GET /memories` is for link-following only                                                                                                                                                                                                      |
| `GET /frontier` returns `{frontier: [{action: {...}, score, leased, blockers}], ...}` | Entries are **nested** — `entry["action"]` not `entry`. The proxy extracts and flattens via `_fmt_frontier_entry`.                                                                                                                                                                        |
| `POST /agentmemory/crystallize` returns 404                                           | Crystallize has no REST endpoint; routes through `POST /mcp/call {name: "memory_crystallize"}`                                                                                                                                                                                            |
| `POST /smart-search` bundles `lessons: []` in response                                | Field is always present but may be empty. `memory_find` reads `obs_resp.get("lessons", [])` for this.                                                                                                                                                                                     |
| Observation `facts[]` may be empty                                                    | Many observations were tombstoned by the summarisation pipeline. `_is_useful_observation` keeps semantic types (decision, subagent, other, architecture, bug, pattern, code, error) even when `facts` is empty, and drops pure action records (`command_run`, `file_read`, `file_write`). |

---

## Debug Scenario Scripts

`mcp/agentmemory/` holds standalone, synchronous scripts that exercise the raw
agentmemory REST + MCP surface against the live service (no async, so each step
is breakpoint-debuggable). Each prints a narrated, step-by-step walkthrough of a
real agent memory pattern. Run any one directly:

```bash
python mcp/agentmemory/scenario_<name>.py
```

They share `_client.py` (HTTP helper + endpoint quick-reference + output
formatters). Scripts that create live data clean up after themselves
(governance-delete, facet-untag, sketch-discard, action-cancel, sentinel-cancel);
destructive governance ops are always `dryRun`. Lessons have **no delete
endpoint**, so scenarios that save a lesson use _stable_ content — the backend
auto-strengthens one lesson on re-save instead of leaving a new orphan per run.

**Recall / read patterns**

| Script                        | Pattern                      | Endpoints exercised                                           |
| ----------------------------- | ---------------------------- | ------------------------------------------------------------- |
| `scenario_context_recovery`   | Progressive disclosure       | `smart-search` + `expandIds`                                  |
| `scenario_queries`            | Targeted topic recall        | `smart-search` (LTX / ComfyUI queries)                        |
| `scenario_arch_discovery`     | Architectural map            | `graph/query` + `memory_relations`                            |
| `scenario_bug_recovery`       | Error-recovery loop          | `smart-search`/expand + `lesson-recall` + `enrich` + memories |
| `scenario_temporal_forensics` | "Why is the code like this?" | `timeline` + `sessions` join + `audit` + `commits`            |

**Task / coordination patterns**

| Script                     | Pattern                 | Endpoints exercised                                       |
| -------------------------- | ----------------------- | --------------------------------------------------------- |
| `scenario_task_init`       | Pick up next work       | `frontier` + `frontier/lease` + `actions/update` + recall |
| `scenario_task_complete`   | Finish & distill        | `actions/update` + `crystallize` + `lessons` + `remember` |
| `scenario_blocked_handoff` | Gate on external event  | `sentinels` + `signals` + block/`frontier`                |
| `scenario_design_sketch`   | Speculative exploration | `sketches` create/add/list/`promote`/`discard`/`gc`       |

**Write / lifecycle / governance patterns**

| Script                       | Pattern                   | Endpoints exercised                                                                    |
| ---------------------------- | ------------------------- | -------------------------------------------------------------------------------------- |
| `scenario_mock_roundtrip`    | Create→retrieve all types | `remember`/`lessons`/`actions`/`crystallize` round-trips                               |
| `scenario_memory_evolution`  | Knowledge goes stale      | `remember`→`evolve`→`verify`→`cascade-update`→`relations`→`governance/memories`        |
| `scenario_insight_synthesis` | Meta-cognitive upkeep     | `diagnostics`(+`heal` dry-run) + `reflect` + `insights`(+search)                       |
| `scenario_facet_governance`  | Dimensional hygiene       | `facet-tag`/`facets`/`facets/stats`/`facets/query` + `governance/bulk-delete` (dryRun) |

> `scenario_insight_synthesis` runs `reflect`, a real LLM synthesis pass (~40s).
> Set `RUN_REFLECT = False` at the bottom to skip it. Memory **slots** are not
> covered — that subsystem is gated behind `AGENTMEMORY_SLOTS=true` and returns
> 503 in this build.

---

## Running

```bash
# Inspect all tools in the MCP inspector
fastmcp dev mcp/agentmemory_mcp_proxy.py

# Or via npx inspector
npx @modelcontextprotocol/inspector .venv/bin/python mcp/agentmemory_mcp_proxy.py
```

Environment variable:

```bash
AGENTMEMORY_URL=http://localhost:3111  # default
```

---

## Tests

Two test files, both require `systemctl --user start agentmemory`:

```bash
python -m pytest tests/mcp/ -v
```

| File                            | What it tests                                                                                                                                                                          |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/mcp/test_agentmemory.py` | Raw REST API endpoints. Every endpoint the proxy calls has a test here. Snapshot fixtures in `tests/mcp/fixtures/` document expected response shapes.                                  |
| `tests/mcp/test_mcp_proxy.py`   | Proxy tool functions directly (imported via importlib, fastmcp stubbed with identity decorators). Pure logic helpers tested without HTTP; live integration tests use the real service. |

### Snapshot fixture system

On first run, live API responses are saved to `tests/mcp/fixtures/<name>.json`. Subsequent
runs load the saved fixture and assert structural parity (same top-level keys). This
documents the "known good" API shape at test-write time without requiring constant API access.

To regenerate a fixture: delete the corresponding `.json` file and re-run the tests.

### Proxy loading in tests

`test_mcp_proxy.py` loads the proxy with a stub fastmcp module (`_FakeFastMCP`) that makes
`@mcp.tool()` and `@mcp.prompt()` identity decorators. This avoids the `mcp` namespace
package conflict (the project's `mcp/` directory is on `sys.path` via pytest rootdir and would
shadow the installed `mcp` PyPI package that fastmcp needs).
