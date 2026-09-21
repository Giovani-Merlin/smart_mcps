# Handoff: Unit Recipes brainstorm + prior-art research (2026-09-21)

Session that turned seed B into a requirements document, and recorded the
prior-art research behind it so it never has to be run again.

## What exists now

| Document                                                   | What it is                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docs/brainstorms/2026-09-21-unit-recipes-requirements.md` | **The deliverable.** 18 R-IDs, 15 decisions, 9 non-goals. Ready for `/orchestrator-plan`.                                                                                                                                                                      |
| `docs/research/2026-09-21-agent-pipeline-prior-art.md`     | Typed node registries, AOrchestra, context/artifact passing, dynamic graphs, cost gates, failure taxonomies, per-project watch-outs. Input to the brainstorm above.                                                                                            |
| `docs/research/2026-09-21-kpi-optimization-prior-art.md`   | The construction-vs-optimization loop: FunSearch → AlphaEvolve → ShinkaEvolve → GEPA → AIDE → MLE-STAR → DGM/MGM, Karpathy's `autoresearch` read from source, the 2026 reward-hacking literature, small-budget statistics. **Input to the *next* brainstorm.** |
| `CONTEXT.md`                                               | Gained **Unit Recipe** and **Artifact Manifest**, each with rejected synonyms under `_Avoid_`.                                                                                                                                                                 |

## The chain this sits in

```
docs/ideation/2026-09-15-A-…   → brainstorm 2026-09-16 → plan 2026-09-16-001 → PR #9 (merged, v0.18.0)
docs/ideation/2026-09-15-B-…   → brainstorm 2026-09-21 (THIS)              → /orchestrator-plan next
```

Seed B was split. This brainstorm took the agent-kinds half. Two pieces were
deliberately left out and still need their own work:

- **The KPI-optimization loop** — its own brainstorm, with
  `docs/research/2026-09-21-kpi-optimization-prior-art.md` as the input.
- **Seed B's P3 / P6 / P7 / P8** planning-skill checks — a separate small plan.
  They do not depend on recipes.

## The design in one paragraph

A plan unit may declare a **Unit Recipe** — a named bundle carrying prompt,
tool allowlist, completion contract, reviewer prompt, merge behaviour, pricing
function and handoff prompt. Three ship: `code` (today's machine, unchanged),
`research`, `synthesize`. Groups are single-recipe. Each recipe prices its own
units, so non-code work stops pricing at zero. Verification is a schema
validated on every unit, plus the existing `ReviewIntensity` dial. Merge is
per-recipe. A run-level **Artifact Manifest** carries what each unit produced so
a downstream unit gets an entry and a bounded summary, not a payload. Opt-in
twice: no recipe declared behaves exactly as today, and any recipe requires a
config flag or the plan fails loudly.

## The standing principle, stated because it constrains everything

> **The pipeline is human-planned. Agents never create nodes.**
>
> A unit repeats until it completes or hits a declared cap — rounds, rewrites,
> generations — and a cap is a Work Failure, terminal by design so a human acts.
> No unit may create, spawn or expand another unit. A research unit that finds
> three candidate strategies writes them into its artifact; a human re-plans.

This is a design principle, not a v1 deferral. It makes all seven documented
failure modes of LLM-generated plans moot rather than managed, and it is why
the Artifact Manifest matters so much — with no automatic expansion, it is the
only handoff channel that needs to work.

## The five findings most worth remembering

1. **Nobody has built this.** Two independent surveys over largely disjoint
   system sets agreed. Three things were found nowhere: **per-recipe merge
   behaviour**, **a machine-readable cost estimate gating an LLM step**, and
   **review mode as a per-type field**. Everything else exists somewhere.
2. **AOrchestra (ICML 2026, arXiv 2602.03786) is the counter-thesis** —
   dynamically synthesized (Model, Task, Tools, Context) sub-agents beat
   hand-built rosters by 16.28% on GAIA/SWE-Bench/Terminal-Bench. Our answer:
   **the recipe defines the envelope and the policy; the unit fills in the
   four-tuple within it.**
3. **Karpathy's `autoresearch` has a ~3.1% hit rate** — ~20 improvements from
   ~650 experiments. At 30 evaluations, **P(zero hits) ≈ 40%**. Plan for one
   hit, not twenty, and treat "nothing found, here is what was ruled out" as
   success. Its naive `lower → keep` rule **demonstrably accepted a random-seed
   change as the final improvement on its own README graph** (issue #466).
4. **Prompting is not a reward-hacking mitigation** — BAITBENCH measured ~6.2pp.
   Structural controls are: bound the mutable region, separate the evaluator,
   diff-gate the harness, hold out a test set, and pick production-aligned
   models (0% vs 13.9% exploit rate in RHB).
5. **Platform fan-out limits exist to protect schedulers, not budgets.** Four
   quantities need capping — N, concurrency, depth/replans, cumulative cost —
   and concurrency bounds burst spend, never eventual cost.

## Two facts about this repository the research surfaced

- **The estimator is entirely file-based** (`grouping/estimator.py:55`), so a
  unit with no files prices at ~zero. That is seed B's P1, which cost a real run
  272,048 tokens. R12 fixes it.
- **Landlock governs writes, not reads or network** (`execution/confinement.py:77-82`).
  A research worker can reach the web with no confinement change; the one thing
  genuinely blocked is a nested `claude`, because it cannot write its transcript.

## The main risk in the plan

**R9's extraction.** Round accounting, heartbeat/Sign of Life, escalation and
manifest updates must come out of the 1,993-line `execution/review.py` into a
layer every executor shares, *before* R8's dispatcher exists — or the new
executors start life without liveness parity, reintroducing exactly the bug the
stall-detection work just fixed. It touches the file we least want to
destabilise and must leave `code`-recipe behaviour identical.

## Environment notes for whoever picks this up

- **`api.perplexity.ai` is blocked by this environment's network policy** (403
  on CONNECT). The key is not the problem. `WebSearch`/`WebFetch` work and were
  used instead. Fixing it means changing the environment's network policy, not
  supplying a credential.
- **`.orchestrator/` does not survive here.** `CLAUDE.md` says to keep working
  notes there because it survives a Claude Code *process* restart — true on a
  laptop. This session ran in a **remote container that is reclaimed on
  inactivity**, and `.orchestrator/` is gitignored. That is why the research was
  promoted to `docs/research/` rather than left where the convention says.

## Next step

```
/orchestrator-plan docs/brainstorms/2026-09-21-unit-recipes-requirements.md
```
