---
date: 2026-08-28
topic: grouper-speccer-flow
---

# Grouper flow: deterministic specs, advisory partitions, plan seams — Requirements

## Summary

Remove the mandatory speccer LLM call by assembling group specs
deterministically from the plan's own prose, make the grouper an *advisor*
(multi-granularity comparison + plan-cohesion/phase diagnostics off one cached
graph) that `/orchestrator-plan` consults before asking the user whether to
split a plan, and ship the already-diagnosed validation fixes (C1–C6) so a
written plan never again costs 15 grouper invocations. Spec *deepening* (edge
cases, sharper verification, human grilling) becomes a separate optional skill
that enriches the plan doc itself; the evaluation golden dataset is deferred to
post-Infinity-Skills.

## Problem Frame

Measured on the 2026-08-26 plan (36 units, 28 groups,
`.orchestrator/runs/r20260827-060206`):

- The grouping's **only** LLM call was the speccer: one Opus call, ~65k input
  tokens (all cache-creation, zero cache read), 29k output, 284 s. Comparing
  g1's spec against the plan's own U1/U2 sections shows the output is ~90% a
  restatement: `name`/`summary` are compressed unit titles, `verification` is
  the plan's bullets with generated ids, and the only genuinely new content —
  relational framing ("these two tasks are one capability; g3 consumes X") —
  is derivable from `depends_on`, `implements/consumes`, and the group DAG.
- Getting the task map to partition cleanly took **15 invocations** of
  `group --no-spec`, each surfacing exactly one problem
  (`docs/todos/grouping_improvements.md`, causes C1–C6).
- The plan produced 28 groups because it actually contained ~3 unrelated
  sub-plans sharing one brainstorm session. Nothing in the pipeline says so;
  the operator discovers it by reading 28 group names.
- The granularity dial (`independent|balanced|monolithic`) exists but each
  level is a separate full pipeline run; nothing compares them or interprets
  the comparison.

Prior art in this repo: `--no-spec`/`--dry-run` already write to a `preview/`
dir (plan U8); the speccer already runs standalone from the Observatory
grouping tab (plan U31); the estimator already prices a file shared within a
group once; the Grouping Trace and edge-provenance artifacts already record
everything the new diagnostics need.

## Key Decisions

- **Group specs are assembled deterministically by default; the LLM speccer
  becomes opt-in.** The plan is the source of truth and the worker already
  receives it as shared context; the speccer's paraphrase adds cost, latency,
  and a hallucination surface, not information. Rejected: keeping a mandatory
  LLM speccer on a cheaper model (still a paraphrase), moving spec-writing
  wholesale into `/orchestrator-plan` (heavier planning sessions, stale specs
  on re-group).
- **Dry deterministic prose is acceptable.** Workers are LLMs; correct facts
  beat fluent prose. The relational header is generated from graph facts and
  can never drift from the partition. No Haiku polish pass for names/summaries.
- **The grouper advises; it never decides to split and the planner never
  rewrites plans.** `--advise` emits deterministic diagnostics; the final
  phase of `/orchestrator-plan` presents them and *asks* the user whether to
  split. The split itself (wave 2) is a mechanical partition of existing unit
  sections and task-map entries — near-zero new tokens. Rejected: an advisor
  LLM call (reintroduces the cost being removed), auto-splitting, the planner
  regenerating N plan documents.
- **Phase splits produce separate plan docs run serially by hand.** No
  cross-run dependency machinery; re-grouping phase B after phase A merges
  also picks up the real codebase. Rejected: phases-as-mega-slices inside one
  run, cross-plan `depends_on` in the manifest.
- **Deepening writes into the plan doc, per unit, from a standalone fresh
  session.** Enrichment survives re-grouping, splits, and resumes because
  spec assembly regenerates group specs from the plan for free. Subagents
  cannot ask the human questions, so the grilling lives in the interactive
  deepen session; forked/read-only explorers do the silent legwork.
  `/orchestrator-plan` ends by printing the deepen command as a reminder — it
  never runs it inline.
- **The plan is ingested in layers; no worker reads the whole document.**
  The plan markdown is the authoring format, not the worker context format:
  a deterministic parse splits it into a shared digest (preamble + tagged
  per-unit summary lines) that every worker gets, and full unit sections
  that travel only in the owning group's assembled spec. Cross-group needs
  are served contracts-only (from `implements/consumes`) — a worker that
  needs more has codegraph, the merged code once a dependency lands, and
  the surprise/contract channel. Rejected: full plan as shared context
  (every group pays for every other group's detail — and deepening would
  compound it), full sections of `depends_on` neighbors in the spec (start
  contracts-only).
