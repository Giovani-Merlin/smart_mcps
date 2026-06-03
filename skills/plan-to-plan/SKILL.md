---
name: plan-to-plan
description: Research orchestration — given a doubt or question, decomposes it into tagged research questions (perplexity/notebooklm/code), produces a research_plan.md for approval, then executes all queries and writes a research_answers.md ready to feed into implementation planning.
user-invocable: true
argument-hint: "[your doubt, question, or topic to research]"
---

# plan-to-plan

**Step 0 — enter plan mode before anything else.**

Call the `EnterPlanMode` tool now. Do not produce any output until plan mode is active.

---

You are in **research decomposition mode**. No code is written. No files are edited during the planning phase. Only exploration and question structuring happen here.

Topic/doubt: `$ARGUMENTS`

## Notebook map

| Topic       | Notebook alias or ID |
| ----------- | -------------------- |
| AgentMemory | `agentmemory`        |

---

## Phase 1 — Understand & scope (do this before generating questions)

Read the doubt in `$ARGUMENTS` carefully. Then:

1. **Explore the codebase** briefly using `find`, `grep`, or `codegraph` to locate relevant files, classes, or entry points related to the topic.
2. **Optionally** run 1–2 quick queries (perplexity or notebooklm) to scope what you don't know yet — use these to inform question generation, not to answer the main question yet.
3. Write a short **Scope block** (3–5 bullets):
   - What is the core question?
   - What is in scope vs out of scope?
   - What sources are available (codebase / internal docs / web)?
   - What is NOT safe to send externally (secrets, PII, proprietary logic)?

## Phase 2 — Decompose into research questions

Generate **atomic research questions** — each answerable by a single tool invocation. Aim for 3–8 questions. Each question gets a stable ID `Q-001`, `Q-002`, …

For each question, assign:

| Field | Values |
|---|---|
| `SRC` | `code` / `perplexity` / `notebooklm` / `perplexity+notebooklm` |
| `CAT` | `design_decision` / `implementation_detail` / `risk` / `requirement` / `open_question` |
| `P` | `P1` (must answer) / `P2` (important) / `P3` (nice to have) |
| `BLOCKING` | add tag if this must be answered before implementation starts |
| `DEPENDS_ON` | Q-IDs this depends on, if any |

**Routing rules:**
- Questions about this codebase → `code`
- Questions about industry practices, libraries, standards, comparisons → `perplexity`
- Questions about internal design docs, ADRs, prior decisions → `notebooklm`
- Questions needing both web + internal context → `perplexity+notebooklm`
- Never route confidential code details to `perplexity`

## Phase 3 — Write the plan file

Determine a short slug from the topic (e.g., `rate-limiting`, `auth-flow`, `cache-strategy`).

Write the plan to: `research/<SLUG>/research_plan.md`

Use this exact format:

```markdown
---
plan_id: RP-<SLUG>
version: 1
topic: "<original question from $ARGUMENTS>"
---

# Research Plan: <Title>

## Scope

- **Goal:** ...
- **In scope:** ...
- **Out of scope:** ...
- **External-safe:** yes/no (and what is NOT safe to send out)

## Question Summary

| ID    | Summary                          | SRC                   | P  | Blocking |
|-------|----------------------------------|-----------------------|----|----------|
| Q-001 | ...                              | perplexity            | P1 | Yes      |

## Questions

### Q-001 [SRC:perplexity] [CAT:design_decision] [P1] [BLOCKING]

**Question**

...

**Expected output**

- ...

**Notes**

...
```

After writing the file, present the question table to the user in your plan output so they can review it before approval.

## Phase 4 — Exit plan mode for approval

Call `ExitPlanMode`. The user will review `research_plan.md`, may edit questions, add/remove/reprioritize, then approve.

---

## Phase 5 — Execute the research plan (runs after plan approval)

Read `research_plan.md`. For each question in order of priority:

### Routing

**`code`** — use `grep`, `find`, `Read`, or `codegraph` to search the codebase. Do NOT call external APIs.

**`perplexity`** — call:
```bash
smart-mcps-perplexity ask "the question" 
# or for complex analysis:
smart-mcps-perplexity reason "the question"
```

**`notebooklm`** — match topic to the notebook map, then:
```bash
nlm notebook query ALIAS "the question"
# save conversation_id and thread follow-ups:
nlm notebook query ALIAS "follow-up" --conversation-id CONV_ID
```

**`perplexity+notebooklm`** — run both, synthesize answers.

### For each question:
1. Run the query/search.
2. Write a concise answer (avoid raw tool output dumps — synthesize).
3. Note the tools used and key sources.
4. Extract **implementation implications** (what this answer means for the implementation plan).
5. Note **residual risks or follow-up questions** if the answer is partial.

## Phase 6 — Write the answers file

Write answers to: `research/<SLUG>/research_answers.md`

Use this exact format:

```markdown
---
plan_id: RP-<SLUG>
plan_version: 1
topic: "<original question>"
---

# Research Answers: <Title>

## Status Summary

| ID    | Status     | Confidence | Blocking | Notes |
|-------|------------|------------|----------|-------|
| Q-001 | answered   | high       | Yes      | ...   |

## Answers

### Q-001 [SRC:perplexity] [CAT:design_decision] [P1] [BLOCKING]

**Original question**

...

**Tools used**

- Perplexity (web search)

**Answer**

...

**Implementation implications**

- ...

**Residual risks / follow-ups**

- ...
```

## Non-negotiable rules

- Enter plan mode (phase 0) before any other action.
- Write `research_plan.md` **before** calling `ExitPlanMode`.
- Do not execute any queries during the planning phase (phases 1–4).
- Do not write code or edit non-research files at any point in this skill.
- Never send confidential code or secrets to `perplexity`.
- The two output files must use the same `plan_id` and question IDs.
- After writing `research_answers.md`, tell the user it is ready to use as input to a new implementation plan.

## Failure conditions

- Running queries during planning (before ExitPlanMode)
- Writing `research_answers.md` without first writing `research_plan.md`
- Sending confidential local code to external tools
- Dumping raw tool output instead of synthesizing answers
- Skipping `conversation_id` threading in multi-query notebooklm sessions
