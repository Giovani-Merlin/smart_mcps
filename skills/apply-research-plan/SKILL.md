---
name: apply-research-plan
description: Executes an approved research_plan.md — grouping its questions across subagents to keep context tight — against external-knowledge sources (perplexity/notebooklm), writes a complete research_answers.md, then a concrete implementation_plan.md ready for /dag-orchestrator. Runs in a clean context, separate from /plan-to-plan, for full focus.
user-invocable: true
argument-hint: "[slug of the approved research plan]"
---

# apply-research-plan

You are in **research execution mode**. The plan was already produced and approved by `/plan-to-plan`. Your job has three parts, in order: (1) get its questions answered against external-knowledge sources via grouped subagents, (2) assemble the complete findings into `research_answers.md`, and (3) translate those findings — together with the plan's objective and resolved context — into a concrete `implementation_plan.md` that `/dag-orchestrator` can pick up next. This phase, and every subagent it spawns, is purely external-knowledge work: do not search this repo's code.

Slug: `$ARGUMENTS`

## Notebook map

| Topic       | Notebook alias or ID |
| ----------- | -------------------- |
| AgentMemory | `agentmemory`        |

Keep this in sync with the notebook map in `skills/plan-to-plan/SKILL.md`.

---

## Step 0 — Start with a clean context

This phase runs best with zero planning-phase noise — `research_plan.md` is the single source of truth, not your memory of how the topic was scoped or explored.

- If your current conversation still holds the planning work (scoping, question decomposition, codebase exploration), tell the user: "For max focus, run `/clear` then re-invoke `/apply-research-plan <SLUG>`." Then stop — do not proceed.
- If you're already in a fresh context, continue to Step 1.

## Step 1 — Read the plan and anchor on its objective

Read `research/<SLUG>/research_plan.md`. If it doesn't exist, tell the user to run `/plan-to-plan` first and stop.

Pull out and hold onto three things — they travel verbatim into every subagent prompt and shape the final implementation plan:

1. **Objective** — what the user wants to accomplish once these questions are answered. Every answer, and the implementation plan at the end, gets held up against this; an accurate-but-irrelevant answer is a wasted query.
2. **What we already know (resolved context)** — treat this as settled. Don't re-derive it, don't contradict it without strong external evidence, and don't burn a query re-confirming it.
3. **The questions** — with their `SRC`/`CAT`/`P`/`BLOCKING`/`DEPENDS_ON` tags, `Question`, `Expected output`, and `Notes` intact.

## Step 2 — Group questions and spawn subagents

You do not run any `perplexity`/`notebooklm` queries directly in this context — running every question's research and synthesis serially here is exactly the kind of context bloat this phase exists to avoid. Instead: group the questions, compose a self-contained prompt per group, and let subagents do the querying. Your job is to group, compose, spawn, and collect — never to query.

**Grouping algorithm — dependency clusters first, then topic/SRC affinity:**

1. Build the dependency graph from each question's `DEPENDS_ON`. A question and everything it (transitively) depends on form one **atomic cluster** — they MUST land in the same group, answered in dependency order, so a later question can build on an earlier answer still live in that subagent's context.
2. Bucket the atomic units (lone questions and dependency clusters alike) into groups small enough to keep each subagent's context tight — typically **2–3 questions per group**, fewer if a single question is expected to need heavy back-and-forth (e.g. several threaded notebooklm follow-ups). Within that constraint, prefer topical/`SRC` affinity: keep the notebooklm design-rationale questions together (they can share one threaded `conversation_id` and cross-reference naturally), keep the perplexity industry-practice questions together.
3. Aim for balanced groups — don't produce a group of 1 and a group of 6 when a 3/3 split is possible, unless a dependency cluster forces the imbalance.

**Spawning:**

- Groups with no dependency relationship between them are independent — spawn them as **parallel** `Agent` calls in a single message (the same pattern `/dag-orchestrator` uses for unblocked workers).
- Cross-group `DEPENDS_ON` should be rare (step 1 above routes same-cluster questions together) but if it happens: run the upstream group to completion first, fold its returned answer text into the downstream group's prompt, then spawn it.
- Subagents only **return text** — they never write files. You assemble and write `research_answers.md` yourself in Step 3, which avoids write races between parallel agents and keeps `plan_id`/ID consistency entirely in your hands.

For each group, compose this prompt — substituting the group's question blocks **verbatim** from `research_plan.md`, plus the plan's `plan_id`, `Objective`, and `What we already know` sections — and spawn with the `Agent` tool, `subagent_type: general-purpose` (it needs `Bash` for the CLI calls below; it must not use codebase-exploration tools):

