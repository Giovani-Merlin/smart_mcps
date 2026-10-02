---
date: 2026-10-02
topic: b3quant-trading-research-pipeline
---

# b3quant — a B3 trading research pipeline on the orchestrator — Requirements

> **Where this document lives.** The project is a **new repository** (working
> name `b3quant`), the first external consumer of the `research`, `evaluate`
> and `optimize` Unit Recipes. This brainstorm was written inside smart_mcps
> because it was the only repo reachable from the cloud session; when the new
> repo is created, copy this file to its `docs/brainstorms/` and
> `2026-10-02-b3quant-CONTEXT.md` to its root as `CONTEXT.md`, then run
> `/orchestrator-plan` there. The Perplexity briefs the research units start
> from are in `docs/research/2026-10-02-b3quant-perplexity-briefs.md`.

## Summary

Build the *ground* for a small-scale, non-market-moving trading research
programme on B3 futures (WIN, WDO) and the most liquid IBOV stocks: a
content-hashed data lake fed by free sources plus one bounded purchase of
historical order-book data, a cost model that charges B3 fees and two ticks
of slippage per side, a walk-forward Evaluation Harness with purged folds and
a held-out year, two KPI Contracts (forecast skill for model loops, net Sharpe
for strategy loops), classical baselines (support/resistance breakout, pairs
and futures spreads) scored on that harness, and one `research` unit per track
(swing forecasting, intraday forecasting, book microstructure and big-player
imprints, pairs, chart patterns, reinforcement learning, news, backtest
engine, data sources) whose Findings Artifact refines the coder unit it feeds.
Model and strategy candidates are then improved by `optimize` loops of twenty
evaluations each, and the run ends with a go/no-go per track and an Attempt
Ledger of what was ruled out. The first run is historical only; no order ever
leaves the machine. Reinforcement learning is gated behind a forecast model
that beats the classical baseline; an LLM never trades.

## Problem Frame

The human day-traded WIN and WDO and knows the Brazilian book is unusually
open: B3 publishes full-depth market data and its times-and-trades is broker-
tagged, which is the raw material for the "follow the big player" and
iceberg-detection edges that retail platforms sell as black boxes. The
hypothesis is that with real data, honest costs and a harness nobody can
cheat, a mix of forecasting models, classical strategies and (later) a
learned sizing/entry policy can "surf" price at a scale that moves nothing.

What fails without this: research done in notebooks has no record of what was
tried, in-sample results get kept, costs get forgotten, and the "AI trading
system" literature is full of exactly those failures. The orchestrator now
has the machinery to prevent them — `research` writes sourced findings,
`evaluate` scores inside a hash-checked harness the optimizer cannot touch,
`optimize` lets the orchestrator decide keep-or-revert from numbers, and the
Attempt Ledger records every ruled-out idea
(`docs/brainstorms/2026-09-27-research-evaluate-optimize-requirements.md`,
validated in run `r20260927-100604`). Nothing in it is finance-specific; this
project is the first domain where the harness protects *money* rather than a
grouping metric.

Prior art already in this repo that constrains the design:
`docs/research/2026-09-21-kpi-optimization-prior-art.md` (a working loop at
twenty evaluations finds nothing half the time; a zero-hit loop with a full
ledger is a success), ADR 0010 (humans plan the pipeline, agents never create
nodes), and the harness contract in the task-map docs (harness paths are
hashed, inputs are frozen).

Facts established from the web during this brainstorm, to be confirmed by
the data-sources research unit before anything depends on them:

- B3's free historical products are daily (COTAHIST since 1986) and the daily
  bulletins; intraday trades and offers are sold through UP2DATA ON DEMAND
  and DATAWISE, price unknown from the container (b3.com.br is egress-blocked
  here).
- MetaTrader 5 through a Brazilian broker exports WIN/WDO M1 bars and ticks
  for roughly five years back; forum users report 2020-07 to 2025-05 at M1.