- **The golden dataset waits for Infinity Skills.** Context-economy,
  execution-outcome, and estimator-calibration metrics exist to validate the
  labeled "root groupings", and the labels need per-agent read/action data
  that Infinity Skills ingestion will provide. Only requirements are written
  now; nothing is built.

## Requirements

### Wave 1a — deterministic spec assembly

- R1. `group` produces, with **zero LLM calls**, a launchable `groups.json`
  whose per-group `name`, `summary`, `spec`, and `verification` are assembled
  deterministically from the plan: name and summary derived from member unit
  titles (summary within the existing 120-char cap), spec = a generated
  relational header (member list, intra-group `depends_on` order, what other
  groups consume from / provide to this group, slice membership) followed by
  the member units' plan sections verbatim, verification = the units'
  verification bullets with generated stable ids (`<group_id>-<n>`).
- R2. The relational header states only facts present in the task graph and
  group DAG (`depends_on`, `implements/consumes`, slices); it is regenerated
  whenever the partition changes and can never disagree with it.
- R3. The LLM speccer survives as an explicit opt-in (CLI flag and the
  existing Observatory grouping-tab action), runs **after** assembly as an
  overlay on the assembled specs, never moves tasks, and uses a configurable
  model defaulting to Sonnet.
- R4. Assembled-spec grouping is the default path end to end: `run` launches
  from it, and the `preview/`-dir quarantine applies only to `--no-spec` /
  `--dry-run` invocations, whose meaning ("don't persist a grouping") is
  unchanged.

### Wave 1b — validation fixes (from `docs/todos/grouping_improvements.md`)

- R5. Every validation phase accumulates and reports **all** its failures
  before stopping: slice-overflow lists every over-cap slice, `plan_reader`
  reports all shape errors together then all reference errors together (C1).
- R6. A `group --price <plan>` mode parses the task map and prints per-task
  node work, per-slice sums against the cap, and the resolved budget
  parameters — no graph build, no codegraph, sub-second; the pricing formula
  and multipliers are documented in the task-map contract (C3).
- R7. "Node work" and "coder work" are named distinctly everywhere an
  operator sees a budget figure, and the slice-overflow error prints both
  with the multiplier stated (C2).
- R8. A degenerate-partition error reports how many offending edges are
  inferred vs declared, with provenance, e.g. "103 of 127 dependency edges
  are inferred from `symbols`" (C4.2).
- R9. `skills/orchestrator-plan/SKILL.md` and the task-map contract state
  that `symbols` is optional, contributes derived precedence, and that on a
  dense codebase omitting it may give a better partition (C4.3).
- R10. The slice-re-entrant dependency shape (a path leaving and re-entering
  a slice) is detected on the contracted graph and reported by name with the
  exact edit needed, instead of as a generic saturation error (C5).
- R11. Cycle repair withdraws *inferred* precedence edges (never declared
  ones) on the group DAG before resorting to a merge that can exceed the
  budget cap (C4.1).

### Wave 1c — advisory grouper

- R12. `group --advise <plan>` builds the task graph **once** and computes
  the partition at every granularity level from that cached graph, emitting a
  deterministic Advisory Report (JSON artifact + human-readable rendering)
  with per-preset group count, per-group budgets, cross-group edges, and
  critical path.
- R13. The Advisory Report includes plan-cohesion diagnostics computed
  without any LLM: weakly-connected components of the task graph (a
  multi-component or near-disconnected plan is flagged "this reads as N
  separate plans", naming the task sets), and a seriality signal (chain depth
  vs width / layering) that flags "this reads as serial phases", naming the
  candidate phase boundaries.
- R14. `--advise` is read-only in the sense of R4's preview semantics: it
  never overwrites a persisted grouping, and it completes in seconds on a
  re-run against an unchanged plan and index.
- R15. The final phase of `/orchestrator-plan` runs `--advise`, presents the
  diagnostics, and **asks** the user whether to split along the reported
  seams or proceed as one plan. It never splits silently and never rewrites
  the plan to split it (the mechanical split is R16, wave 2). It ends by
  printing the ready-to-run deepen command (R17) as a recommendation.

### Wave 2 — mechanical split and deepening

- R16. A mechanical plan split takes a chosen seam (a task→plan assignment
  from the Advisory Report) and partitions the existing plan doc into N plan
  docs by moving unit sections and their task-map entries verbatim — new
  content is limited to titles/frontmatter and a predecessor note on phase
  splits ("assumes plan A merged"). No LLM regeneration of unit prose.
