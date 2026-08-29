# The orchestrator task map (`orchestrator-task-map v1`)

The machine-readable task map a planning session embeds in its plan document so
the `group` pipeline can skip the mapper LLM entirely. The planning session
already knows what the mapper would have to guess — files (including ones that
don't exist yet), symbols, ordering, and vertical-slice membership — so it
writes them down once, at plan time, and the parser
([`orchestrator/grouping/plan_reader.py`](../orchestrator/grouping/plan_reader.py))
consumes them deterministically.

Writers: the `orchestrator-plan` skill (generates the block 1:1 from the plan's
units). Reader: `parse_task_map()`. Both cite this document; change it only with
both sides in view.

Verification items are not part of the map — when writing them in the plan's
units, follow the behavioural-phrasing guidance in
[`skills/orchestrator-plan/SKILL.md`](../skills/orchestrator-plan/SKILL.md).

## Placement and shape

A fenced YAML block under a `## Task Map` heading, whose **first line is the
version marker comment**:

````markdown
## Task Map

```yaml
# orchestrator-task-map v1
tasks:
  - task_id: u1-scaffold
    description: Create the FastAPI app skeleton and settings module
    slice: null
    files:
      - app/main.py
      - app/settings.py
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u2-users-api
    description: Users CRUD routes on the scaffold
    slice: users
    files:
      - app/routes/users.py
    symbols: []
    depends_on: [u1-scaffold]
    implements: ["/api/users"]
    consumes: []
  - task_id: u3-users-ui
    description: Users admin page calling the users API
    slice: users
    files:
      - web/src/pages/Users.tsx
    symbols: []
    depends_on: [u1-scaffold]
    implements: []
    consumes: ["/api/users"]
```
````

The parser locates the block by the version marker, not the heading — the
heading is a human convention. Exactly one marked block per plan.

## Fields

| Field         | Required | Type                                     | Semantics                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------- | -------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task_id`     | yes      | string                                   | Kebab-case, unique, equal to the plan's unit id (U-ID). Group membership and `groups.json` task lists use it verbatim.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `description` | yes      | string                                   | One sentence; feeds the speccer's group skeletons.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `slice`       | no       | string \| null                           | Vertical-slice label. Must-link is a **hard output invariant** (per CONTEXT.md's Slice entry): a slice lands whole in exactly one group, or grouping fails loudly naming it — never a silent split. Slice-mates are **contracted into one node** before Louvain, and the budget splitter computes its cut candidates between whole slices — never inside one — so the invariant holds through every later stage (split, merge, SCC repair), not only through Louvain. A slice whose own summed work exceeds the budget cap raises `GrouperError`, naming the slice, its members, each member's work, the cap, and the overshoot; `--allow-oversized-slice` (or `[partition] allow_oversized_slice` in `.orchestrator/config.toml`) accepts the overshoot instead, keeping the slice whole as one group with a `flags[]` entry recording it. Shared-infra / cross-cutting tasks carry **no** slice (they are hub material, never forced into a feature slice). |
| `files`       | no       | list of repo-rel paths                   | Files the task will touch. **Prospective files (not existing yet) are allowed** — they are retained, flagged as info, contribute shared-file affinity, appear in `Group.files`, and count in the per-file token allowance (or a `size_hints` price, if given — see below).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `size_hints`  | no       | map of path → `small`\|`medium`\|`large` | Prices a **prospective** file by declared size instead of the flat per-file allowance: `small` 500, `medium` 2,000, `large` 5,000 tokens. `medium` is today's default rate (`per_file_tool_allowance`), so a prospective file left out of `size_hints` is priced exactly as before — unhinted files do not change shape. Every key must name a path already listed in that task's `files`; a path that already exists in the working tree, or a class outside the three, is a hard error (see Validation rules). Existing-file pricing (source bytes ÷ tokens-per-byte) is untouched.                                                                                                                                                                                                                                                                                                                                                                         |
| `symbols`     | no       | list of symbol names                     | Must exist in the codegraph index; unknown symbols are dropped with a flag (never a hard error — mirrors the mapper's verification).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `depends_on`  | no       | list of task_ids                         | **Directed dependency edges only — never affinity.** The named task is upstream. Feeds the group DAG, hub detection (a scaffold task everything depends on becomes a `utility_hub` → own group, scheduled first), merge guards.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `implements`  | no       | list of route/tag strings                | Contract surface this task provides, e.g. `/api/users`, `UserEvent`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `consumes`    | no       | list of route/tag strings                | Contract surface this task uses. A matched `implements`/`consumes` value creates a **symmetric semantic affinity edge** between the two tasks (the cross-stack signal codegraph cannot see).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |

Unlisted keys are a hard error (they are almost always typos of the above).

### Example: `size_hints`

```yaml
  - task_id: u4-reports-api
    description: Reports export endpoint rendering a multi-page PDF
    slice: reports
    files:
      - app/routes/reports.py
      - app/reports/pdf_renderer.py
    size_hints:
      app/reports/pdf_renderer.py: large      # 5,000 tokens — a real module, not boilerplate
    symbols: []
    depends_on: [u1-scaffold]
    implements: ["/api/reports"]
    consumes: []
```

`app/routes/reports.py` is left out of `size_hints` and still prices at
`medium` (2,000 tokens, today's flat rate) — only files whose planner-known
size diverges from that default need a hint.

## Pricing a task's node work

Each task's **node work**, in tokens, is:

```
node_work = source_bytes / 4.0 × 1.3 + per-file allowance
```

- `source_bytes / 4.0` — existing files' bytes converted to tokens at
  `bytes_per_token` (4.0).
- `× 1.3` — `slack_multiplier`, applied to the byte-derived estimate.
- **per-file allowance** — each existing file adds `per_file_tool_allowance`
  (2,000 tokens, the tool-output overhead of reading it); each prospective
  file adds its `size_hints` class price (`small` 500, `medium` 2,000,
  `large` 5,000) or the same flat 2,000 default if unhinted.

A group's (and a slice's) **budget cap** is the token budget minus a fixed
head:

```
cap = 200,000 − head
head = (base_tokens + spec_tokens_allowance) × 1.3 × 2.5
```

`base_tokens` is the size of the shared base context every worker forks from;
`spec_tokens_allowance` is the assembled-spec budget; `2.5` is
`coder_slack_multiplier`, converting read-cost tokens into the coder-session
peak the cap actually guards (a coder iterates, so its real usage runs well
above a single read pass). Node work is scaled by the same `2.5` before it is
compared against the cap, so both sides of the comparison are in coder
tokens.

These are today's defaults (`orchestrator/grouping/estimator.py`,
`orchestrator/config.py`) — check `group --price <plan>` for the actual
resolved values and a sub-second, per-task breakdown against the cap before
running the full pipeline.

## Validation rules

**Hard errors** (`GrouperError`, zero LLM calls — the run stops):

- YAML that does not parse, or a top level that is not a mapping with a `tasks` list
- A task entry missing `task_id` or `description`, or with a duplicate `task_id`
- `depends_on` naming an unknown task_id or the task itself, or forming a cycle
- A slice with more than 5 tasks (a whole-plan slice would contract to one giant
  node and degenerate grouping to pure budget-splitting — split it or drop labels)
- `size_hints` naming a path not present in that task's `files`, or naming a
  class other than `small`/`medium`/`large`
- `size_hints` naming a file that already exists in the working tree — hints
  price unwritten (prospective) work only
- Unknown keys, or wrong types for any field

**Info flags** (recorded in `groups.json` `flags`, run continues):

- A file that does not exist in the working tree → retained as a *prospective
  file* (workers will create it)
- A symbol not found in the codegraph index → dropped

Malformed maps are **never** silently ignored — a broken block would otherwise
hide drift between the plan prose and the map.

## Compatibility rule

**Absent block → the LLM mapper runs as before.** Plans written without a task
map (foreign plans, hand-written plans) keep working unchanged. Present block →
the mapper LLM is skipped and `groups.json` records the flag
`task map: parsed from plan — mapper LLM skipped`.

## Versioning

The marker line pins the contract version. Additive evolution bumps the minor
semantics here and keeps the parser accepting v1 blocks; incompatible changes
mint `orchestrator-task-map v2` (candidate already parked: `feature_tags`).
