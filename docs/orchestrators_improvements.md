# Orchestrator — known defects and improvements

**The register of everything wrong with, or worth improving in, the orchestrator
system.** Living document: append findings as they surface, and delete entries when the
fix lands. Started 2026-07-21 from the session that produced
`docs/plans/2026-07-21-001-feat-orchestrator-observatory-plan.md`, and extended with
findings from executing that plan through the orchestrator itself.

Each defect carries a severity and, where known, a citation. Re-verify citations before
acting — they drift.

| #   | Defect                                                  | Severity | Status                |
| --- | ------------------------------------------------------- | -------- | --------------------- |
| D2  | Slice contraction is undone by the budget splitter      | High     | Resolved (2026-07-25) |
| D3  | Group-DAG cycles fail the run instead of being repaired | High     | Resolved (2026-07-25) |
| D4  | Greenfield work estimation is file-count-driven         | Medium   | Resolved (2026-07-25) |

**Absorbed 2026-07-22:** D1, D5, D6, D7, D8, D9, D10, D11, D12 moved into the
approved run-hardening requirements —
`docs/brainstorms/2026-07-22-orchestrator-run-hardening-requirements.md` (R1–R26;
verified mechanics preserved in its appendix). D5's absorption is the full harness:
`--no-spec` (R18/R19), fixtures + properties (R20/R21), and the opt-in LLM
scenarios (R26) — but *not* the acyclicity-as-invariant assertion, which needs
D3/H1 behavior changes and stays with the study. What remains here is the grouping
study only: D2/D3/D4 and the H1–H5 decision (H5 — repair cycles instead of
raising — included), to be taken up by a dedicated session once the harness has
landed.

______________________________________________________________________

## D2–D6 — Slice dissolution and group-DAG cycles

> **Absorption note (2026-07-22):** within this study, D5 (the full harness:
> `--no-spec` R18/R19, fixtures + properties R20/R21, opt-in LLM scenarios R26)
> and D6 (the must-link doc correction, R22) are absorbed by the run-hardening
> requirements. The D2/D3/D4 mechanisms and the H1–H5 decision below — including
> H5's repair-instead-of-raise, which is a partitioner behavior change — remain
> the open brief; the study text is kept whole for coherence.