- The LOB deep-learning line (DeepLOB 2019 → TLOB, LiT, LOBFrame 2024-25,
  LOBERT 2025, scaling-law study 2026) is benchmarked on NASDAQ stocks, not
  futures; transfer to WIN/WDO is an open question.
- Off-the-shelf time-series foundation models (Chronos-2, TimesFM-2.5,
  Moirai-2) win generic benchmarks but underperform gradient boosting on
  daily excess returns with negative out-of-sample R² in at least one 2026
  study; the intraday case is unmeasured.
- NautilusTrader is the one open-source engine with order-book replay, queue
  position and partial fills; vectorbt-class tools are the fast path for
  signal research.

## Key Decisions

- **A new repository, `b3quant`.** Runs would otherwise drag the plugin's
  test suite, Preflight and git history along. smart_mcps is installed as the
  plugin; this document and the glossary seed are copied over. Rejected: a
  subfolder of smart_mcps.

- **The first loop delivers the ground, not a profit.** Data, costs, harness,
  baselines, findings and a ledger. A strategy clearing a Sharpe bar is a
  bonus, never the acceptance criterion — the ledger of twenty ruled-out
  ideas per track is what the second loop plans from. Rejected: "one strategy
  over threshold" (puts the optimizer in charge of defining profitable).

- **Two KPI Contracts, two kinds of loop.** Model loops optimise *forecast
  skill* against a naive baseline with a net-P&L guard (a fixed rule mapping
  the forecast to positions may not lose money versus the baseline rule).
  Strategy loops optimise *net walk-forward Sharpe* with forecast skill as a
  guard when a model feeds them. The human's instinct was a mix of both;
  this is that mix with each loop owning one number, as the KPI Contract
  requires. Whether a direct action label (triple-barrier / meta-labelling)
  beats a return forecast is a loop-1 research question, not a decision.
  Rejected: net Sharpe only (model loops would chase execution noise);
  forecast skill only (blind to costs).

- **Costs are conservative: B3 fees from an ingested table plus two ticks of
  slippage per side, always.** The human chose the harsher of the offered
  models. A one-tick variant is reported as an observation so the gap is
  visible, never used to keep a candidate. Rejected: one tick (the human's
  own experience says fills are worse than that for a retail account).

- **Instrument universe: WIN and WDO traded, the ~20 most liquid IBOV stocks
  traded for pairs, everything else a feature.** USD/BRL spot, DI futures,
  ES/NQ, DXY, Brent and iron-ore proxies, the macro calendar and B3 regime
  flags are covariates only. Rejected: WIN+WDO only (no equity pairs); the
  full IBOV (heaviest ingestion for a loop whose point is the pipeline).

- **Free data first, one bounded book purchase.** MT5 ticks/M1, COTAHIST and
  whatever B3 still gives away are ingested first; historical offers/book
  data is bought for a chosen window only after the data-sources research
  unit prices it, and the purchase is a human decision raised as an
  escalation, bounded at roughly R$ 2,000. Rejected: free only (the book
  track is the human's main edge and would be starved); pay whatever it costs.

- **Split backtest stack.** Vectorised research (polars, vectorbt-class
  tooling) for forecast skill and swing strategies; NautilusTrader for the
  intraday strategy harness where fill realism decides. A research unit
  confirms Nautilus can replay B3 trades and book before any coder depends
  on it; if it cannot without an adapter larger than a thin engine, the
  refinement switches the intraday harness to a thin in-house replayer.
  Rejected: Nautilus everywhere (every worker pays the learning cost); own
  engine everywhere (fills we invent are fills we cannot trust).

- **Compute is one consumer GPU and about a terabyte.** Tick-level book work
  for months, M1 for years, DeepLOB-class training feasible but budgeted per
  evaluation. No cloud GPU in loop 1.

- **Reinforcement learning is gated.** Loop 1 ships a research unit and a
  gym-style environment over the harness; RL training (sizing, entry/exit
  policy fed by forecast signals) starts only when a forecast model beats the
  classical baseline on the harness. Rejected: an RL track from loop 1
  (compute and noise-chasing); deferring entirely (the environment is cheap
  and the research is needed to plan loop 2).

- **Macro calendar in, news NLP out.** Copom, Fed, IPCA, payroll and auction
  dates are features; a research unit maps Brazilian news sources, licences
  and costs; no scraping or LLM feature extraction in loop 1. Rejected: news
  NLP now (a licensing and cost problem on top of a data problem).

- **Classical scope: breakout and pairs coded, chart patterns researched.**
  Support/resistance breakout and pairs/spread trading (stock pairs, WIN-WDO-
  DOL-DI relations) are coded, scored baselines; head-and-shoulders and other
  chart patterns get a research unit (kernel-regression detection, Lo-
  Mamaysky-Wang lineage) and a go/no-go, no coder in loop 1. Rejected:
  coding all three (rounds spent on the weakest evidence); dropping patterns.

- **Loop-1 budget: about two weeks unattended, twenty evaluations per
  optimize loop.** The recipe's validated dials (patience 4, three
  consecutive reverts escalate); usage-limit pauses resume on their own.

