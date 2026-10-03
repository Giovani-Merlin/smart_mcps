# Handoff — b3quant loop 1 plan written, not yet deepened or run

**Date:** 2026-10-03
**State:** `docs/plans/2026-10-03-001-feat-b3quant-loop1-free-data-plan.md`
is written, verified and validated (plan-check consistent; `group --no-spec`
gives 21 groups with every recipe unit isolated; `--advise` reports no
cohesion seam; `--dry-run` shows the task map parsed and the mapper skipped).
Nothing has been deepened or run.

## What exists

- `docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md`
  — revision 2 of the requirements (R1–R33), rewritten after the nine
  Perplexity briefs were run from the cloud container.
- `docs/research/inbox/B1…B9-*.md` — the raw Perplexity answers (five
  `research`, four `reason`, all `--context-size high`); the briefs are in
  `docs/research/2026-10-02-b3quant-perplexity-briefs.md`.
- `CONTEXT.md` — the glossary ("Order Book", not "book").
- `.orchestrator/config.toml` — committed on purpose: recipes enabled,
  `data_dirs = ["data"]`, escalation on.
- The plan above: 30 units, task map v2.

## Decisions taken with the human (2026-10-02/03)

- `b3quant/` lives inside smart_mcps until the human splits it into its own
  repository (the cloud session could not create one). Commands run from
  inside `b3quant/` with `uv run --project .. smart-mcps-orchestrate …`;
  after the split, drop `--project ..` and install the plugin.
- Two plans, split at the order-book boundary: plan 1 (this one) runs on
  free data; plan 2 (R5 book-ingest, R26 book-signals, the deep LOB
  candidate of R25, R30 combiner) is written after `U9. book-data-research`
  returns quotes and the human decides the purchase.
- The MT5 export (`scripts/mt5_export.py`, unit U2) runs on the human's
  Windows machine before the big run; the files are copied into
  `data/raw/mt5/` on the WSL2 side.
- The run executes on the same Windows box under WSL2 with the GPU passed
  through.

## Next actions, in order

1. **Split the repo** when convenient (`git subtree split -P b3quant` or a
   fresh repo seeded from this folder), install smart_mcps as the plugin,
   `uv tool install` the orchestrator, and `codegraph init && codegraph index`.
2. **Set the Perplexity key** in the WSL2 shell that will drive the run:
   `cp .envrc.example .envrc`, fill `PERPLEXITY_API_KEY`, `direnv allow` (or
   `export` it). Workers inherit it. Never commit it.
3. **Deepen the plan**: `/orchestrator-deepen docs/plans/2026-10-03-001-feat-b3quant-loop1-free-data-plan.md`
   — edge cases, non-goals and `Run:`/`Pass:` splits per group; its sandbox
   sweep re-checks every `Run:` line against the allowlist.
4. **Run the first group only** (U1–U4 land together as g1): it ships the
   export script. Then run `scripts/mt5_export.py` on Windows per
   `docs/runbooks/mt5-export.md`, copy the output to `data/raw/mt5/`, and
   resume. The research groups (g2–g9, g21) need no data and can run in the
   same window.
5. **Drive the run** with `/orchestrator-run docs/plans/2026-10-03-001-feat-b3quant-loop1-free-data-plan.md`.
   Driver items to expect: `PERPLEXITY_API_KEY` present; the MT5 export's
   `export.json`; the fee table against B3's page; the held-out evaluations
   with `B3QUANT_HOLDOUT=1`; the ledgers' harness hashes.
6. **Plan 2** once `docs/research/r4-book-data-findings.md` exists and the
   purchase is decided: `/orchestrator-plan` with the brainstorm plus that
   artifact as origin.

## Open items

- Which broker's MT5, and how far back its WIN/WDO history reaches — known
  after the export.
- Whether B3's public `NEGOCIOS*` trade files are still published (U9).
- The verifier's advisory findings, if any were declined, are listed at the
  bottom of the plan's commit message history (`git log -- docs/plans`).