> **Resolution (2026-07-25):** D2/D3/D4 are resolved by
> `docs/plans/2026-07-25-001-feat-orchestrator-grouping-improvement-plan.md`.
> The causal model below — D2/D6 blaming the budget splitter alone, and H2's bet
> that a lower `per_file_tool_allowance` would restore slices — was **falsified
> by measurement** during that plan's exploration. Slice dissolution and
> group-DAG cycles have **three independent mechanisms**, not one:
>
> - **M1 — hub-role exclusion deletes the slice before contraction runs.**
>   `slice_atoms` filtered every slice member classified `aggregator_hub` /
>   `utility_hub` out *before* contraction, so a slice with a hub-shaped member
>   (a vertical's backend half, which naturally has several upstreams) dropped
>   to one member and was never contracted at all — it wasn't split by the
>   budget splitter (the original D2 account); it was deleted earlier.
> - **M2 — `merge_small_groups` creates the cycles, not the splitter.** It had
>   budget, chain-compatibility, and makespan guards but no acyclicity guard, so
>   merging a small group across an intermediate group could invert a
>   dependency edge in the group-level quotient graph. This — not D3's original
>   "the splitter cut a slice in a direction that inverted an edge" — is what
>   produced most of the cycles below.
> - **M3 — `split_over_budget` cutting inside an expanded slice** (D2's
>   original account) is real, but secondary: it explains the brownfield
>   run-hardening regression at low allowances and the `slice-over-budget`
>   fixture, not the Observatory dissolution (that was M1).
>
> **H2 was tested first, as this study recommended, and falsified it:**
> sweeping `per_file_tool_allowance` from 2000 down to 100 never restored a
> dissolved slice, and *lowering* it dissolved a slice that had survived at the
> default (cheaper nodes let `merge_small_groups` build larger clusters that
> then exceed the cap and get cut) — recorded as evidence, not a default, in
> `docs/orchestrator-grouping.md`. **H1 was taken** (slice must-link is now a
> hard output invariant, enforced at every stage that could break it — see
> `docs/orchestrator-task-map.md`), plus **H5** (a surviving group-DAG cycle is
> repaired — SCC-merge then a dependency-safe re-split — rather than raised;
> `GroupCycleError` reaching the operator is now an orchestrator bug, not an
> expected outcome). **H3** (drop `slice:`, accept layer-shaped groups) and
> **H4** (grouper becomes advisory) were considered and not taken — the
> measured fixes make `slice:` mean what the contract always said it meant.
> Pricing gained the `size_hints` field for precision (H2's honest half: a
> `tsconfig.json` shouldn't cost what a real module costs) but is explicitly
> **not** the slice-integrity lever. The exploratory study below is kept for
> the mechanism data it recorded — the mechanism *conclusions* it draws (D2/D3
> as originally stated, and the "test H2 first" recommendation as a fix rather
> than a falsified hypothesis) are superseded by this note.

Everything below is the original slice-dissolution study, from writing the Observatory
plan. That plan shipped and groups cleanly; this section is about what went wrong on the
way there, and is the brief for a session that improves the grouper. Nothing here proposes
changing the Observatory plan.

### TL;DR

Writing one greenfield plan took **five `group --dry-run` rounds**, three of which failed
with `dependency cycle across groups`. Each round costs ~5 minutes, almost all of it the
speccer LLM, which runs *after* every deterministic stage that could have failed.

The loop I fell into was **editing the plan document to steer the partitioner** — moving a
unit's `depends_on`, retagging `implements`/`consumes`, adding and removing shared stub
files — and re-running to see which shape the partitioner tolerated. That is tuning the
input to fit the tool, and it is the wrong loop. It also produced a lot of low-value diff
churn (`events.py` → `events.py *(new)*` and back) as I flip-flopped between two designs.

Two findings matter more than the cycles:

1. **In both runs that succeeded, every slice was dissolved anyway.** All three vertical
   slices were split across groups. The `slice:` labels — the whole vertical-slice
   feature — contributed nothing to the final grouping.
2. **For greenfield plans, a task's estimated work is almost entirely file *count*.**
   Prospective files contribute zero source bytes but a full `per_file_tool_allowance`, so
   `node_work ≈ 2000 × len(files)`. Listing `tsconfig.json` costs the same 2,000 tokens as
   listing a 400-line module. Group sizing on a greenfield plan is therefore driven by how
   granularly the planner enumerated files, not by how much work there is.

### What happened, round by round

Plan: 10 units, 3 slices of 2, ~50 files, nearly all prospective (greenfield front-end +
new backend package).

| #   | Plan shape                                                                                             | Result                             |
| --- | ------------------------------------------------------------------------------------------------------ | ---------------------------------- |
| A   | U3 (SPA hub) `depends_on` U2 (backend hub); U10 (verification) `depends_on` all three slices           | `cycle across groups [0,1,2,3]`    |
| B   | U3 made a root; `consumes` moved so only U3 (the API-client owner) consumes routes                     | `cycle across groups [0,1]`        |
| C   | U10 made fully isolated (no edges in or out); its tests distributed into the units owning each surface | **OK** — 4 groups, g1 = 86,577 tok |
| D   | Removed U2's stub router modules in favour of `pkgutil` auto-discovery in `app.py`                     | `cycle across groups [0,1,2]`      |
| E   | Reverted D                                                                                             | **OK** — 4 groups, g1 = 87,699 tok |

Final grouping (E), against the intended 3 vertical slices:

```
g1  u1, u2, u4, u6, u8, u10   87,699 tok  paired_plus  30 verification items
g2  u3, u5                    51,324 tok  self_verify
g3  u7                        18,990 tok  self_verify
g4  u9                        18,976 tok  self_verify
```

Slice `live-board` = {u4, u5} → split g1/g2. Slice `hitl` = {u6, u7} → split g1/g3. Slice
`drill-in` = {u8, u9} → split g1/g4. **Three for three dissolved.** What came out is four
horizontal layers: one backend group and three front-end groups.

### Why — the mechanism

All citations are current as of this commit; re-verify before acting on them.

#### D5 — The expensive stage runs last, and the failing stage runs first

`run_grouping` (`orchestrator/grouping/pipeline.py:47`) is:

```
parse_task_map  →  build_task_graph  →  partition  →  build_group_dag  →  write_specs
   (no LLM)         (codegraph)        (deterministic)   (RAISES HERE)      (~5 min LLM)
```

`DefaultPartitionStrategy.partition` (`partition.py:166`) ends with
`build_group_dag(graph, partition)  # cycles must fail loudly`. So **every cycle failure
is fully deterministic and reachable in about a second**, and every *success* pays five
minutes for specs I did not need while iterating on structure. This is the single biggest
lever: there is no way today to ask "how would this plan group?" without paying for specs.

#### D2 / D6 — Slice contraction is soft, and the softening is what breaks it

`DefaultPartitionStrategy.partition` (`partition.py:166-186`):

```python
roles = detect_hub_roles(graph, threshold=self.hub_threshold)
atoms = _slice_atoms(graph, roles)          # slice -> core members, 2+
if atoms:
    unit_graph, self_loops, unit_of = _contract_slices(graph, atoms)
    unit_partition = _hub_isolated_clustering(...)
    unit_partition = lift_independent(unit_graph, unit_partition)
    partition = {node: unit_partition[unit_of[node]] for node in graph.nodes}   # EXPAND
...
if self.budget_cap is not None:
    partition = split_over_budget(graph, partition, self.work_fn, self.budget_cap)
partition = merge_small_groups(graph, partition, self.work_fn, self.budget_cap)
```

Its own docstring is explicit: *"Softness comes after expansion: `split_over_budget` may
still break an oversized slice at its weakest internal edges and `merge_small_groups` may
combine small ones."*

`split_over_budget` (`partition.py:414`) runs on the **expanded** node set — the
supernodes are already gone — and does reverse-Kruskal: drop the weakest internal affinity
edges until the group falls apart. It has no knowledge that `u4` and `u5` were must-linked.
So the contract in `docs/orchestrator-task-map.md` — *"Slice-mates are **contracted into
one node** before Louvain — a hard must-link"* — is accurate only through Louvain. It is
**not** an invariant of the pipeline output. That gap is arguably a documentation bug on
its own.

#### D3 — Why a split slice becomes a cycle

`depends_on` edges are directed and preserved. Once `u4` lands in g1 and `u5` in g2, the
edge `u4 → u5` becomes `g1 → g2`. Cycles appear when a *different* split puts an edge back
the other way — e.g. `u3 → u7` giving `g2 → g1`. The task DAG is still perfectly acyclic;
only the quotient graph cycles. `build_group_dag` then fails the whole run.

So the cycles were never a defect in the plans I wrote. They were the budget splitter
cutting slice supernodes in a direction that happened to invert an edge.

#### D4 — Greenfield work estimation is file-count-driven

`node_work` (`estimator.py:36`):

```python
source_bytes = int(metadata.get("source_bytes", 0) or 0)
file_count = len(metadata.get("files", ()) or ()) + len(metadata.get("prospective_files", ()) or ())
tokens = source_bytes / config.bytes_per_token * config.slack_multiplier
return tokens + file_count * config.per_file_tool_allowance
```

On a greenfield plan `source_bytes == 0` for essentially every task, so
`node_work == 2000 × file_count` (`per_file_tool_allowance` default 2,000). U3 listed 16
files — several of them `tsconfig.json`-class boilerplate — and was priced at ~32k of
"work". The partitioner then split the plan to respect a cap derived from that number.

The effective cap is also lower than `token_budget` suggests
(`estimator.py:52`):

```python
head = (base_tokens + config.spec_tokens_allowance) * config.slack_multiplier
return max(config.token_budget - head, 0.0)
```

With `token_budget=100_000`, `spec_tokens_allowance=3_000`, `slack_multiplier=1.3`, the cap
on summed node work is `100_000 - (base_tokens + 3_000) × 1.3` — meaningfully below 100k
once the base context is counted. *(Worth verifying whether the per-group `est. tokens`
printed in the report includes that head; I did not confirm this and did not rely on it.)*

#### `depends_on` carries no affinity — by design, but it has a consequence

`docs/orchestrator-task-map.md`: *"Directed dependency edges only — never affinity."* True
in the code. The consequence is that **a unit whose only relationships are dependencies has
no gravity at all**. My original U10 (verification) depended on all three slices and shared
files with nothing; Louvain parked it in the hub cluster, and the slices then pointed back
into that cluster. That is round A's cycle.

Corollary that shaped rounds C/D/E: **shared files are the only real grouping lever a plan
author has.** Round D removed U2's stub router files to decouple the backend units; that
removed their only anchor and immediately re-cycled. Round C/E kept the shared stubs, which
anchored u4/u6/u8 to u2 — and that is precisely *why* the whole backend collapsed into one
87.7k group. The thing that made the plan groupable is the same thing that destroyed the
verticals.

#### Cross-stack verticals are structurally impossible right now

A `live-board` slice is meant to hold `events.py` (Python) and `GroupBoard.tsx` (TS). There
is no codegraph edge between them, they share no files, and `depends_on` contributes no
affinity. The only cross-stack signal is the `implements`/`consumes` route tag. So the
*only* force holding a cross-stack slice together is slice contraction — the one thing
`split_over_budget` is free to undo. When it does, nothing is left.

### The real question to study

I do not think this is settled, and I would not want the next session to start from my
guess. The plausible positions, with what each would cost:

**H1 — Slices should be a hard constraint.** Make `split_over_budget` and
`merge_small_groups` slice-aware: never cut inside a slice; if a slice alone exceeds
budget, fail loudly and tell the planner to split it. Pro: `slice:` finally means what the
contract says. Con: greenfield file-count inflation means slices will blow the budget
often, so this trades cycles for a different loud failure — possibly a better one, since
the error would name the actual problem.

**H2 — The estimator is the bug, not the partitioner.** If prospective files were priced
by something other than a flat 2,000 (declared size hint? a smaller greenfield rate?), the
Observatory plan might fit in fewer, larger groups and never trigger the splitter at all.
Cheapest to test: re-run the existing plan with `per_file_tool_allowance` at 500/1000 and
see whether slices survive. **Do this first — it is one config change and one dry run.**

**H3 — Vertical slices are the wrong abstraction for this pipeline.** Louvain optimizes
modularity over affinity; verticals are by definition low-affinity across the stack
boundary. Forcing them may be fighting the algorithm. The honest alternative is to accept
layer-shaped groups (which is what actually came out, and which is not obviously worse —
one worker owning a coherent backend API is defensible) and drop `slice:` from the
contract rather than keep a feature that measurably does nothing.

**H4 — The grouper becomes advisory.** The planning session already decides slices; let
the task map declare group membership directly and have the grouper validate (budget,
acyclicity, hub ordering) rather than cluster. Biggest change, but it removes a whole class
of "why did it group that way" debugging.

**H5 — Cycles should be repaired, not raised.** A group-DAG cycle is always caused by a
cut the partitioner itself chose. It could back off the offending cut and re-split
elsewhere instead of failing the run. This is orthogonal to H1–H4 and may be worth doing
regardless.

b

My weak lean is H2 first (cheapest, and file-count inflation is clearly *a* real bug),
then H1, with H5 as an independent robustness fix. But H3 deserves a genuine hearing —
"the feature did nothing in 2/2 successful runs" is evidence, and I would rather delete a
feature than keep one that only appears to work.

### Proposed harness — stop paying 5 minutes per question

#### 1. A partition-only entry point (prerequisite for everything else)

Everything deterministic already precedes `write_specs`. Expose that:

- `smart-mcps-orchestrate group <plan> --no-spec` (or `--explain`), printing the partition,
  the group DAG, per-node `node_work`, the budget cap, detected hub roles, slice atoms, and
  which stage last modified the partition.
- Refactor `run_grouping` so the pre-speccer half is callable on its own — the speccer call
  at `pipeline.py:111` is the natural seam.

This turns a 5-minute question into a sub-second one and is what makes the rest cheap. It
is also directly useful to planning sessions: "will this plan group?" without burning
tokens.

#### 2. Deterministic fixture plans (no LLM)

`tests/fixtures/grouping/*.md` — small plans, each isolating one behaviour, asserted
through the partition-only path:

- `greenfield-cross-stack.md` — the Observatory shape in miniature: 2 hubs, 3 two-task
  cross-stack slices, 1 isolated doc task. **Regression for this session.** Assert: no
  cycle, and record whether slices survive.
- `slice-over-budget.md` — one slice whose files alone exceed the cap. Asserts what we
  *decide* should happen (hard error under H1, silent split today).
- `hub-in-the-middle.md` — hub B depends on hub A, feature units depend on B. Round A's
  cycle, minimized.
- `no-affinity-sink.md` — a task depending on everything, sharing files with nothing. Round
  A's other cycle, minimized.
- `pure-backend.md` — same-stack plan where affinity is real; control case showing slices
  behave when the stack boundary is absent.
- Existing-code variants of the above, so greenfield vs brownfield estimation is compared
  on the same shapes.

Property assertions worth encoding regardless of which hypothesis wins:

- The group DAG is acyclic for every fixture (today: 3 of 5 shapes fail).
- No group's summed `node_work` exceeds the cap.
- Partitioning is byte-stable across runs (the memory notes a prior dry-run 3 vs real 2
  discrepancy — worth re-testing now that the mapper LLM is skipped for task-map plans).
- Under H1: no group contains a strict subset of a slice.

#### 3. LLM-in-the-loop scenarios (few, explicit, opt-in)

Marked `@pytest.mark.llm` and excluded from the default run, since the suite is currently
zero-token:

- One end-to-end `group` on a fixture plan asserting the `task map: parsed from plan — mapper LLM skipped` flag and non-empty specs.
- One *without* a task map, exercising the mapper fallback on a greenfield plan — that path
  has the known "drops nonexistent-file mappings" behaviour and is not covered by the
  deterministic fixtures.

#### 4. Suggested order for the next session

1. Build `--no-spec`. Everything else is gated on it.
2. Port the five fixture plans; record current behaviour as a baseline (expect failures —
   that is the point).
3. Test H2 by sweeping `per_file_tool_allowance`. One config knob, immediate signal.
4. Decide H1 vs H3 with the baseline table in hand, not from first principles.
5. If H1: make `split_over_budget` / `merge_small_groups` slice-aware, and fix
   `docs/orchestrator-task-map.md`, which currently overstates the must-link guarantee.

### Process note for planning sessions, until this is fixed

Do not iterate on a plan document to satisfy the partitioner. If `group` reports a cycle,
the useful response is to record the shape that caused it (here) and pick a plan structure
on its own merits — not to keep permuting `depends_on` and file lists until the tool stops
complaining. Three of my five rounds produced no improvement to the plan as a document.

Related: `docs/orchestrator-task-map.md` (the contract), `orchestrator/grouping/partition.py`
(hub roles, contraction, splitting), `orchestrator/grouping/estimator.py` (work and cap),
`docs/handoffs/2026-07-16-multiagent-orchestrator-phase-d-and-grouping-next.md` (the
grouping-quality items already queued).

______________________________________________________________________

## Live-run observations — `obs1` (Observatory plan, 2026-07-21)

The first execution of a task-map plan through the full orchestrator, on
`test/orchestrator-frontend` with `--hitl --intensity on_stuck`. Recorded as it ran;
findings are promoted to numbered defects above once understood.

**Setup.** Four groups from the task map, matching the plan's own prediction almost
exactly:

```
g1  observatory-backend   u1,u2,u4,u6,u8,u10   87,725 tok  paired_plus  28 items  (root)
g2  spa-shell-and-board   u3,u5                54,111 tok  self_verify           (← g1)
g3  escalation-panel-ui   u7                   19,059 tok  self_verify           (← g1,g2)
g4  group-drill-in-ui     u9                   19,034 tok  self_verify           (← g1,g2)
```

Confirms the D2 finding independently: all three vertical slices dissolved again
(`live-board` split g1/g2, `hitl` split g1/g3, `drill-in` split g1/g4), and the result is
four horizontal layers. Second run, same outcome — this is the grouper's stable behaviour
on cross-stack plans, not a fluke.

### Observations

- **D1 reproduced here** (cold codegraph index silently dropped six real symbols; absorbed
  as run-hardening R13/R14). Found on this run's very first `group` invocation.
- **The DAG is shared, not per-run (ADR 0002), and it bit immediately.** Running `group`
  for the Observatory plan overwrote `.orchestrator/groups.json`, which still described
  the earlier `smoke1` run. `smoke1` is the fixture U2 needs for its post-mortem tests, so
  its DAG had to be hand-copied into `runs/smoke1/groups.json` *before* grouping, or it
  would have silently acquired the Observatory's DAG. U1 fixes this going forward, but it
  confirms the ADR's premise with a concrete loss: **any run predating the snapshot feature
  has an unrecoverable DAG once a new `group` runs.**

### Absorbed findings (D7–D12) — moved to run-hardening requirements (2026-07-22)

Full write-ups, verified mechanics, and fix decisions now live in
`docs/brainstorms/2026-07-22-orchestrator-run-hardening-requirements.md`.
One-line record of what this run surfaced:

- **D7** — `run.log` emitted ~4 lines per group and nothing at all in autonomous
  mode (`_log` gated on the HITL broker) → always-on lifecycle log, R10–R12.
- **D8** — a 900s round timeout killed a healthy g1 four units in → the round
  timeout is removed entirely, R7 (decision: no replacement, no watchdog).
- **D9** — envelope failures (timeout, `claude exited 1`/empty stderr) landed as
  terminal `failed`, needing manual `state.json` surgery twice in ~40 min →
  `INTERRUPTED` state + resume-first re-entry, R1–R6, R9.
- **D10** — the venv lives outside the worktree; dependency-adding units were
  unverifiable → per-worktree venv, R16–R17.
- **D11** — a verification item introspecting FastAPI internals failed while the
  behaviour was correct → behavioural-phrasing guidance, R23.
- **D12** — the orchestrator drives itself from a stale editable install; `obs1`
  could not use its own snapshot feature → docs + plan-time warning, R15/R24
  (worker-side half fixed by R16's per-worktree venv).

### Correction to this run's recorded setup

`obs1` ran **sequentially**, not at concurrency 3. `.orchestrator/config.toml` in the
target repo sets `sequential = true` and `timeout_s = 900.0`, and config beats defaults
(CLI flags > config file > defaults). No `--concurrency` flag was passed, so the config
won. Worth noting as an operator trap: the run banner does print `concurrency 1`, but only
*after* launch, and the value that mattered — a 900s timeout on an 87.7k-token group — is
never surfaced at all.

### Config change applied after `obs1` (2026-07-22)

In response to D8, the target repo's `.orchestrator/config.toml` sets
`session.timeout_s = 7200.0` (2h/round) as a stopgap. Superseded by D8's decided
resolution (2026-07-22): the round timeout is removed entirely (run-hardening R7),
at which point this key becomes a no-op and draws a deprecation warning.

`estimator.token_budget` was **left at the default 100k**. It was briefly bumped to 200k
and reverted once the two budgets below were disentangled — 200k is the wrong lever for the
problem it was reached for.

### Two budgets are easy to confuse — and they are not the same knob

The operator asked, correctly, whether `token_budget` governs the grouper or the
worker-hits-limit-and-respawns behaviour. It is the **former only**. There are two
independent token budgets:

| Knob                            | Default | Scope                   | What it controls                                                                                                                                                           |
| ------------------------------- | ------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `[estimator] token_budget`      | 100k    | **plan time** (`group`) | How large a group the partitioner builds. Read only by `run_grouping`; never consulted during a run.                                                                       |
| `[breaker] context_token_limit` | 120k    | **run time** (worker)   | The latest-round context size at which a coder session is **retired and a fresh generation is forked** from base. This is the "hit the limit → spawn a new one" threshold. |

To let a worker session run longer before it respawns, raise **`context_token_limit`**, not
`token_budget`. Raising `token_budget` only makes the grouper pack more into each group,
which — absent a matching `context_token_limit` bump — would make workers hit the retirement
threshold *sooner*, not later. That is why the 200k experiment was the wrong move and was
reverted.

**Token-budget semantics, confirmed from the estimator** (`estimator.py:52`) — for the
grouper knob specifically:

```python
def partition_budget_cap(base_tokens, config):
    head = (base_tokens + config.spec_tokens_allowance) * config.slack_multiplier
    return max(config.token_budget - head, 0.0)
```

`token_budget` is the **total** per-group context budget — *inclusive* of the shared base
context, not on top of it. The partitioner does not let a group's summed *task* work
(`node_work`: source bytes + per-file allowances) exceed `token_budget` **minus** the
slacked head (base context + spec allowance).

### Design discussion — compact the worker instead of respawning it

Raised by the operator, 2026-07-22, for later discussion — **not a defect, an open design
question.**

Today, when a coder crosses `context_token_limit`, the breaker **retires the session and
forks a fresh generation from base with a condensed handoff** (`review.py:9`,
`_advance_generation`). The retired session's live working memory is discarded; the new
fork re-orients from git and a short summary.

The alternative is to **compact the worker in place** — summarize the session's own
conversation and keep going in the same session, the way Claude Code's `/compact` does —
rather than throwing the session away and re-forking.

- **For compaction:** preserves the coder's in-flight reasoning and local context (which
  file it was mid-edit on, why it chose an approach), instead of forcing a cold re-orient
  from git each respawn. Fewer wasted rounds re-discovering state.
- **The operator's own caveat:** the fork model shares **one** base session across all
  coders — the base context is compiled once and inherited by every fork, so it is not
  re-paid per generation. A compacted standalone session may **duplicate the base context**
  (it is already inside that session's window, and compaction could re-summarize or re-embed
  it) and loses the cross-coder base sharing. So compaction may trade re-orient cost for
  base-context duplication cost.
- **Open questions to resolve before deciding:** does the CLI expose programmatic
  compaction on a `-p` session at all? Can a forked session be compacted without collapsing
  the shared-base KV benefit? Is the win (continuity) worth losing the single-base-session
  economy the current design is built around? Worth measuring both on a real over-limit
  group before committing either way.

### `obs1` completed — end-to-end result (2026-07-22)

**All four groups completed and merged; the full 10-unit Observatory plan is on
`orchestrator/run-obs1`.** Timeline: g1 (backend, `paired_plus`) survived one round-timeout
(D8) and one manual resume, then dual-approved; g2 (SPA) survived one envelope failure (D9)
and one manual resume; g3 and g4 ran clean first try. Three interruptions, two distinct
causes, **zero lost work** — the resume-with-worktree-reuse path held every time.

Independently verified after completion:

- **341 Python tests pass** on the integration branch.
- **The SPA builds** (`tsc && vite build`, 40 modules, ~164 KB bundle, no TS errors).
- **The Observatory serves and renders its own creation run.** Launched the backend against
  the fe-test repo and exercised every endpoint against real `obs1`/`smoke1` data:
  - `/api/projects` → the registry project, `error: null`.
  - `/api/runs` → `obs1` and `smoke1`, newest-first.
  - snapshot → 4 groups `completed` with `group_id`/state/generation/failure/`depends_on`,
    the groups→sessions join (g1 shows the resumed coder + reviewer; the timed-out session
    is correctly absent), and DAG edges `g1→g2, g1→g3, g2→g3, g1→g4, g2→g4`.
  - the **`stale_dag` ladder works both ways**: `obs1` → `true` (fell back to the shared
    DAG, per D12), `smoke1` → `false` (preferred its per-run snapshot). This is R20's
    board/DAG half, proven live.
  - transcript → U8's tolerant parser normalized the g1 coder log into 119 events
    (text/tool_use/tool_result) with the right `seq/role/kind` shape.
  - artifacts → g1's `report` + both `verdict` JSONs, parsed.
  - escalations → `[]` (none fired). SPA root → the built `index.html`.

The only blemishes are D12 (the run couldn't use its own per-run-snapshot feature because
it was driven by stale installed code) and the known layer-shaped grouping (D2). Neither is
a plan defect; both are recorded above.

*(Append further findings here as the run progresses.)*
