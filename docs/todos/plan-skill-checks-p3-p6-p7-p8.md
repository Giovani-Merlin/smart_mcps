# Planning-skill checks P3 / P6 / P7 / P8 — a small plan of their own

**Source:** `docs/ideation/2026-09-15-B-agent-kinds-and-plan-checks.md` Part 2.
**Split out** by the Unit Recipes brainstorm (2026-09-21) because none of the
four depends on recipes; recorded here 2026-09-23 so the split has a home.

| Check | What it adds                                                                                                             | Where it lives                                    |
| ----- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------- |
| P3    | A unit that changes a signature must list its call sites in `files:`; check via codegraph callers against a synced index | `group` (has the index) or `plan-check --callers` |
| P6    | A unit whose Done means "files on disk" must name a `[workspace] data_dirs` path, or the grouper warns                   | `group`                                           |
| P7    | Flag any unit that asks a coder for a large natural-language table; route to a generator script plus committed artefact  | `/orchestrator-plan`                              |
| P8    | Deepen's edge-case questions ask "which input exercises this edge case?" and put that input in the `Run:` item           | `/orchestrator-deepen`                            |

Status: not started. Independent of Plan A (review-loop split) and Plan B
(Unit Recipes); can run in parallel with either.
