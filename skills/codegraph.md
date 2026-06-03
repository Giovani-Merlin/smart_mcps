---
name: codegraph
description: Explore, search, and understand code structure using the codegraph CLI. Use when asked to find symbols, trace call flows, understand architecture, or assess change impact.
argument-hint: "[query or symbol name]"
user-invocable: true
---

Use the `codegraph` CLI via bash to answer code structure questions.

## Primary commands

**Explore an area or concept** (start here for most questions):

```bash
codegraph context "<query or symbol>"
```

Composes search + node details + callers + callees in one call. Best first step.

**Find a specific symbol**:

```bash
codegraph search "<symbol name>"
```

**Trace call flow** — what calls X, what X calls:

```bash
codegraph callers "<symbol>"
codegraph callees "<symbol>"
```

**Assess change impact**:

```bash
codegraph impact "<symbol>"
```

**List indexed files**:

```bash
codegraph files [path]
```

**Check index health**:

```bash
codegraph status
```

## Usage guidelines

- Always start with `codegraph context` — it is the richest single call.
- Follow up with `codegraph callers`/`callees` only when you need to trace a specific path not covered by context.
- Use `codegraph impact` before suggesting changes to a widely-used symbol.
- The index is kept up to date automatically (Setup hook runs `codegraph index` on session start).
