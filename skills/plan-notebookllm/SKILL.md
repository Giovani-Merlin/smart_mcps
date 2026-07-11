---
name: plan-notebook
description: Notebook-grounded planning — queries a NotebookLM notebook at least 4 times before producing any plan. Enters plan mode automatically. Configure your notebook map in this file.
user-invocable: true
argument-hint: "[topic or task to plan]"
---

# plan-notebook

**Step 0 — enter plan mode before doing anything else.**

Call the `EnterPlanMode` tool now. Do not produce any output until plan mode is active.

______________________________________________________________________

You are in strict **notebook-first planning mode**. No code is written. No files are edited. Only research and planning happen here.

Task: `$ARGUMENTS`

## Process

### Step 1 — discover and identify the notebook

**First, ground the topic in existing code, if relevant.** Prioritize `codegraph context "<topic>"` for exploring code — see the codegraph skill (`skills/codegraph/SKILL.md`). If it surfaces concrete symbols, files, or patterns, carry them into Step 2: ask the notebook about the actual `<symbol>`/`<file>` you found rather than the topic in the abstract — concrete, code-grounded questions get sharper answers than generic ones.

Run:

```bash
nlm notebook list
```

Output is a JSON array: `[{"id": "...", "title": "...", "source_count": N, "updated_at": "..."}]`

- If the command fails → tell the user to run `nlm login` and stop.
- From the list, pick the notebook whose `title` best matches the topic in `$ARGUMENTS`.
- If only one notebook exists, use it.
- If multiple are plausible, list their titles and ask the user to choose before continuing.

State your chosen notebook (title + id) before running any queries. All subsequent queries target that notebook using its `id`.

### Step 2 — interrogate the notebook (minimum 4 queries)

Run the first query:

```bash
nlm notebook query NOTEBOOK_ID "first question"
```

Save the `conversation_id` from the response. Pass it to every follow-up:

```bash
nlm notebook query NOTEBOOK_ID "follow-up question" --conversation-id CONV_ID
```

Your 4 queries must cover:

| Query | Goal                                                                  |
| ----- | --------------------------------------------------------------------- |
| 1     | Scope clarification — what does the notebook say this topic involves? |
| 2     | Relevant facts / source material — key constraints, APIs, or patterns |
| 3     | Implementation constraints — what won't work, what has caveats        |
| 4     | Edge cases / alternatives — what else should be considered            |

After each answer, decide what is still missing and ask a narrower follow-up. If confidence is still low after 4 queries, continue until gaps are closed - you must question it until all of the doubts it can answer are answered. You can send code snippets and specific ideas to notebookllm to confirm beliefs.

### Step 3 — summarize evidence

Before drafting the plan, write a complete **Evidence gathered** section:

- What the notebook confirmed
- What it left ambiguous
- What is still unknown
- Watch outs and clear paths

### Step 4 — produce the plan

Write the plan grounded only in notebook findings. Do not add background knowledge as if it came from the notebook. Explicitly label any remaining unknowns.

## Non-negotiable rules

- Enter plan mode (step 0) before any other action.
- Do not answer from prior knowledge or intuition unless the notebook has been queried and cannot answer.
- Do not produce a plan before completing the notebook interrogation.
- Minimum 4 notebook queries, all using the same `conversation_id` thread.
- If the notebook response is partial or ambiguous, ask a narrower follow-up — do not assume.
- Final plan must include the **Evidence gathered** section.

## Failure conditions

- Producing a plan with fewer than 4 queries
- Skipping `conversation_id` threading (each query treated as isolated)
- Using unsupported background knowledge as notebook evidence
- Writing code or editing files during this skill
