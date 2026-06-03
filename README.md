# smart_mcps

A Claude Code plugin that registers four MCP servers — **AgentMemory**, **NotebookLM**, **Perplexity**, and **Codegraph** — with citation-stripping hooks.

## Installation (Claude Code)

```text
/plugin marketplace add https://github.com/Giovani-Merlin/smart_mcps
/plugin install smart-mcps
```

That's it. No separate package install needed. The plugin uses `uvx` to fetch and run the agentmemory server on demand (same pattern as `npx` for JS MCPs), so everything is self-contained.

### Environment variables

Perplexity requires an API key. The MCP server is spawned by the VS Code extension host, so the key must be in that process's environment — not just a terminal session.

**VS Code extension (recommended setup):**

1. Install the [mkhl.direnv](https://marketplace.visualstudio.com/items?itemName=mkhl.direnv) VS Code extension — it reads your project's `.envrc` on workspace open and injects vars into the extension host, which propagates to all spawned MCP servers.
2. Create a per-project `.envrc`:

   ```bash
   cp .envrc.example .envrc   # fill in your key
   direnv allow
   ```

**devcontainer:** add to `containerEnv` in `devcontainer.json` (reads from host shell, never committed):

```json
"PERPLEXITY_API_KEY": "${localEnv:PERPLEXITY_API_KEY}"
```

**Shell profile fallback** (all projects share the same key):

```bash
export PERPLEXITY_API_KEY="pplx-..."   # in ~/.bashrc or ~/.profile
```

### Codegraph setup (per project)

Codegraph requires an index built from your project's source. Run this once in each project where you use the plugin:

```bash
pip install codegraph   # or: uv tool install codegraph
codegraph init
codegraph index
```

The file watcher keeps the index up to date as you edit. Re-run `codegraph index` after large changes (e.g. switching branches).

### NotebookLM auth (one-time)

```bash
uv tool install notebooklm-mcp-cli   # install the CLI
nlm login                             # browser OAuth → saves to ~/.notebooklm-mcp-cli/
```

Re-run `nlm login` when cookies expire. Auth state is stored in `~/.notebooklm-mcp-cli/` and persists across projects.

---

## What's included

| MCP server    | Launched via                                | What it does                                        |
| ------------- | ------------------------------------------- | --------------------------------------------------- |
| `agentmemory` | `uvx --from git+... smart-mcps-agentmemory` | 11-tool FastMCP proxy over the agentmemory REST API |
| `notebooklm`  | `notebooklm-mcp`                            | 39-tool NotebookLM interface                        |
| `perplexity`  | `npx -y @perplexity-ai/mcp-server`          | Web-grounded search / research                      |
| `codegraph`   | `codegraph serve --mcp`                     | Sub-millisecond code graph queries                  |

Hooks strip citation noise from NotebookLM and Perplexity responses automatically.

---

## AgentMemory MCP Proxy

A lean FastMCP proxy over the agentmemory REST API, exposing 11 tools optimised for
the orchestrator → explorer → worker multi-agent pipeline.

**Design principle:** the MCP is a router and response shaper, not a search engine.
All relevance ranking is delegated to the agentmemory engine (BM25+vector via
`/smart-search`, file-graph via `/enrich`). The proxy's only client-side logic is
`_follow_memories` — a simple set-intersection that links scored observations to their
unindexed curated memories.

### Two Memory Stores

| Store                | Tool                 | Endpoint         | Semantics                                                                                                                                    |
| -------------------- | -------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **Curated memories** | `memory_save`        | `POST /remember` | Immutable after save. No native query index — GET `/memories?q=` is ignored server-side. Reached by link-following from scored observations. |
| **Lessons**          | `memory_lesson_save` | `POST /lessons`  | Confidence-scored (0–1). Auto-strengthen when the same insight is re-saved. Dedicated recall endpoint.                                       |

Always choose the right store:

- `memory_save`: one-off facts, decisions, constraints found during a task.
- `memory_lesson_save`: patterns that repeat, gotchas that recur, wisdom that accumulates.

### Tool Reference

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

### Agent Flow

```text
Session start
  └─ memory_profile(project=...)          # snapshot: concepts, files, lessons, frontier

Orchestrator
  └─ memory_next(project=...)             # enriched frontier — each action has context field
  └─ memory_update_task(operation="create", ...)
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

If the action came from `memory_next`, the `context` field is **already populated** —
pass it directly to workers without calling `memory_task_context`.

### Known API Quirks

| Quirk                                                                                 | Impact                                                                                                                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /memories?q=` is ignored server-side                                             | Use `POST /smart-search` for text search; `GET /memories` is for link-following only                                                                                                                                                                                                      |
| `GET /frontier` returns `{frontier: [{action: {...}, score, leased, blockers}], ...}` | Entries are **nested** — `entry["action"]` not `entry`. The proxy extracts and flattens via `_fmt_frontier_entry`.                                                                                                                                                                        |
| `POST /agentmemory/crystallize` returns 404                                           | Crystallize has no REST endpoint; routes through `POST /mcp/call {name: "memory_crystallize"}`                                                                                                                                                                                            |
| `POST /smart-search` bundles `lessons: []` in response                                | Field is always present but may be empty. `memory_find` reads `obs_resp.get("lessons", [])` for this.                                                                                                                                                                                     |
| Observation `facts[]` may be empty                                                    | Many observations were tombstoned by the summarisation pipeline. `_is_useful_observation` keeps semantic types (decision, subagent, other, architecture, bug, pattern, code, error) even when `facts` is empty, and drops pure action records (`command_run`, `file_read`, `file_write`). |

---

## Debug Scenario Scripts

`scenarios/` holds standalone, synchronous scripts that exercise the raw agentmemory
REST + MCP surface against the live service (no async, so each step is
breakpoint-debuggable). Run any one directly:

```bash
python scenarios/scenario_<name>.py
```

They share `scenarios/_client.py` (HTTP helper + endpoint quick-reference). Scripts that
create live data clean up after themselves. Lessons use stable content — the backend
auto-strengthens one lesson on re-save instead of leaving orphans per run.

---

## Running the proxy directly (dev / inspection)

```bash
# Inspect all tools in the MCP inspector
uvx fastmcp dev agentmemory/proxy.py

# Or via npx inspector
npx @modelcontextprotocol/inspector python3.12 -m agentmemory.proxy

# Environment variable (default shown)
AGENTMEMORY_URL=http://localhost:3111
```

---

## Tests

Requires `systemctl --user start agentmemory` (the agentmemory daemon running locally):

```bash
python -m pytest tests/ -v
```

| File                        | What it tests                                                                                                                                                              |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/test_agentmemory.py` | Raw REST API endpoints. Snapshot fixtures in `tests/fixtures/` document expected response shapes.                                                                          |
| `tests/test_mcp_proxy.py`   | Proxy tool functions directly (imported via importlib, fastmcp stubbed with identity decorators). Pure logic helpers tested without HTTP; live tests use the real service. |

To regenerate a fixture: delete the corresponding `.json` from `tests/fixtures/` and re-run.
