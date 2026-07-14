---
name: codegraph
description: Explore, search, and understand code structure using the codegraph MCP tools. Use when asked to find symbols, trace call flows, understand architecture, or assess change impact.
argument-hint: "[query or symbol name]"
user-invocable: true
---

The `codegraph` MCP tools are already loaded — they are a tree-sitter knowledge graph of every symbol, edge, and file in the project. This skill covers **when and in what order** to call them; the tool schemas cover what each one takes.

## The workflow

1. **`context` first.** It composes search + symbol details + callers + callees in one call and is usually enough to answer outright.
2. **Then ONE `explore`** for the source of the symbols `context` surfaced. Its output is verbatim line-numbered file source — treat those files as already read.
3. **Read only what's left.** If you still need a file, Read the specific line range codegraph gave you (`file:line`) via `offset`/`limit` — don't re-read the whole file, and never raw-grep for cross-file analysis.

For a **flow** question ("how does X reach Y"), skip step 1 and start with `trace` from→to: one call returns the whole path with each hop's body and bridges dynamic dispatch. Don't rebuild the path from `search` + repeated calls.

## Decision table

| Need                              | Tool                                                   |
| --------------------------------- | ------------------------------------------------------ |
| Map the area / what matters here? | `context` — always first                               |
| Where is symbol X defined?        | `context`, or `search` if you only need the location   |
| Don't know the exact symbol name? | `search` — fuzzy lookup, returns kind + location + sig |
| How does X reach Y?               | `trace` — the whole call path in one call              |
| See several symbols' source       | `explore` — one capped call, not a Read loop           |
| What breaks if I change this?     | `impact` — real call/import edges, not name matches    |
| What files are in a directory?    | `files` — not `ls`, `find`, or Glob                    |

**Empty output**: if `context` returns only a header and no symbols, the query matched nothing — fall back to `search` with a shorter or partial name to find the exact symbol first. Don't loop.

## Staleness

The index is a **session-start snapshot** (the SessionStart hook reindexes; don't re-index by hand). Codegraph is authoritative for any file you haven't touched this session.

Files **edited during this session** are the exception — their structure is not reindexed, so Read those directly instead of trusting the graph. Source printed by `explore`/`trace` is re-read from disk on every call and is never stale; only structure (edges, signatures) lags.

If a response opens with "⚠️ Some files referenced below were edited since the last index sync…", Read exactly the files it lists. That banner is watcher-gated and the backend runs with `--no-watch`, so it rarely fires — rely on the session-edit rule above, not the banner.

## Cold path — the CLI

The MCP surface is trimmed to the 6 highest-value tools. Everything else stays reachable via the `codegraph` CLI (allowlisted, on PATH):

```bash
codegraph callers "<symbol>"          # context already includes these
codegraph callees "<symbol>"
codegraph affected src/proxy.py       # which tests cover this file
git diff --name-only HEAD | xargs codegraph affected
codegraph status                      # index health / pending sync
codegraph query "<partial>" --kind function
```

## Usage rules

- **Start with `context`** for any exploration question — it is the richest single call.
- **Do not use codegraph for files you have already read** — use it for discovery and impact analysis only.
- **Trust the results.** They come from a full AST parse; do not re-verify with grep.
- **Index lag**: edits made during the session aren't indexed until the next `codegraph index` run. Read the file directly for recent edits. Deleted files are pruned at session start by `codegraph index --force`; a plain `codegraph index` is incremental and leaves them in.
- **Edit rejected as "not read"**: Read only the snippet's line range using `offset`/`limit` (codegraph gives `file:line`) — do not re-read the whole file.
