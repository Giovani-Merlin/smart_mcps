---
name: codegraph
description: Explore, search, and understand code structure using the codegraph CLI. Use when asked to find symbols, trace call flows, understand architecture, or assess change impact.
argument-hint: "[query or symbol name]"
user-invocable: true
---

Use the `codegraph` CLI via bash to answer code structure questions.

## Decision table — pick the right command

| Need                              | Command                                                     |
| --------------------------------- | ----------------------------------------------------------- |
| Map the area / what matters here? | `codegraph context "<query>"` — always first                |
| Where is symbol X defined?        | `codegraph context "<symbol>"` — also handles symbol lookup |
| Don't know exact symbol name?     | `codegraph query "<partial name>"` — fuzzy symbol search    |
| What calls this function?         | `codegraph callers "<symbol>"`                              |
| What does this call?              | `codegraph callees "<symbol>"`                              |
| What breaks if I change this?     | `codegraph impact "<symbol>"`                               |
| What tests cover this file?       | `codegraph affected <file> [<file>...]`                     |
| What files are in a directory?    | `codegraph files --filter <dir>` — not `ls` or `find`       |
| Is the index healthy / empty?     | `codegraph status`                                          |

## Primary command — `context`

```bash
codegraph context "authentication flow"
codegraph context "_call"
codegraph context "session hooks"
```

`context` already composes search + node details + callers + callees in one call. **Do not stack three separate calls when one `context` call covers it.** Only follow up with `callers`/`callees` when context output is truncated or you need to trace a specific edge deeper.

**Empty output**: if `context` returns only the header line and no symbols, the query matched nothing — fall back to `codegraph query` with a shorter or partial name to find the exact symbol first.

Useful options:

- `--max-nodes <n>` — cap how many nodes are returned (default 50)
- `--max-code <n>` — cap how many code blocks are shown (default 10)
- `--no-code` — skip code blocks entirely for a faster overview

## Symbol lookup — `query`

```bash
codegraph query "_call"
codegraph query "memory" --kind function
codegraph query "BASE_URL" --kind constant
```

Use `query` when you don't know the exact symbol name. It does fuzzy matching and shows signatures and locations. Filter by kind: `function`, `class`, `method`, `variable`, `constant`, `import`.

## Trace call edges

```bash
codegraph callers "_call"
codegraph callees "main"
```

Use only as a follow-up to `context` when you need to trace a specific path that was cut off.

## Assess impact before changes

```bash
codegraph impact "_call"
codegraph impact "BASE_URL"
```

Run this before modifying any widely-used function or constant — it shows what would break.

## Find affected tests

```bash
codegraph affected src/proxy.py
git diff --name-only HEAD | xargs codegraph affected
```

Given one or more source files, lists the test files that transitively depend on them. Useful before a focused change to know which tests to run.

## List indexed files

```bash
codegraph files
codegraph files --filter src/
codegraph files --pattern "**/*.test.*"
```

Prefer this over `ls`/`find` when exploring what's in the index. Use `--filter <dir>` to scope to a subdirectory — positional path arguments are not supported and will error.

## Index health

```bash
codegraph status
```

If status shows no index, run `codegraph init && codegraph index` before using any other command. The SessionStart hook runs `codegraph index --force` (detached) automatically on session start, but only if codegraph was installed before the session began.

## Usage rules

- **Start with `context`** for any exploration question — it is the richest single call.
- **Do not use codegraph for files you have already read** — use it for discovery and impact analysis only.
- **Do not loop**: if `context` returns nothing, use `codegraph query` with a shorter symbol name before assuming the symbol is unindexed.
- **Index lag**: changes made during the session are not in the index until the next `codegraph index` run. Read the file directly if you need to see recent edits. Deleted files are pruned at session start by `codegraph index --force`; a plain `codegraph index` is incremental and leaves them in the index.
- **Edit rejected as "not read"**: if an Edit of codegraph-surfaced code is ever rejected because the file was not read, Read only the snippet's line range using `offset`/`limit` (codegraph gives `file:line`) — do not re-read the whole file.