- **No live trading, no LLM trader, no market impact.** Live and paper trading
  are two or three loops away; the LLM's job is research, code and review,
  never a position; order sizes are assumed small enough that the book is
  unmoved, and the harness never models impact beyond slippage.

- **Everything the harness reads is frozen and hashed.** Datasets carry a
  manifest with source, licence, date range and sha256; fold definitions,
  seeds, the fee table and the held-out period are harness paths; an
  optimizer's mutable region never includes them.

## Requirements

### Repo and data ground

- R1. `repo-skeleton` — **A `b3quant` repository the orchestrator can run.**
  Python managed by `uv`, `polars` for data, `pytest` plus `ruff` as the fast
  checks Preflight runs, the smart_mcps plugin installed with `research`,
  `evaluate` and `optimize` enabled, `[workspace] data_dirs` pointing at the
  data lake and the evaluation output directory, this document under
  `docs/brainstorms/`, the glossary seed as `CONTEXT.md`, and a `README`
  that states the non-goals (no live trading, no LLM trader).
- R2. `data-lake` — **A content-hashed parquet lake with a dataset
  manifest.** Every dataset lives under `data/<source>/<instrument>/ <granularity>/` as parquet with UTC timestamps (session times are
  America/Sao_Paulo and stored as UTC plus a session-date column), and is
  listed in `data/MANIFEST.json` with source, licence, instrument, granularity,
  date range, row count and sha256. A dataset absent from the manifest does
  not exist for the harness.
