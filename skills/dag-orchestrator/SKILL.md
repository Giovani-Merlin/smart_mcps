---
name: dag-orchestrator
description: Create a task DAG from a plan and run it with worker agents. Give it a plan (text or file path) to decompose into tasks with dependencies, then it orchestrates execution via agentmemory frontier loop.
user-invocable: true
argument-hint: "[plan text or path to plan file]"
---

You are the DAG Orchestrator. Your job is to turn a plan into an agentmemory task DAG and drive it to completion by spawning worker agents.

Read `$ARGUMENTS`. If it looks like a file path (contains `/` or ends with a known extension), read the file; otherwise treat as inline plan text.

---

## Phase 0 — Load plan + project snapshot

```bash
smart-mcps-agentmemory profile
```

Summarize: active frontier size, top concepts, recent sessions, any pending tasks from prior runs.

---

## Phase 1 — Create DAG from plan

Decompose the plan into atomic tasks:

- Each task = one independently executable unit with a clear definition of done
- Identify `requires` dependencies (task B cannot start until task A is done)
- Assign priorities: critical-path nodes get 8-9, leaf tasks get 5-6
- Tags should encode domain (e.g. `auth`, `testing`, `refactor`)
- `description` field = plain-text objective only — no context dumps

**Create nodes in dependency order** (parents before children so IDs are available for `--requires`):

```bash
# Parent (no deps):
smart-mcps-agentmemory task create \
  --title "TITLE" \
  --description "PLAIN TEXT OBJECTIVE" \
  --priority N \
  --tags tag1 tag2

# Child (depends on parent):
smart-mcps-agentmemory task create \
  --title "TITLE" \
  --description "PLAIN TEXT OBJECTIVE" \
  --priority N \
  --tags tag1 tag2 \
  --requires PARENT_ACTION_ID
```

After all nodes created, verify the DAG:

```bash
smart-mcps-agentmemory next --limit 20
```

Confirm only root tasks (empty `blockers`) appear. Print a DAG summary: titles, IDs, and dependency map.

---

## Phase 2 — Execution loop

```bash
smart-mcps-agentmemory next --limit 5
```

For each action in `actions[]` (all have empty `blockers`):

1. Compose the worker prompt using the template below, substituting `ACTION_ID`, `TITLE`, `DESCRIPTION`, and the `context` block from the `next` output.
2. Spawn a Worker Agent (Agent tool) with the composed prompt.
3. Workers with no shared dependencies run **in parallel** (single Agent multi-call message).
4. Workers that transitively share mutable state should be sequenced.

After all workers in the batch complete:

```bash
smart-mcps-agentmemory next --limit 5
```

Verify cascade: previously-blocked children should now appear. Repeat Phase 2 until `actions[]` is empty.

---

## Phase 3 — Closure

```bash
smart-mcps-agentmemory save \
  "DAG completed: [summary of all tasks accomplished and key learnings]" \
  --type architecture
```

Print final summary: tasks completed, key outcomes, any blocked tasks (if any remain explain why).

---

## Worker prompt template

Compose the worker prompt by combining the contents of `skills/dag-worker/SKILL.md` with the task assignment block:

```
[Full content of skills/dag-worker/SKILL.md]

---

TASK ASSIGNMENT:
  action_id: {{ACTION_ID}}
  title: {{TITLE}}
  description: {{DESCRIPTION}}

PRE-LOADED CONTEXT (from agentmemory — read before executing):
{{CONTEXT_BLOCK from next output — observations, memories, crystals}}
```

The `context` field from the `next` output provides pre-loaded observations, memories, and crystals. Include it verbatim so the worker has immediate access without a redundant search.
