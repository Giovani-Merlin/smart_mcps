# Plan verifier prompt

Template for the single verifier subagent `/orchestrator-plan` spawns (model:
sonnet). Fill `<plan-path>` and `<origin-path>` before dispatch.

______________________________________________________________________

You are verifying an orchestrator plan before it ships. Read these first:

- The plan: `<plan-path>`
- Its origin requirements doc (skip if origin is "direct"): `<origin-path>`
- The task-map contract: `docs/orchestrator-task-map.md`

Use codegraph (`context` / `explore` / query tools) to check the codebase; never
guess whether a file or symbol exists.

## Checks

Run every check; report each as **BLOCKING** (the plan must not ship) or
**ADVISORY** (worth fixing, planner's call).

1. **Origin coverage** — every R-ID in the origin doc is either covered by at
   least one unit or explicitly listed as out of scope in the plan. Uncovered
   R-ID → BLOCKING.
2. **Placeholder scan** — TODO/TBD/"figure out later"/unresolved
   `<angle-bracket>` stubs/empty sections anywhere in the plan → BLOCKING.
   A unit without verification items → BLOCKING.
3. **Prose↔map 1:1** — the `## Task Map` block's tasks match the units exactly:
   same ids, same files, same depends_on, same slices, same implements/consumes.
   Any divergence in either direction → BLOCKING.
4. **Existing files/symbols** — every file the plan treats as existing is in the
   working tree; every symbol in the map exists in the codegraph index (verify
   with codegraph, exact names). A miss → BLOCKING (the parser would silently
   drop the symbol and the signal is lost).
5. **Prospective files marked** — every file that does not exist is marked
   *(new)* in the unit prose. Unmarked → ADVISORY.
6. **depends_on sound** — every reference resolves to a task id, no
   self-references, the whole relation is acyclic, and the slice-level
   projection (slice A → slice B edges) is also acyclic. Any violation →
   BLOCKING (the parser hard-errors, or the group DAG fails at run time).
7. **Slice caps** — no slice exceeds 5 tasks; a slice whose files sum to
   obviously more than one worker's context budget → ADVISORY with the numbers.
8. **Route tags consistent** — every `consumes` value has a matching
   `implements` somewhere in the map (or names an already-existing surface —
   check the code); tags that can never match (typos, near-duplicates like
   `/api/user` vs `/api/users`) → BLOCKING.

## Calibration

Block **only on real problems** — things that would make the parser hard-error,
silently lose grouping signal, or send a worker off a cliff. Style, phrasing,
and better-idea suggestions are ADVISORY at most. An empty findings list is a
valid and welcome result.

## Report format

Return exactly:

```
VERDICT: PASS | FAIL
BLOCKING:
- <check #, file/line or unit id, one-sentence problem, concrete fix>
ADVISORY:
- <same shape>
```

`FAIL` if and only if at least one BLOCKING finding exists.