- R3. `free-ingest` — **Ingest the free sources into the lake.** MT5 M1 bars
  and ticks for WIN and WDO (the export runs on the human's machine through
  their broker's MT5 and is a driver-run item), COTAHIST daily for the stock
  universe, B3 daily bulletins where still free, and for each the parser,
  the schema test and a manifest entry. WIN and WDO are also published as
  continuous contracts with the roll rule (volume-based, roll date recorded)
  and a back-adjustment flag; raw per-contract series are kept.
- R4. `book-data-research` — **A research unit prices historical B3 offers
  and book data and refines the book-ingest unit.** Questions: what UP2DATA
  ON DEMAND, DATAWISE and third-party vendors sell (offers-level, L2
  snapshots, trades with broker codes), formats, price per month per
  instrument, minimum window, licence terms for research use; whether
  historical times-and-trades carries buyer/seller broker codes; what a six-
  to twelve-month WIN+WDO window costs. The Findings Artifact ends in a
  purchase recommendation under the R$ 2,000 bound and a Spec Refinement of
  R5 naming the format to parse. The purchase itself is an escalation the
  human answers.
- R5. `book-ingest` — **Ingest the purchased or otherwise obtained book data
  into an event-level schema both stacks read.** Order events (add, modify,
  cancel, trade) with instrument, timestamp, side, price, quantity, order id,
  and broker code when present; an L2 snapshot reconstruction at a declared
  depth and cadence; schema tests that replay a day and check the
  reconstructed top of book against the trade tape. If no book data is
  obtained, the unit lands with the schema and tests against the MT5 tick
  stream and reports the gap in its manifest entry.
- R6. `exogenous-features` — **A point-in-time feature store for covariates.**
  USD/BRL spot, DI futures curve points, ES/NQ, DXY, Brent and an iron-ore
  proxy at daily and, where free, M1; the macro calendar (Copom, FOMC, IPCA,
  payroll, Treasury auctions, B3 holidays and auction/circuit-breaker flags)
  as event features with the publication time, never the reference time.
  Every feature carries the timestamp it became known at, and a test proves
  no feature at time t uses data published after t.
- R7. `cost-model` — **B3 fees from an ingested table plus two ticks per side.**
  Emoluments, registration and settlement fees per contract and per share
  from a versioned fee table in the harness; brokerage zero (discount
  broker); slippage two ticks per side on WIN and WDO, two ticks on stocks;
  a one-tick variant computed and reported as an observation. The fee table
  and slippage constants are harness paths.

### Evaluation Harness

- R8. `harness-protected` — **One harness directory, hash-checked, never in
  an optimizer's files.** `eval/` holds the scoring CLI, fold definitions,
  seeds, the fee table, the held-out period boundary and the leakage probes;
  every `evaluate` child records its hash; the plan's optimize units exclude
  it from `files:`; the plan parser refuses an overlap (R12 of the recipes
  brainstorm, applied).
- R9. `walk-forward-folds` — **Purged, embargoed walk-forward folds and a
  held-out final year.** Folds roll forward in time with a purge gap at least
  the longest label horizon and an embargo after each test window; the last
  twelve months of every dataset are held out and scored only by a driver-run
  final evaluation, never by an optimize loop. Fold boundaries are a harness
  file keyed by dataset hash.
- R10. `forecast-kpi` — **The model KPI Contract: forecast skill against a
  naive baseline.** Objective: out-of-sample skill score (one minus the ratio
  of the model's loss to the naive loss, where the loss is pinball loss for
  return quantiles or log-loss for triple-barrier classes, declared per
  unit), direction up, minimum effect declared per unit. Guards: net P&L of
  a fixed rule applied to the forecast is not below the same rule on the
  naive forecast; the leakage probe (R12) passes; evaluation wall clock.
- R11. `strategy-kpi` — **The strategy KPI Contract: net walk-forward
  Sharpe.** Objective: annualised Sharpe of the net-of-costs daily P&L series
  across the walk-forward test windows, direction up, minimum effect declared
  per unit. Guards: maximum drawdown not worse than a declared bound; trade
  count at least a declared minimum per window; turnover not above a bound;
  a deflated-Sharpe or probability-of-backtest-overfitting statistic computed
  over the loop's ledger and reported; forecast skill not worse when a model
  feeds the strategy.
- R12. `leakage-probes` — **Leakage is a crash, not a score.** The harness
  runs, before every scoring run: a shuffled-label probe (skill must fall to
  roughly zero), a future-shift probe (features shifted forward must be
  detected), timestamp monotonicity and point-in-time checks on every feature
  used. A failing probe is a `crash` with the probe named, and the candidate
  is never scored.
- R13. `eval-cli` — **One command scores anything and writes the measurements
  JSON.** `b3quant eval <candidate> --contract <forecast|strategy> --folds <fold-set>` runs the candidate's declared entry point inside the harness
  and writes the JSON the KPI Contract reads (objective, guards, harness hash,
  dataset hashes, fold set, wall clock). It is the only way a number enters
  the ledger.

### Research tracks (one `research` unit each, Findings Artifacts under `docs/research/`)

- R14. `research-swing-forecast` — **Multi-day forecasting on daily and
  hourly bars.** What works for one-to-five-day horizons on index futures and
  FX futures: gradient boosting over engineered features versus fine-tuned
  time-series foundation models versus zero-shot; evidence on constituent-to-
  index forecasting (IBOV from its weights, USD/BRL and DI); the published
  negative-R² results and what they imply. Refines R24.
- R15. `research-intraday-forecast` — **One-to-five-minute forecasting from
  trades and book.** DeepLOB, TLOB, LiT, LOBFrame, LOBERT and the 2026
  scaling study: inputs, label design (k-step mid-price change, triple
  barrier), horizon, sample sizes, what transferred beyond NASDAQ; gradient
  boosting over order-flow features (OFI, microprice, volume imbalance,
  volume clocks) as the baseline; compute per training run on one consumer
  GPU. Refines R25.
- R16. `research-book-microstructure` — **Icebergs, big-player imprints and
  order-flow signals.** Published iceberg-detection methods and their data
  requirements; what broker-tagged trades allow (participant flow,
  persistence of a participant's aggression); VPIN, OFI, trade classification
  on futures; what Brazilian retail platforms claim to detect and what is
  reproducible. Refines R26.
- R17. `research-pairs-spread` — **Pairs and spread trading on B3.**
  Cointegration and Kalman hedge-ratio methods at daily and intraday
  horizons; evidence on B3 stock pairs (ON/PN pairs, units, same-sector);
  WIN-WDO-DOL-DI structural relations and the intraday spread between WIN and
  the cash index; costs that kill pairs at two ticks. Refines R23.
- R18. `research-chart-patterns` — **Support/resistance and chart-pattern
  evidence.** Lo-Mamaysky-Wang kernel-regression detection and its
  successors; what definition of support/resistance has out-of-sample
  evidence on futures; head-and-shoulders detectability and returns; a
  go/no-go on coding pattern detectors in loop 2. Refines R22.
- R19. `research-rl` — **Reinforcement learning on top of a forecast.**
  Sizing and entry/exit policies fed by forecast signals; execution and
  market-making RL on LOB simulators; the FinRL critique and reproducibility;
  environment design over a walk-forward backtester; what evidence exists
  that RL beats a tuned rule at tens of evaluations. Refines R28.
- R20. `research-news-macro` — **Brazilian news and macro data as features.**
  Sources (Valor, Broadcast, InfoMoney, CVM filings, Bacen communications,
  B3 announcements), licences and cost, latency, prior art on LLM-extracted
  event features for intraday futures. Findings only; no consumer unit in
  loop 1.
- R21. `research-backtest-engine` — **Can NautilusTrader replay B3 trades and
  book from our schema?** Data adapters, instrument definitions for WIN/WDO
  (tick size, multiplier, session hours, rolls), fill model options, cost of
  writing the adapter versus a thin in-house replayer. Refines R27.

### Baselines, models and loops

- R22. `baseline-breakout` — **A support/resistance breakout strategy on WIN
  and WDO at five-minute bars, scored.** Levels from prior-session highs/lows
  and intraday pivots, breakout entry with a stop and a time exit, parameters
  seeded from the human's own practice; scored by R13 under R11 as the
  intraday classical baseline every later strategy must beat.
- R23. `baseline-pairs` — **Pairs and spread baselines, scored.** Daily
  cointegration pairs over the stock universe with a z-score entry/exit, and
  one futures spread (WIN against WDO or DI); scored under R11.
- R24. `model-swing` — **Swing forecast candidates and their optimize loop.**
  A gradient-boosting model over R6 features and a fine-tuned foundation
  model as candidates for one-to-five-day returns; an `optimize` loop of
  twenty evaluations under R10 with the harness of R8.
- R25. `model-intraday` — **Intraday forecast candidates and their optimize
  loop.** Gradient boosting over order-flow features as the baseline
  candidate and one DeepLOB-class network on the book data of R5; one-to-five-
  minute horizon with the label design R15 recommends; an `optimize` loop of
  twenty evaluations under R10.
- R26. `book-signals` — **Iceberg and big-player detectors as features.**
  Detectors from R16 implemented over R5's events, each scored by how much it
  improves R25's forecast skill when added, with the detector's own precision
  measured against the reconstructed book. Lands as a feature library and a
  findings table, not a strategy.
- R27. `intraday-strategy-harness` — **The intraday strategy harness over
  NautilusTrader or the thin replayer.** Reads R5 and R3, applies R7, exposes
  the R13 entry point, replays a full session deterministically; the choice
  between Nautilus and in-house follows R21's refinement.
- R28. `rl-env` — **A gym-style environment over the harness, gated.** State
  from forecasts and position, actions sizing and entry/exit, reward net P&L
  under R7, episodes walk-forward windows; it ships with a random-policy and
  a fixed-rule-policy test and no trained agent. Training starts only when a
  R24 or R25 champion beats R22/R23 under R11.
- R29. `strategy-from-forecast` — **A rule layer mapping forecasts to
  positions and its optimize loop.** Threshold entry, confidence-scaled size
  within a hard cap, stop and time exits; one `optimize` loop of twenty
  evaluations under R11 with forecast skill as a guard.
- R30. `forecast-combiner` — **A combiner over forecasts across horizons.**
  Stacking or a simple weighted blend of R24 and R25 champions, scored under
  R10 against the best single model; lands with a findings table of when the
  combination helped.

### Run shape and reporting

- R31. `loop-dials` — **Twenty evaluations per optimize loop, a declared
  wall clock per evaluation, patience 4, three consecutive reverts
  escalate.** The run is sized for about two weeks unattended; a GPU-training
  evaluation declares its wall clock and the plan schedules at most one GPU
  loop at a time.
- R32. `track-verdicts` — **A closing findings document with a go/no-go per
  track.** For each track: the champion and its KPI, the ledger digest (what
  was tried, what was ruled out), the held-out final evaluation run by the
  driver, the data gaps found, and the recommended loop-2 scope. It is the
  seed of the next brainstorm.
- R33. `driver-checklist` — **Driver-run items the plan carries.** The MT5
  export ran and its manifest entry hashes; a candidate editing `eval/` is
  discarded unscored; the leakage probe crashes a candidate with shifted
  features; the held-out year is absent from every fold set an optimize loop
  used; the two-tick cost model is in every ledger row; the purchase
  escalation was raised before any book parquet appeared.

## Non-Goals

- **Live or paper trading, order routing, broker APIs.** Two or three loops
  away.
- **High-frequency trading, co-location, latency modelling.** Horizons are
  one minute and up.
- **An LLM that trades, sizes or retrains on its own.** The LLM researches,
  codes and reviews; the orchestrator decides keep-or-revert; a human
  promotes.
- **Market-impact modelling** beyond fixed slippage; sizes are assumed small.
- **Options, Forex outside USD/BRL futures, crypto, fixed income beyond DI as
  a feature.**
- **News NLP, sentiment features, scraping.** Research only (R20).
- **Portfolio construction across strategies, risk budgeting, capital
  allocation.** One strategy at a time in loop 1.
- **Paid data beyond the bounded book purchase; cloud GPUs.**
- **Coded chart-pattern detectors** (head-and-shoulders and the like) in
  loop 1; research and a go/no-go only (R18).
- **A trained RL agent** in loop 1; the environment only (R28).
- **Any change to the orchestrator or the recipes.** Gaps found are filed as
  findings against smart_mcps, not fixed from this repo.

## Open Questions

- Which broker's MT5 the export runs through, and therefore how far back the
  M1 and tick history reaches; settled when the human runs R3's export.
- The exact fee table (emoluments, registration, settlement by contract and
  by investor type) is read from B3's published schedule by R7's coder, not
  decided here.
- Whether historical times-and-trades files carry broker codes; R4 answers
  it and R16 and R26 scale with the answer.

## Next Step

Create the `b3quant` repository, copy this document and the glossary seed,
then run `/orchestrator-plan docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md`
there, feeding the research units the briefs in
`docs/research/2026-10-02-b3quant-perplexity-briefs.md`.
