# b3quant

A historical-only trading research pipeline on B3 futures (WIN, WDO) and the
most liquid IBOV stocks, run by the smart-mcps orchestrator: a content-hashed
data lake, a conservative cost model (B3 fees plus two ticks of slippage per
side), a walk-forward evaluation harness nobody can cheat, classical
baselines, one research track per idea, and optimize loops that keep or revert
from numbers. The deliverable of the first loop is the ground and an Attempt
Ledger of what was ruled out, not a profit.

## Non-goals, permanently

- **No live or paper trading, no order routing, no broker API.** No order ever
  leaves the machine.
- **No LLM trader.** The LLM researches, codes and reviews; the orchestrator
  decides keep-or-revert from the harness; a human promotes.
- **No market-impact modelling** beyond fixed slippage; sizes are assumed
  small enough that the book is unmoved.

## Where things are

- `docs/brainstorms/` — the requirements this repo is built from (R1–R33).
- `docs/research/inbox/` — the nine Perplexity answers the research units
  start from and verify; `docs/research/` — their Findings Artifacts.
- `CONTEXT.md` — the domain glossary (opinionated, project-specific terms).
- `.orchestrator/config.toml` — committed orchestrator config (recipes,
  shared data dirs); `data/` — the lake, never committed, shared into every
  worker worktree.

## Secrets

`PERPLEXITY_API_KEY` is read from the environment by the research units'
workers, which inherit the run-driver's shell. Copy `.envrc.example` to
`.envrc` (gitignored) locally, or set it as an environment secret on a cloud
container. Never commit it.
