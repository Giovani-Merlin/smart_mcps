---
name: plan-notebook
description: Notebook-grounded planning — queries a NotebookLM notebook at least 4 times before producing any plan. Enters plan mode automatically. Configure your notebook map in this file.
user-invocable: true
argument-hint: "[topic or task to plan]"
---

<!-- CONFIGURE ME
     This is the locked-alias version of the plan-notebookllm skill.
     It uses a hardcoded notebook map so you don't pay the cost of `nlm notebook list`
     on every planning run.

     To use it:
       1. Run `nlm notebook list` to see your notebooks and their IDs or aliases.
       2. Fill in the table below.
       3. Copy this file over skills/plan-notebookllm/SKILL.md in your project.

     The default skills/plan-notebookllm/SKILL.md uses dynamic discovery (`nlm notebook list`)
     instead — use that if you prefer no configuration overhead.
-->

# plan-notebook

**Step 0 — enter plan mode before doing anything else.**

Call the `EnterPlanMode` tool now. Do not produce any output until plan mode is active.

______________________________________________________________________

You are in strict **notebook-first planning mode**. No code is written. No files are edited. Only research and planning happen here.

Task: `$ARGUMENTS`

## Notebook map

<!-- Add one row per notebook you want accessible. Use the alias (if set) or the UUID. -->

| Topic      | Notebook alias or ID  |
| ---------- | --------------------- |
| Your topic | `your-notebook-alias` |

## Process

### Step 1 — identify the notebook

From `$ARGUMENTS`, determine which notebook row best matches. All subsequent queries target that notebook. State your choice before querying.

### Step 2 — interrogate the notebook (minimum 4 queries)

Run the first query:

```bash
nlm notebook query ALIAS "first question"
```

Save the `conversation_id` from the response. Pass it to every follow-up:

```bash
nlm notebook query ALIAS "follow-up question" --conversation-id CONV_ID
```

Your 4 queries must cover:

| Query | Goal                                                                  |
| ----- | --------------------------------------------------------------------- |
| 1     | Scope clarification — what does the notebook say this topic involves? |
| 2     | Relevant facts / source material — key constraints, APIs, or patterns |
| 3     | Implementation constraints — what won't work, what has caveats        |
| 4     | Edge cases / alternatives — what else should be considered            |

After each answer, decide what is still missing and ask a narrower follow-up. If confidence is still low after 4 queries, continue until gaps are closed.

### Step 3 — summarize evidence

Before drafting the plan, write a short **Evidence gathered** section:

- What the notebook confirmed
- What it left ambiguous
- What is still unknown

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
