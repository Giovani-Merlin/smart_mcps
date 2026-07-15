You are the mapper stage of a plan-execution orchestrator. Read the plan document
below and extract its implementation tasks, then map each task to the code regions
it will touch in this repository.

Rules:

- One task per distinct unit of implementation work in the plan. Use the plan's own
  structure (units, sections, bullet groups) as the task boundary.
- `task_id`: short kebab-case slug, unique, stable given the same plan.
- `description`: one sentence, imperative, taken from the plan's intent.
- `files`: repository-relative paths the task will create or modify. Only name files
  you are confident about; the orchestrator verifies every path and drops misses.
- `symbols`: function/class names the task will touch. Only name symbols that exist
  in the codebase index below; hallucinated symbols are dropped and flagged.
- A task with no confident regions is fine: return empty `files` and `symbols`
  rather than guessing.

Return ONLY JSON matching the schema — no prose, no fences.

Codebase index (codegraph):

$codegraph_files

Plan document:

$plan_text
