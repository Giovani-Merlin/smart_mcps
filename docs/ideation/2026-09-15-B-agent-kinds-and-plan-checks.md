---
title: "Brainstorm seed B — not every unit is a coding task: agent kinds, plus the planning-skill checks that go with them"
date: 2026-09-15
status: seed (input to /orchestrator-brainstorm)
sources:
  - learning_podcast/docs/orchestrator-feedback.md §1, §2, §3, §8, §10, §11
  - learning_podcast/.orchestrator/notes-r20260905-200024.md
  - learning_podcast/.orchestrator/notes-r20260907-123644.md
  - learning_podcast/.orchestrator/notes-r20260907-131128.md
  - learning_podcast/.orchestrator/notes-r20260908-224537.md
  - learning_podcast/.orchestrator/notes-r20260913-162134.md
  - .orchestrator/notes-sweep-2026-09-15.md (B6, B7, P1–P8)
---

# B — Not every unit is a coding task

## The thesis

Every group today runs the same machine: **a coder session plus an optional
reviewer, looping on a worktree, committing code, merged through the
preflight gate.** Six of the problems from the last five learning_podcast
runs are this machine being used for work that isn't coding:

| #   | symptom                                                                                                                                                            | the work was really…                       |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------ |
| B6  | Workers can't call `claude`, `VAR=1 uv run …`, `bash x.sh`, `setsid`/`nohup` — real-model checks escalate at midnight (r0913 g11, g5)                              | **running** a costly real-model job        |
| B7  | A Bash call is capped at 10 minutes and background tasks die between rounds — 3 generations lost on one render (r0907 g6)                                          | **running** a long job                     |
| P1  | A unit is sized by its file list; two "tiny" units merged and the coder overflowed at 272k tokens on command output (§1, r0908 g8)                                 | **running** + **judging**                  |
| P2  | `Pass:` shares on live model output fail even when the change worked — 9× improvement, gate still red (§2, r0908 U13/U17)                                          | **measuring** — an observation, not a gate |
| P4  | "Map every reader note to a pair" became keyword matching; the human rejected it, and the driver redid it with 4 LLM subagents: 57/236 notes moved (§10, r0913 U2) | **LLM batch** interpretation               |
| P5  | Real-model / >10-minute verification was never marked operator-run or given a cost (§8, r0907)                                                                     | **running**, with a budget                 |

Patching each one on its own (widening the allowlist, adding a timeout
exception, adding a plan-skill rule) treats symptoms. The structural fix is to
let a plan unit **declare what kind of work it is**, and give each kind its
own machine.

Already shipped in 0.17.2, **not** in scope here: worker ground rules now say
how to list symlinked data folders (Glob, not `ls`), and that a >10-minute
command must be split or reported `skipped` rather than relaunched. That is a
stopgap for B7, not the fix.

______________________________________________________________________

## Part 1 — Agent kinds

### Candidates, from what the runs actually needed

