---
name: codegraph
description: Explore, search, and understand code structure using the codegraph CLI. Use when asked to find symbols, trace call flows, understand architecture, or assess change impact.
argument-hint: "[query or symbol name]"
user-invocable: true
---

Use the `codegraph` CLI via bash to answer code structure questions.

## Decision table — pick the right command

| Need | Command |
| ---- | ------- |
| Map the area / what matters here? | `codegraph context "<query>"` — always first |
| Where is symbol X defined? | `codegraph context "<symbol>"` — also handles symbol lookup |
| What calls this function? | `codegraph callers "<symbol>"` |
| What does this call? | `codegraph callees "<symbol>"` |
| What breaks if I change this? | `codegraph impact "<symbol>"` |
| What files are in a directory? | `codegraph files [path]` — not `ls` or `find` |
| Is the index healthy / empty? | `codegraph status` |

## Primary command — `context`

```bash
codegraph context "authentication flow"
codegraph context "_call"
codegraph context "session hooks"
```

`context` already composes search + node details + callers + callees in one call. **Do not stack three separate calls when one `context` call covers it.** Only follow up with `callers`/`callees` when context output is truncated or you need to trace a specific edge deeper.

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

## List indexed files

```bash
codegraph files
codegraph files agentmemory/
```

Prefer this over `ls`/`find` when exploring what's in the index.

## Index health

```bash
codegraph status
```

If status shows no index, run `codegraph init && codegraph index` before using any other command. The Setup hook runs `codegraph index` automatically on session start, but only if codegraph was installed before the session began.

## Usage rules

- **Start with `context`** for any exploration question — it is the richest single call.
- **Do not use codegraph for files you have already read** — use it for discovery and impact analysis only.
- **Do not loop**: if `context` returns nothing, try `codegraph search` with a shorter symbol name before assuming the symbol is unindexed.
- **Index lag**: changes made during the session are not in the index until the next `codegraph index` run. Read the file directly if you need to see recent edits.
