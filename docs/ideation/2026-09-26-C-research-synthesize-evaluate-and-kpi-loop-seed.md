---
title: "Brainstorm seed C — the next recipes (research, synthesize, evaluate) and the KPI-optimization loop"
date: 2026-09-26
status: seed (input to /orchestrator-brainstorm)
sources:
  - docs/brainstorms/2026-09-21-unit-recipes-requirements.md (R5–R7, R10, R14, R15, R17 — the "next increment")
  - docs/research/2026-09-21-kpi-optimization-prior-art.md (§8 checklist = starting positions)
  - docs/research/2026-09-21-agent-pipeline-prior-art.md (§4 decision record, §6 context failures)
  - docs/ideation/2026-09-15-B-agent-kinds-and-plan-checks.md (the candidate table)
  - docs/adr/0010 (agents never create nodes), docs/adr/0011 (Run Child re-adoption)
  - .orchestrator/notes-r20260924-134934.md, drummAI/.orchestrator/notes-r20260925-101742.md
  - memory: run-recipe-nested-llm-cost-unknown (recipe children are never costed)
---

# C — The next recipes and the KPI loop

## Where the orchestrator stands (2026-09-26)

Shipped and live-validated twice (r20260924 on smart_mcps, r20260925 on
drummAI): the recipe registry (`orchestrator/recipes/`), `code` + `run`,
the dispatcher, task-map v2 (`recipe` / `recipe_args`), singleton non-code
groups priced by the recipe, the Artifact Manifest with downstream injection
of non-code entries only, Run Children (Landlock-confined, re-adopted on
resume, one-shot `run_triage` on failure), and the 0.20.0 visibility fixes
(runner session role, synthetic run report, "cost unknown (recipe child)").
Branch `fix/run-child-confinement-r20260925` (3 commits, pushed, **no PR
yet**) carries the last of it; `main` is at PR #12 / 0.19.0.

Deliberately not done: any $ figure for `run` (decision: recipe children are
never costed, reports say "unknown"); F5/F7 cosmetics; plan-skill checks
P3/P6/P7/P8 (`docs/todos/plan-skill-checks-p3-p6-p7-p8.md`); Observatory
surface for the manifest.

What the registry can express today, and what the next recipes will need:

| dimension        | today (`UnitRecipe`)                              | research / synthesize / evaluate need                                  |
| ---------------- | ------------------------------------------------- | ---------------------------------------------------------------------- |
| prompt           | `coder` (code); none (run)                        | a role prompt each; per-project override? (open, Q18)                  |
| tools            | one shared `DEFAULT_ALLOWED_TOOLS`, no web tools  | research: web/Perplexity/NotebookLM; synthesize: repo read only         |
| contract         | `CoderReport`, `RunRecord`                        | findings-with-sources; decision record; measurement record             |
| reviewer prompt  | `reviewer` / none                                 | source-checker; consistency-with-inputs; "did this cheat" (KPI)        |
| merge            | `code_ladder` / `run_commit_paths`                | docs-commit through Preflight (= commit_paths over `docs/research/`)   |
| pricing          | file arithmetic / wall-clock + triage allowance   | size class (R12); evaluate = run pricing                               |
| handoff prompt   | `handoff` / none                                  | found / exhausted / open (R10)                                         |
| session roles    | base, coder, reviewer, runner                     | researcher, synthesizer (+ evaluator?) — additive on the bundle (R17)  |
| loop policy      | rounds / rewrites / generations, cap → human      | keep-or-revert decided by the orchestrator between two recipes         |

## The first use case, as read from the human's brief (to be confirmed — Q1)

Summarise orchestrator sessions (Infinity Skills already ingests the Run
Bundle: `infskills ingest-run`), extract recurring patterns, and use them to
improve the smart_mcps skills/prompts — a pipeline of research → (maybe)
synthesize → code → evaluate, human-planned per ADR 0010, then a KPI loop
over a measured quality metric. Two facts about that target matter for the
design: Infinity Skills' key metrics are **comparative LLM-as-judge**
(pairwise better/worse, human-validated), not a scalar from a deterministic
script; and drummAI already produces scalar JSON measurements
(`data/cache/eval/repair_eval.json`: hat accuracy, false-repair rate) that a
`run` unit registers today — a ready-made first KPI target.

## Questions for the brainstorm

Legend: **stakes** = what changes in the plan depending on the answer;
**default** = the position the seed carries in if unanswered.

### A. Scope and sequencing

- **Q1 · first-use-case** — Which repo hosts the first live pipeline
  (infinity-skills, smart_mcps, drummAI), and which units are research /
  synthesize / code / run / evaluate? Stakes: which recipes must ship first
  and what the driver's checklist verifies. Default: drummAI for the KPI
  loop (scalar metrics exist), infinity-skills for research→code.
- **Q2 · one-brainstorm-or-two** — The requirements doc split the KPI loop
  into its own brainstorm because accept criteria (binary gate vs
  statistical comparison) and trust boundaries (immutable harness) differ.
  One requirements doc with two tracks, or two docs? Stakes: plan size and
  whether `evaluate` blocks `research`. Default: one brainstorm, two
  documents, `research` increment first.
- **Q3 · synthesize-at-all** — Across five learning_podcast runs `research`
  was needed once and `synthesize` never; ADR 0010 already says "a research
  unit writes candidates into its artifact; a human re-plans". Is synthesize
  needed for the first use case, and who consumes its decision record — a
  downstream coder or the human? Stakes: R6/R7 now vs deferred. Default:
  defer unless a downstream coder consumes it.

### B. `research`