| kind                            | what it does                                                                                                                                            | evidence it is needed                                                                           | what differs from `code`                                                                                                                                               |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code` (today)                  | coder ↔ reviewer on a worktree, commits, merge gate                                                                                                     | everything so far                                                                               | —                                                                                                                                                                      |
| `llm-batch` ("brute LLM power") | split an input into shards, run one LLM call or subagent per shard against a brief, validate each output against a schema, merge results                | r0913 remap: 4 subagents, one per chapter, with a written brief and a verify-then-resume loop   | no coder loop; size = shards × per-shard budget; completion = schema-valid output for every shard; review = spot-check sample                                          |
| `run`                           | execute a declared command detached, with a wall-clock timeout and a $ cap, poll it, capture measurements; the driver or a small model triages failures | r0907 g6 / r0913 g5 renders; the operator had to run them unconfined both times                 | no Bash tool cap (the orchestrator owns the process); outputs go to `data_dirs` by contract; size = expected output, not files; needs a cost figure before launch (P5) |
| `research`                      | web / Perplexity / docs research → a findings doc with sources                                                                                          | r0907-123644: "is there a larger DeReKo list?" was asked, researched by hand, and answered "no" | read-only on the repo except `docs/research/`; review = source checking, not tests; no merge gate                                                                      |
| `test` / `evaluate`             | measure an outcome on real artifacts and report it; judge passes                                                                                        | r0908 g8 (twelve judge passes + acceptance report), §2 observations                             | completion = measured and written down (P2); never blocks on a threshold; *the user will define this one further*                                                      |

### What a kind would define

A kind is a bundle. The brainstorm should settle which of these are really per
kind, and which stay shared:

1. **Prompt / role file** — `prompts/<kind>.md` instead of `coder.md`.
2. **Tool allowlist** — `run` needs process control, `research` needs web but
   no Edit, `llm-batch` needs `claude -p` or the Agent tool. This replaces
   growing one shared `DEFAULT_ALLOWED_TOOLS` (`config.py:~40`) for everyone.
3. **Completion contract** — the report schema (`report_contract.md`). Gates
   vs observations live here.
4. **Review mode** — reviewer session, sampled spot-check, source check, or
   none.
5. **Merge behaviour** — merge gate, docs-only merge, or no merge (outputs go
   to `data_dirs`).
6. **Sizing model** — the estimator prices files read today (`group --price`).
   `run` and `llm-batch` need output-based or shard-based pricing (P1).
7. **Budget** — rounds/tokens today; `run` needs wall-clock + $, and
   `llm-batch` needs per-shard caps.

### Where it plugs in (verified on main 055abfa)

- **Task map** (`docs/orchestrator-task-map.md`): fields are `task_id, description, slice, files, size_hints, symbols, depends_on, implements, consumes`. A `kind:` field is the natural carrier. It needs a
  compatibility rule: kinds can't share a group, or can only share it with
  `code`?
- **Group model** (`model.py` `Group`): no kind field. The grouper would need
  "never merge two different kinds" (or "never merge two `run` units", the
  minimum from §1).
- **Review loop** (`execution/review.py`): one ~1,700-line class built around
  the coder ↔ reviewer ferry. The decision point is whether a kind is a new
  executor behind the scheduler's `Executor` seam (`scheduler.py`,
  `make_executor(deps)`) or a strategy inside the review loop. The scheduler
  already treats the executor as opaque, which favours a separate executor per
  kind.
- **Session names** already carry a role (`sessions.session_display_name(run, gid, role, gen)`), so export/Observatory attribution has somewhere to put a
  kind.
- **Bundle / export contract** (`docs/run-bundle-contract.md`): any new
  session role or artifact is a contract change, and Infinity Skills ingests
  it.

### Open questions

- Is `run` really an agent, or a deterministic step the orchestrator runs
  itself, with an LLM only on failure? (The render in r0913 needed no
  judgement until it failed on a network outage.)
- `llm-batch` inside the orchestrator (it spawns N `claude -p` calls) or
  delegated to one session using the Agent tool? The first is observable and
  budgetable; the second is how the driver actually did it.
- Can a unit mix kinds (g8 = run renders, then judge, then write a report), or
  must the plan split it into `run` → `test` → `code` units with
  `depends_on`?
- Cost gate: does `run`/`llm-batch` with a $ estimate above a threshold always
  escalate before launch (the r0913 g5 $70–160 render sat unanswered
  22:10→00:43)?
- Confinement: Landlock and the allowlist were designed around one worker
  profile (`confinement.py`). Per-kind profiles need their own threat model —
  `run` executes plan-declared commands unconfined by the Bash rules.

______________________________________________________________________

## Part 2 — Planning-skill checks (plan / deepen / group)

These go with agent kinds because the plan skill is what would *assign* a
kind, and several checks only make sense once kinds exist. Items marked
**independent** could ship before kinds do.

### P1 — Units that run things can't be sized by files *(needs kinds)*

§1: U16 + U17 were merged because their file lists were two markdown files;
the work was six renders, twelve judge passes and three EPUB builds →
272,048 / 250,000 tokens. With kinds: `run`/`test` units are priced by
declared output and never merged with each other. Without them: at minimum a
`runs: heavy` flag the grouper refuses to merge.

### P2 — Gates vs observations *(needs kinds, or at least a new verification field)*

§2: a `Pass:` expressed as a share is only a gate when its denominator is
fixed and its numerator is under the unit's control. U13's "common prefix ≥
60 %" and U17's "quoted-word share ≥ 0.20" failed both conditions. Also
r0907-123644: "≥ 180 lemmas" was written without checking the source corpora,
whose real ceiling was 134. `/orchestrator-deepen` should classify every
numeric `Pass:` as **gate** (deterministic, computed from code the unit
controls) or **observation** (measure, report, state whether it cleared). A
count over derived data must be checked against the actual source first.
`VerificationItem` (`model.py:32`) has only `id, description, required` —
`required=False` is the closest existing hook.

### P3 — A signature change must carry its call sites *(independent)*

§3: U6 changed `triage.build_triage_prompt`'s signature; its caller
`scripts/build_pipeline_doc.py` wasn't in U6's `files:`, so the grouper saw no
dependency. It was caught only because an unrelated group ran the doc build.
Same family, r0907: u7 (gloss budget) and u9 (expression register) both needed
`gate/stage.py`, and neither listed it. Note that `plan-check` is **zero
codegraph by design** (`cli.py:_cmd_plan_check`), so this check belongs in
`group` (which has the index) or in a new `plan-check --callers` mode.
Run it against a synced index (`codegraph sync` first): the grouper silently drops symbols on a cold index.

### P4 — Interpretation must say who interprets *(becomes the `llm-batch` kind)*

§10: any unit that turns human prose into structured data must state model vs
rule, and its `Pass:` must include a spot-check against the source (e.g. "10
rows against the EPUB marker"). With kinds, "model" means `kind: llm-batch`.

### P5 — Costly or long verification is declared up front *(becomes the `run` kind)*

§8 + r0907: mark real-model verification operator-run **with a cost figure**,
so the human decides before launch. Any `Run:` step longer than ~10 minutes
per call can't run inside a coder as written. Also from §8: prefer CLI flags
over env-var switches for real backends (`GAB_REAL_FIDELITY=1` matched no
allowlist rule).

### P6 — On-disk deliverables must land in a shared data folder *(independent)*

r0905: the plan's own deliverable (four EPUBs) was built into `output/`, which
wasn't in `[workspace] data_dirs`, and died with the worktree. 0.17.2 now
rescues ignored files as a capped copy into
`groups/<gid>/ignored-outputs/`, but that is a safety net. The plan/grouper
check: a unit whose Done means "files on disk" must name a `data_dirs` path,
or the grouper warns.

### P7 — Bulk language data comes from a generator, not a coder's message *(independent)*

r0907-123644: asking a coder to write a ~180-row German strong-verb table
tripped the API output content filter twice, at the identical point. Bulk
lexical data belongs in a generator script plus a committed artefact. The plan
skill should flag any unit that asks for a large natural-language table. With
kinds, it may be an `llm-batch` over shards or a `run` of a generator.

### P8 — Verification inputs must include the edge case *(independent)*

r0905: U3 counted popup markers only on der-sandmann, which has no word-less
sentences; das-cafe ch02 was the chapter that would have caught the `«[EN]`
defect. Deepen's edge-case questions should ask "which input exercises this
edge case?" and put that input in the `Run:` item.

### Kept as-is (§11, recorded so nobody "fixes" it)

Estimates over budget (216–256k vs 200k) were accurate: g3 and g10 retired
gen 1 on context and passed gen 2 with no escalation. The warning is about
cost, not a blocker.

______________________________________________________________________

## Suggested brainstorm order

1. Settle the **kind contract** (the seven dimensions above) against the four
   concrete cases: r0913 remap (`llm-batch`), r0913 g5 render (`run`), r0908 g8
   (`run` + `test`), r0907 DeReKo question (`research`).
2. Decide the executor seam: a separate executor per kind vs a strategy inside
   `review.py`.
3. Then the plan/deepen side: how a unit gets its kind, plus P1/P2/P4/P5
   rewritten in terms of kinds.
4. P3, P6, P7, P8 can be split into their own small plan at any point; they
   don't wait on kinds.

The `test` kind is deliberately under-specified; the user will develop it.