```
You are answering a batch of questions from an approved external-knowledge research plan
(plan_id: {{PLAN_ID}}). This is purely external-knowledge work — do NOT search, grep, or read
this repo's source code; the objective and resolved context below are your sole grounding in
"what this project already knows."

OBJECTIVE — hold every answer up against this; an accurate-but-irrelevant answer wastes the query:
{{OBJECTIVE}}

WHAT WE ALREADY KNOW (resolved context — settled; don't re-derive or contradict without strong
external evidence; don't burn a query re-confirming it):
{{RESOLVED_CONTEXT}}

YOUR QUESTIONS — answer in this order ({{N}} questions: {{ID_LIST}}). If one DEPENDS_ON
another in this same list, that dependency comes first and its answer is available to you
when you reach the dependent one:

{{Full verbatim Q-ID blocks: SRC/CAT/P/BLOCKING/DEPENDS_ON tags, Question, Expected output, Notes}}

ROUTING:

perplexity:
  smart-mcps-perplexity ask "the question"
  smart-mcps-perplexity reason "the question"   # for complex analysis

notebooklm (match topic to the map below):
  nlm notebook query ALIAS "the question"
  nlm notebook query ALIAS "follow-up" --conversation-id CONV_ID
  # thread follow-ups — if you have 2+ notebooklm questions in this batch, keep them in ONE thread

perplexity+notebooklm: run both, synthesize into one answer

NOTEBOOK MAP:
{{notebook map table}}

For EACH question, return a block in EXACTLY this structure (it gets assembled verbatim into
research_answers.md — match it precisely so nothing needs reformatting):

### Q-XXX [SRC:...] [CAT:...] [PX] [BLOCKING?]

**Original question**

...

**Tools used**

- ...

**Answer**

...

**Implementation implications**

- ...

**Residual risks / follow-ups**

- ...

CRITICAL — be COMPLETE, not summarized. This is the research record that the implementation
plan (and eventually DAG-worker tasks) will build on *without re-querying* — a "yes, X is
true" one-liner is a failed answer even if it's correct. Capture concrete details: names,
numbers, code-shape claims, watch-outs, caveats, sources. Synthesize from raw tool output
(don't dump it raw), but don't compress away the substance that makes an answer actionable.

Never send confidential code, secrets, or proprietary logic to perplexity/notebooklm.
```

## Step 3 — Assemble `research_answers.md`

Collect every subagent's returned Q-ID blocks. In the **original `research_plan.md` question order**, assemble them into:

`research/<SLUG>/research_answers.md`

Use this exact format:

```markdown
---
plan_id: RP-<SLUG>
plan_version: 1
topic: "<original question>"
---

# Research Answers: <Title>

## Status Summary

| ID    | Status   | Confidence | Blocking | Notes |
| ----- | -------- | ---------- | -------- | ----- |
| Q-001 | answered | high       | Yes      | ...   |

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

Build the `Status Summary` table yourself from what each subagent returned — `status` (`answered` / `partial` / `unanswered`), `confidence` (`high`/`medium`/`low`, judged from how directly the sources addressed the question), `Blocking` carried over from the plan. Don't relabel everything `answered`/`high` by default — a partial or low-confidence finding is useful signal for the implementation plan (and a candidate residual risk), not a blemish to paper over.

## Step 4 — Write the implementation plan

This is the artifact `/dag-orchestrator` consumes next — it must stand on its own as a concrete development plan, grounded in the objective, the resolved context, and every answered question, precise enough that someone (or a DAG of worker agents) could build from it without re-deriving any of this research.

Write to: `research/<SLUG>/implementation_plan.md`

Structure it as a markdown plan covering:

- **Objective** — restate the plan's objective (carried over from `research_plan.md`), sharpened with what the research surfaced
- **Grounding** — translate each load-bearing finding into a concrete design/build decision, citing its `Q-ID` inline (e.g. `Q-00X: "..."`) so a reader can trace every decision back to its evidence. Where an answer's framing needs correcting against this codebase's actual reality (a design-intent-vs-actual-behavior mismatch, a renamed field, a missing mechanism) — say so explicitly. A "research says X / the code says Y / so do Z instead" note is *more* trustworthy than silently picking one and hoping
- **Concrete build steps** — phased, with file paths, code snippets, exact commands, what to watch out for, what's still in doubt — written precisely enough that `/dag-orchestrator` can decompose it into an atomic task DAG without you in the loop
- **Open questions / residual risks** — what the research left partial, to resolve empirically during the first implementation pass; carry over the `Residual risks / follow-ups` from `research_answers.md` that still matter

After writing it, tell the user:

> `implementation_plan.md` is ready at `research/<SLUG>/implementation_plan.md` — feed it to `/dag-orchestrator research/<SLUG>/implementation_plan.md` to decompose it into a worker-driven task DAG.

## Non-negotiable rules

- Start clean (Step 0) — if planning context is still loaded, send the user to `/clear` and stop; do not answer questions in a polluted context.
- This phase, and every subagent it spawns, is purely external-knowledge: no `codegraph`/`grep`/`Read` of this repo's source. `research_plan.md`'s `Objective` and `What we already know` sections are the sole grounding in "what this project knows" — treat them as settled, not as something to re-verify.
- Group questions and delegate to subagents (Step 2) — never run more than one group's worth of external queries serially in this context. Dependency clusters stay together; independent groups run in parallel.
- `research_answers.md` must reuse the same `plan_id` and question IDs declared in `research_plan.md`.
- Findings must be **complete**, not summarized to yes/no — substantive enough that neither the implementation plan nor a future DAG-worker task ever needs to re-query to fill a gap.
- `implementation_plan.md` lives in the **same** `research/<SLUG>/` folder as the plan and answers, and shares the `RP-<SLUG>` lineage — it is the natural last artifact in the chain `/plan-to-plan` → `/apply-research-plan` → `/dag-orchestrator`.
- Never send confidential code or secrets to `perplexity`/`notebooklm` — including inside subagent prompts.

## Failure conditions

- Proceeding without a clean context (skipping the Step 0 `/clear` gate)
- Searching the codebase — in this context, or by letting a subagent do it — instead of relying on `research_plan.md`'s resolved context plus external-knowledge sources
- Writing `research_answers.md` without an existing `research_plan.md`
- Running questions serially in this context instead of grouping and delegating to subagents — the exact context-bloat this phase is designed to avoid
- Dumping raw tool output, or compressing findings into yes/no one-liners, instead of synthesizing complete answers
- Skipping `conversation_id` threading in multi-query notebooklm batches
- Writing `implementation_plan.md` to a different folder or slug than `research_plan.md`/`research_answers.md` — breaks the chain `/dag-orchestrator` depends on
- Sending confidential local code to external tools (directly, or via a subagent prompt)