- **Q4 · tool-surface** — Perplexity MCP (`smart-mcps-perplexity`, key present
  locally; unreachable only from the remote container), built-in
  WebSearch/WebFetch, NotebookLM (`nlm`), codegraph for grounding. Which are
  allowed, in what preference order, and is R5's "fallback is recorded, never
  silent" kept? Stakes: allowlist + `[recipes]` config + cost. Default:
  Perplexity first, WebSearch/WebFetch fallback recorded, NotebookLM opt-in
  per unit.
- **Q5 · session-or-one-shot** — A `claude` worker session with generations
  and a source-checking reviewer (like `code`), or a `run`-like one-shot the
  orchestrator owns (the `perplexity-explorer` agent brief, or
  `plan-to-plan` → `apply-research-plan`'s research_plan/answers contract)?
  Stakes: reuse of three existing research skills vs a new prompt;
  observability; nested-cost rule. Default: worker session; reuse the
  research_answers contract as the artifact schema.
- **Q6 · findings-contract** — Findings under `docs/research/`; every claim
  with source, confidence, freshness (pipeline research §4)? Is "every claim
  carries a source" checked by schema (URL present) or by the reviewer?
  Stakes: R13 mechanical vs R14 reviewer. Default: schema requires ≥1 source
  per finding; reviewer checks support.
- **Q7 · research-inputs** — Does a research unit receive upstream manifest
  entries (e.g. "why is `hat_after` flat?") and codegraph grounding, or only
  the plan's question? Stakes: injection already exists for non-code
  upstreams; grounding is what makes Perplexity answers specific. Default:
  both.
- **Q8 · multi-unit-groups** — Non-code units are singleton groups today. Three
  research questions = three sessions; acceptable, or lift the singleton rule
  for document recipes? Stakes: partitioner change. Default: keep singletons.

### C. `synthesize` (only if Q3 says yes)

- **Q9 · decision-record** — Adopt the §4 decision record as-is (decision,
  rationale→evidence refs, alternatives_rejected, implementation_contract,
  open_questions)? Stakes: contract + reviewer prompt. Default: yes.
- **Q10 · refine-not-create** — May `implementation_contract` *refine* an
  existing downstream code unit's spec (a speccer-rewrite input) without
  creating nodes? Stakes: the ADR 0010 boundary; this is where synthesize
  earns its keep. Default: yes, as a rewrite of the declared consumer only.
- **Q11 · repo-read** — No web (R6); may it read the repo/codegraph beyond its
  declared artifacts? Default: yes, read-only.

### D. `evaluate` and the KPI loop

- **Q12 · metric-shape** — First KPI: a scalar from a deterministic script
  (research assumption, drummAI shape) or a comparative LLM judge (Infinity
  Skills shape)? Stakes: the whole accept rule; a judge is itself an
  uncosted LLM child and needs pairing. Default: scalar first; judge later
  as a paired score with a declared noise floor.
- **Q13 · loop-locus** — Where does "repeat" live: (a) one `optimize` group
  whose rounds are coder-change → evaluate child → orchestrator keep/revert,
  or (b) plan-level `code → evaluate → code…` unrolled by the human? Stakes:
  (a) is a new executor with an inner Run Child; (b) is recipes + policy
  only. Default: (a), bounded by the existing round/generation caps.
- **Q14 · who-decides** — Orchestrator compares numbers, never the coder
  (research §5); four-valued outcome (promising/keep/discard/inconclusive),
  `promising → keep` gated on one confirmation run, noise floor = MAD over the
  last ~5 (zero extra evaluations). Confirm? Stakes: cheap insurance vs
  autoresearch's seed-change failure. Default: all three.
- **Q15 · mutable-region-and-harness** — The unit's `files:` bounds edits; the
  harness (eval script, held-out data, seed, budget-enforcing line) lives in
  protected paths hash-checked before each scoring run; evaluate runs as a
  Run Child (already a separate, confined process). Which protections are v1?
  Stakes: the CUDA-Engineer / issue #466 class of failure. Default: files
  bound + hash-checked harness denylist + held-out set; reviewer gets the
  "did this cheat" job.
- **Q16 · budget-dials** — Evaluation-count cap, fixed wall-clock per
  evaluation, patience (3–5), consecutive-revert cap, good-enough threshold,
  plateau → escalate not stop. Plan-declared or config? Stakes: cost;
  "no improvement, here is what was ruled out" must count as success. Default:
  plan declares count + wall-clock + threshold; config holds patience/revert
  caps.
- **Q17 · ledger** — Per attempt: commit, metric, delta, noise floor, outcome,
  one line why, what was tried — in the manifest as one entry per attempt, or
  a dedicated ledger file that also rides the Run Bundle (Infinity Skills
  ingests it)? Default: dedicated `ledger.json` per group + one manifest entry
  for the champion.

### E. Flexibility and validation

- **Q18 · per-project-prompts** — "There is a specific prompt for each one":
  are role prompts fixed in the plugin, or overridable per project
  (`.orchestrator/prompts/<recipe>.md`) with the machine shared? Stakes: the
  registry's `custom` mapping vs a prompt-resolution rule. Default:
  plugin default + per-project override, recorded in the run.
- **Q19 · cost-later** — Cost estimation stays out; is "N × declared
  wall-clock, LLM children unknown" acceptable for the first KPI loop, or is
  a $ cap a prerequisite there (the r0913 $70–160 render sat unanswered
  overnight)? Default: acceptable; hard count cap stands in.
- **Q20 · live-checklist** — Define the driver's verifiable checks for each
  new recipe at brainstorm time (as the 12-check list did for `run`)?
  Default: yes, one list per recipe in the requirements doc.

## Suggested brainstorm order

1. Q1–Q3 (scope), then B (research) — it is the one recipe with evidence.
2. D (evaluate + loop) against a concrete drummAI metric.
3. C only if Q3 survives; E last.