- R17. `/orchestrator-deepen <plan>` is a standalone interactive skill run in
  a fresh session: it reads the plan (and the Advisory Report if present),
  spawns read-only explorer subagents per group/area to hunt edge cases and
  risks in the actual codebase, then grills the human with their findings,
  and writes the results as per-unit enrichment (edge cases, sharpened
  verification bullets) into the plan doc itself. Optional for every run.
- R18. The deepen skill's question policy and spec-enrichment template are
  grounded in the commissioned research (R19) before implementation — the
  skill asks few, high-yield questions rather than walking every unit.
- R19. Two Perplexity research reports are commissioned (launched
  2026-08-28 from the brainstorm session) and their findings folded into
  this doc's Open Questions before wave 2 is planned: (a) spec-deepening /
  acceptance-criteria practice for autonomous coding agents; (b) the
  CoCoder-line grouping literature and budget-constrained acyclic
  partitioning, checked against our measured failure modes (greedy first-fit
  fragmentation, SCC saturation, group-DAG cycles).

### Wave 1a (appended) — layered plan context

- R21. The `/orchestrator-plan` template gains a **tagged one-line summary
  per unit** (a dedicated field in each unit section, e.g. `Summary:`),
  written by the planner and deterministically parseable. The small
  redundancy with the Goal prose is intentional — it is the digest's raw
  material, so the digest is assembled by parsing, never by summarizing at
  group time.
- R22. Worker context is layered deterministically from the plan parse:
  **shared context** = base-context plus a plan digest (the plan preamble —
  objective, architecture decisions, cross-cutting constraints, the
  implements/consumes registry — and every unit's tagged summary line);
  **group spec** = the member units' full sections verbatim (per R1) plus,
  for each unit the group consumes from or provides to, the exchanged
  contract only — never the neighbor's full section. No worker receives the
  full plan document.
- R23. The shared block is byte-identical across all groups of a run and is
  placed first in every worker prompt, so parallel workers launched within
  the cache TTL hit the prompt cache on the shared prefix.

### Wave 3 — deferred (post-Infinity-Skills)

- R20. A grouping evaluation harness is specified but **not built** until
  Infinity Skills ingestion provides per-agent read/action data. Its purpose
  is to prove the Advisory Report recommends good groupings. Its metrics:
  context economy (predicted group files vs files each worker actually
  read), execution outcome (generations, rewrites, verdicts, merge success
  per group), and estimator calibration (estimated_tokens vs actual
  occupancy) — all three serving to validate agent/human-labeled "root
  groupings" harvested from past runs in both repos.

## Non-Goals

- No LLM call anywhere in the default `group` path.
- No automatic plan splitting — the user is always asked, and the planner
  never regenerates plan prose to effect a split.
- No cross-run dependency machinery (phase ordering is operational, by hand).
- No cross-group read-overlap-aware merging (pricing shared read context
  across candidate merges to co-group context-heavy but task-different
  units). Discussed and parked: it needs the wave-3 eval data to justify the
  added partitioner complexity. Revisit after R20.
- No LLM that moves tasks between groups, ever (unchanged invariant).
- No building of the eval harness before Infinity Skills lands.

## Research Findings (R19a — spec-deepening practice, received 2026-08-28)

The industry pattern (Amazon Kiro, GitHub Spec Kit, MetaGPT) validates both
wave-1 decisions: spec artifacts are **assembled, versioned documents with
slots** — the LLM's role is filling specific slots (edge cases,
clarifications), never rewriting the whole thing. Kiro's "Quick Spec" flow is
almost exactly the deepen design: pre-ask clarifying questions, then write
structured artifacts the coding agent executes. Benchmarks find the dominant
agent failure mode is **requirement omission and weak self-verification** —
the gap sharpened verification items close. Adoptable specifics for R17/R18:

- **Verification items become `Run: <command>` + `Pass: <observable condition>`** by convention (no schema change to `VerificationItem`) —
  turns the reviewer loop into an objective check. Deepen-generated commands
  must be grounded against the actual test tree by the explorer (and
  runnable in the group worktree env) or they burn reviewer rounds.
- **Three new per-unit plan slots deepen fills**: `Edge cases`
  (taxonomy-keyed one-liners, `N/A — <why>` allowed so absence is a
  decision), `Non-goals / must-not`, and EARS-style requirement lines in
  Goal ("When <trigger>, <unit> shall <response>" — adopt the pattern, no
  grammar validation tooling). `Files/Symbols/Depends-on` stay as-is.
