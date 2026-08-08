---
name: orchestrator-strategy
description: Create or maintain STRATEGY.md - the repo's target problem, approach, who it serves, key metrics, and tracks of work. Use when starting a new initiative, updating direction, or when prompts like "write our strategy", "what are we working on", or "set up the strategy doc" come up. Also relevant when /orchestrator-brainstorm or /orchestrator-plan need upstream grounding and no strategy doc exists yet.
user-invocable: true
argument-hint: "[optional: section to revisit, e.g. 'metrics' or 'approach']"
---

# orchestrator-strategy

`orchestrator-strategy` produces and maintains `STRATEGY.md` — a short, durable
anchor document that captures what this repo (or the thing it ships — a
product, a plugin, a library) is, who it serves, how it succeeds, and where
work is being invested. It lives at the repo root as a canonical, well-known
file, a peer of `README.md` and `CONTEXT.md`. `/orchestrator-brainstorm` and
`/orchestrator-plan` read it as grounding when it exists.

The document is short and structured on purpose. Good answers to a handful of
sharp questions produce a better strategy than any amount of prose. This skill
asks those questions, pushes back on weak answers, and writes the doc. No code
is written, no plan is produced, and **no subagents are spawned — everything
happens inline in this session**.

Focus: `$ARGUMENTS`

Interpret any argument as an optional focus: a section name to revisit
(`metrics`, `approach`, `tracks`) or a scope hint. With no argument, proceed
open-ended and let the file state decide the path.

______________________________________________________________________

## Core Principles

1. **Anchor, not plan.** Strategy is what the thing is and why. Features and
   requirements belong in `/orchestrator-brainstorm`; implementation units
   belong in `/orchestrator-plan`. Do not let either creep into this doc.
2. **Rigor in the questions, not the headings.** The section headers are plain
   English. The interview questions enforce strategy discipline.
3. **Short is a feature.** The template is constrained. Adding sections costs
   more than it looks like. Push back on expansion.
4. **Durable across runs.** This skill is rerunnable. On a second run it
   updates in place, preserves what is working, and only challenges sections
   that look stale or weak.
5. **This repo may be a plugin, not a product.** Some repos that run this
   skill ship as a plugin or library consumed by other repos, not a
   standalone product with end-customers. "Who it's for" and "Marketing"
   are written to fit either case — see `references/interview.md`.

## Phase 0 — Route by file state

Read `STRATEGY.md` at the repo root.

- **File does not exist** → first run. Go to Phase 1.
- **File exists and the argument names a specific section** → targeted
  update. Go to Phase 2.
- **File exists, no argument** → ask which section(s) to revisit, then Phase 2.

Announce the path in one line: "Strategy doc not found — let's write it." or
"Found existing strategy — let's review and update."

## Phase 1 — First-run interview

Read `references/interview.md`. This load is non-optional — the pushback
rules, anti-pattern examples, and quality bar for each section live there.
Improvising from memory produces a passive transcription instead of a
strategy doc.

Run the interview in the section order of the final document:

1. Target problem
2. Our approach
3. Who it's for
4. Key metrics
5. Tracks
6. Milestones (optional)
7. Not working on (optional)
8. Marketing (optional)

For each section, ask the opening question, apply the pushback rules, and
capture the final answer in the user's own language. Do not skip the
pushback step — it is the core of the skill. Two rounds of pushback per
section maximum; capture what the user has given after that and note the
section is worth revisiting on the next run.

**Batch independent questions, explain the stakes.** Group opening questions
(and any pushback follow-ups) that don't depend on each other's answer into a
single `AskUserQuestion` call — up to 4 questions per call, the tool's own
limit. Only serialize when a later question's options genuinely can't be
framed until an earlier one is answered (e.g. "Who it's for" before "Key
metrics" that measure that persona's outcomes). Give each question a short
explanation of why it matters or what it trades off — a bare stem isn't
enough here; this is a grilling session and the extra sentence is worth it.
Prefer free-form responses for the substantive sections (problem, approach,
persona); reserve single-select for routing decisions (which section to
revisit). Each option label must be self-contained.

When all required sections (1–5) are captured, read
`references/strategy-template.md`, fill it in, and present the full draft in
chat before writing. Offer one round of edits. Then write to `STRATEGY.md`.

## Phase 2 — Update run

Read the existing `STRATEGY.md` thoroughly. Summarize current state in 3–5
lines so the user sees what is on file.

If the argument named a specific section, jump to that section in
`references/interview.md`. Preserve all other sections exactly. Apply
pushback as if this were a first run — do not rubber-stamp existing weak
content just because it is already written.

If no specific target, ask the user which section to revisit using
`AskUserQuestion`. Options:

- "Target problem"
- "Our approach"
- "Who it's for"
- "Metrics, tracks, or other"

For each revisited section, re-interview with full pushback, batching
independent follow-ups as in Phase 1. For sections the user confirms are
still accurate, leave them untouched. Update the `last_updated` value in the
YAML frontmatter to today's ISO date.

Write the updated doc back to `STRATEGY.md`.

## Phase 3 — Downstream handoff

After writing, note in one line where the file lives and that
`/orchestrator-brainstorm` and `/orchestrator-plan` will pick it up as
grounding on their next run.

If no downstream skill has run yet on this repo, suggest `/orchestrator-brainstorm`
as a next step.

## What This Skill Does Not Do

- Does not track or reconcile in-flight work. Strategy is the doc; execution
  lives in the requirements docs and plans.
- Does not prioritize the backlog. Prioritization is a separate workflow.
- Does not write requirements or implementation plans — those are
  `/orchestrator-brainstorm` and `/orchestrator-plan`.
- Does not compute metric values. It records which metrics matter and where
  they live, not what they read today.

## Non-negotiable rules

- **Anchor only.** The only file this skill may write is `STRATEGY.md`.
- Batch independent questions per `AskUserQuestion` call (max 4 per call);
  serialize only on genuine dependency. Every question carries a short
  explanation, not a bare stem.
- Explore instead of asking whenever the codebase can answer.
- Section order in `STRATEGY.md` is locked — see `references/strategy-template.md`.