- **Question policy** (ClarifyGPT divergence + EVPI ranking): the explorer
  drafts two plausible interpretations where the plan is ambiguous — if
  they'd produce different code, that's a question candidate; candidates are
  scored (blocking risk × effect size), hard-capped at **3–5 per group /
  ~10 per plan**, always offered with candidate answers ("either is fine" is
  recorded as a freed constraint). Every accepted answer must land as an
  EARS line, edge-case entry, or verification item — never loose prose.
- **Edge-case taxonomy for the explorer prompt**: boundary/range,
  empty/null/missing, error & partial-failure modes, concurrency/ordering,
  idempotency/retries, duplication/uniqueness, authz/security, performance
  budget, data invariants, contract compat/versioning.
- **Safety**: deepen appends into designated slots only — it must not
  perturb the YAML task map or unit ids (the fingerprint-drift bug class);
  stamp deepen output with the plan-content hash it was derived from.
- **Cheap lint independent of deepen**: a deterministic coverage check that
  every plan verification bullet maps to at least one `VerificationItem` in
  some group — candidate addition to wave 1a/1b.

## Research Findings (R19b — grouping literature, received 2026-08-28)

The CoCoder line has **no published fixes** for our measured failure modes —
our fork is already ahead of the source (we added the budget cap, cycle
detection, and always-on merge). The applicable fixes come from the classical
acyclic-DAG-partitioning literature (dagP; Moreira–Popp–Schulz acyclic /
evolutionary partitioning; Sarkar; acyclic hypergraph partitioning):

- **Greedy fragmentation** is not first-fit but a merge key whose
  `merged_work` term is a 5th-place tiebreaker with no fill/balance penalty
  (`partition.py:974`), so one group wins until it hits the cap. Cheap
  experiment first: promote a fill-ratio/variance penalty into the key
  (prefer merges landing in a target band, not at the cap) before any
  multilevel work. A dagP-style acyclicity-safe refinement pass (moving
  nodes back out of oversized groups) is the larger follow-up — the pipeline
  currently has no refinement stage at all.
- **SCC saturation from `symbols`**: model dense shared symbols as
  hyperedges/affinity (or route them through the existing hub machinery at
  graphing time) instead of projecting them into pairwise precedence edges.
  Semantics decision required: which symbol relations stay ordering
  constraints. Complements R8/R11.
- **Group-DAG cycles**: the robust pattern is maintaining a topological
  order as an invariant through every stage (all moves forward-compatible)
  rather than repairing cycles at the end. Any refinement pass must be built
  acyclicity-safe from day one.
- **Advisory metrics validated**: WCCs computed *before* Louvain with
  per-component metrics; per-preset score vector (group count, budget
  utilization mean/max, group-DAG depth, simulated makespan via the existing
  `_simulate_makespan`, edge cut, modularity/conductance) with
  Pareto-dominant options flagged; seriality index = critical path / max
  level width off the existing `_compute_waves`; a topological-order cut
  sweep whose valleys mark phase boundaries. Also: when modularity is low
  and every cut has high conductance, `--advise` should report
  "structurally monolithic" instead of partitioning anyway. These feed
  directly into R12–R13's implementation.
- Watch-outs: acyclic k-way partitioning is NP-complete (all fixes are
  heuristics; keep the sorted-iteration determinism discipline); Sarkar's
  makespan no-regression test is known to under-merge on wide graphs, which
  compounds with the fill pathology on `independent`; verify the CoCoder
  title/venue before citing it in docs (single-source).

## Open Questions

- Per-group vs plan-global question budget for deepening's grilling (the
  research recommends both caps; which dominates is a UX choice at R17
  planning time).
- Are `N/A — <why>` edge-case entries mandatory (completeness) or optional
  (token cost)? Token-optimality preference argues for taxonomy-keyed
  one-liners, not full blocks per unit.
- Does deepen propose verification *commands* (`Run:` lines) in v1, or only
  sharpen conditions? Higher value, but only safe with explorer grounding
  against the real test tree.
- Hyperedges vs hub-routing for symbol-derived edges (semantics decision, see
  findings above) — decide when planning R8/R11.
- Whether the merge-key fill-penalty experiment lands in wave 1c alongside
  `--advise` (it is small and directly reduces the 28-group symptom) or waits
  for the eval harness to measure it — leaning wave 1c, user to confirm at
  planning time.
- Exact thresholds for the cohesion diagnostics (what edge weight counts as
  "near-disconnected"; what depth/width ratio flags "phases") — to be tuned
  on the existing runs in both repos during wave-1c implementation.

## Next Step

Run `/orchestrator-plan docs/brainstorms/2026-08-28-grouper-speccer-flow-requirements.md`
(scope it to waves 1a–1c; waves 2–3 wait on R19's research reports).
