---
date: 2026-10-02
topic: b3quant-trading-research-pipeline
---

# b3quant — a B3 trading research pipeline on the orchestrator — Requirements

> **Where this document lives.** The project is a **new repository** (working
> name `b3quant`), the first external consumer of the `research`, `evaluate`
> and `optimize` Unit Recipes. This brainstorm was written inside smart_mcps
> because it was the only repo reachable from the cloud session; when the new
> repo is created, copy this file to its `docs/brainstorms/`,
> `2026-10-02-b3quant-CONTEXT.md` to its root as `CONTEXT.md`, and the whole
> of `docs/research/inbox/` (the nine Perplexity answers) to its
> `docs/research/inbox/`, then run `/orchestrator-plan` there.
>
> **Revision 2 (2026-10-02, same day).** The nine Perplexity briefs in
> `docs/research/2026-10-02-b3quant-perplexity-briefs.md` were run from the
> container through `smart-mcps-perplexity` (five with `research`, four with
> `reason`, all at `--context-size high`); the raw answers are in
> `docs/research/inbox/B1…B9`. This revision folds what they changed into the
> facts, the decisions and the requirements. Each `research` unit now
> *starts from* its inbox answer and verifies rather than rediscovers.

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

### What the nine briefs established

Raw answers in `docs/research/inbox/`; every item below is a Perplexity claim
the named `research` unit verifies against primary sources before anything
depends on it. The briefs could not reach `b3.com.br` either (the answers
are built from mirrors, glossaries and third parties), so B3's own pages are
the first thing R4 reads from the human's machine.

**Data (B1 → R4, R3, R5).**

- COTAHIST is the only fully documented free daily source: annual fixed-width
  files (245-byte records, type `01` detail rows, prices as integers with two
  implied decimals) since 1986, prices **not** adjusted for corporate actions;
  the R package `rb3` is a working reference parser. B1 claims futures appear
  in it "at contract level" — that is doubtful (COTAHIST is the equities
  segment); daily WIN/WDO bars must be sourced from the derivatives daily
  bulletin or UP2DATA, and R4 settles it.
- B3's public-data glossary still defines the daily `NEGOCIOSAVISTA` trade
  files with `CodigoParticipanteComprador` / `CodigoParticipanteVendedor`,
  anonymised only for agro derivatives. A **free, broker-tagged tape may
  still exist** for recent sessions, and a derivatives equivalent may too;
  how deep it goes is R4's first question and the one that scales R16/R26.
- UP2DATA ON DEMAND sells retroactive packages ("twenty years of history",
  "intraday data" named explicitly) by online purchase to anyone including
  academics; granularity, format and price are not public anywhere Perplexity
  could reach. Licensed distributors covering futures and FX — dxFeed, Cedro,
  Enfoque, Tryd (Guanahaní), CMA, Nelogica among others — publish no tariffs
  either. Quotes are the only way to price the purchase.
- Platform exports (Profit's book replay, MT5 ticks) are undocumented in depth
  and licence; the ~five-year MT5 M1 figure from the first draft stays a forum
  claim until the human's own export proves it.
- Order-of-magnitude sizes (unverified heuristics): WIN+WDO trades ≈ 0.9 GB
  per month as CSV; M1 bars negligible; event-level book tens of GB per year;
  one-second top-ten L2 snapshots ≈ 0.2 GB per month for both. All fit the
  terabyte; event-level book needs partitioning by instrument and day.

**Intraday forecasting (B2 → R15, R25).**

- Gradient-boosted trees over engineered order-flow features match or exceed
  DeepLOB-class networks in the head-to-head comparisons that exist (a
  BTC/USDT LOB study; the FI-2010 inference–compute frontier), and "better
  inputs beat another layer" is the stated conclusion. Deep models win only
  against naive feature baselines.
- No public evidence of transfer to index or FX futures; everything is NASDAQ,
  FI-2010 or crypto. TLOB reports predictability on equities **declining over
  time** (about 6.7 F1 points), and the early DeepLOB FI-2010 numbers are
  fragile to label and split conventions.
- Labels with one-tick thresholds do not survive two ticks per side; the
  cost-aware designs are TLOB's spread-relative trend thresholds and Briola
  et al.'s "complete transaction" operational labels, and both lower the
  headline accuracy. Compute: DeepLOB/TLOB-class training on FI-2010-sized
  data is hours on one GPU; HLOB-scale is tens of GPU-hours; nobody reports
  budgets properly.

**Book microstructure (B3 → R16, R26).**

- The three signals most likely to add skill at a two-tick cost: a multi-level
  **microprice** (Stoikov lineage, deeper-level imbalances), **aggressor-
  classified OFI** over one-to-five-minute windows (Cont et al.; Lee–Ready or
  direct bid/ask matching), and **broker-tagged aggressor imbalance** for a
  small set of persistently informative participants.
- Iceberg detection is mechanical and precise only where the exchange exposes
  iceberg semantics or stable order ids (CME native); synthetic detection
  (refills at the same price and side after fills) is a heuristic whose
  precision is instrument-dependent and must be calibrated; snapshot-only
  detection is noise. VPIN's incremental predictive power disappears once
  volume and volatility are controlled — a diagnostic, not a signal.
- B3's FIX `ClOrdID` embeds the participant code in its first eight
  positions; Nelogica's Motion Tracker (participation, "saldo" per broker,
  average price) and "TR – Acúmulo de Agressão" (cumulative aggressor delta)
  are proprietary renderings of exactly these quantities, undocumented and
  unvalidated. No Brazilian academic study of participant-level persistence
  surfaced.

**Swing forecasting (B4 → R14, R24).**

- The strongest cost-adjusted evidence at one-to-five-day horizons is gradient
  boosting over engineered and cross-asset features (a FinBERT-sentiment
  XGBoost study on EUR/USD, USD/JPY and ZN with purged expanding-window CV and
  costs deducted); a 2010–2025 futures/FX benchmark gives hybrid sequence
  models (VSN+LSTM, xLSTM) the largest break-even cost buffers.
- Time-series foundation models (Chronos-2, TimesFM-2.5, Moirai-2) show only
  sparse, small gains over a random walk on daily returns and **no net-of-cost
  result anywhere**; no TiRex finance paper exists; off-the-shelf they
  underperform finance-specific models. Multivariate Chronos-2 improves
  accuracy only across economically linked series.
- Brazil: Ibovespa work is monthly, static-split and cost-free; **no published
  WIN or WDO one-to-five-day study exists** in either language.

**Pairs and spreads (B5 → R17, R23).**

- A two-leg pair at two ticks per side costs **eight ticks per round trip**;
  at a z = 2 entry the spread needs ≥ 4 ticks of standard deviation to break
  even and a half-life under about twenty days (daily data) for a Sharpe above
  one. The arithmetic mixes tick values across legs, so the harness must
  express pair costs in BRL per leg, never in "ticks of spread".
- WIN–cash index and WDO–DOL are arbitraged away at retail cost; **WIN–WDO is
  a macro-correlation pair, not an arbitrage, and remains tradable**; DI–WDO
  is a macro spread with thinner evidence.
- Brazilian evidence favours daily same-sector cointegration pairs over the
  most liquid names (RCF 2021 on the 90 most liquid stocks 2016–18; Caldeira &
  Moura 2013; a Johansen/half-life ≤ 60-day screen); Sharpe and cost
  treatment are under-reported; **no B3 Kalman hedge-ratio study exists**.

**Reinforcement learning (B6 → R19, R28).**

- No broad evidence that RL beats a tuned rule layer at tens of training runs
  on one consumer GPU; the honest answer is "cautiously negative". The gate in
  this document stands.
- Documented failure modes with named patterns: transaction churning,
  drawdown masking, benchmark gaming, liquidity exploitation, tail-risk
  accumulation (a 2026 reward-hacking study), plus regime overfitting and
  leakage (FinRL-Meta's own critique). Mitigations with evidence: a reward
  that carries a risk term, stress tests by regime, a strict data / environment
  / agent layering, one environment per walk-forward window, features and
  normalisation computed from the past only.

**Chart patterns and breakouts (B7 → R18, R22).**

- No post-2010 peer-reviewed evidence of cost-adjusted edge from head-and-
  shoulders-class detectors on index or FX futures; what exists is
  practitioner material. Go/no-go: **no-go on a pattern detector, go on
  breakout parameterisation** — prior-session high/low and opening-range
  levels, a time-of-day filter, volatility-targeted size — with the warning
  that walk-forward studies with costs find most intraday breakout edges
  "indistinguishable from zero". The answer's own sources are weak, which is
  itself the finding R18 must either overturn with primary literature or
  confirm and close the track.

**Backtest engine (B8 → R21, R27).**

- Recommendation: NautilusTrader for the intraday harness, a vectorised stack
  (polars, vectorbt, backtesting.py) for bars. Nautilus brings book types
  L1/L2/L3, a fill-model family (default, one-tick slippage, probabilistic,
  partial-fill, size-, competition- and volume-aware, market-hours), order-book
  immutability and seedable determinism. Adapter effort "a few focused days"
  is the answer's own guess.
- The single fact that reverses it: if the breakout baseline's P&L and
  drawdown are statistically indistinguishable between Nautilus and a thin
  replayer at our sizes and horizons, Nautilus is not worth its learning cost.
  B8 did **not** verify that custom order-level (order-id) B3 events load into
  Nautilus's L3 book, nor B3 instrument and session definitions — that is
  R21's narrowed question.

**Macro calendar and news (B9 → R6, R20).**

- Free, timestamped calendar sources for loop 1: IBGE's keyless release
  calendar (JSON, UTC instants for IPCA, IPCA-15, GDP, PNAD), B3's published
  holiday and special-session table, the Copom calendar (Bacen's own open data
  first; FXMacroData and Trading Economics are paid aggregators), and a paid
  calendar API (Trading Economics) for FOMC, payrolls and Treasury auctions
  if no free source with publication instants is found.
- News: Broadcast, Valor, Reuters Brasil and Bloomberg Línea are licensed
  institutional feeds; InfoMoney is free with a multi-year dated archive but
  no API; CVM and Bacen documents are free. Prior art for LLM event features
  on intraday index products is thin (one 2025 SSRN USTEC study, about five
  points of annual return). Loop 1 stays calendar-only; loop 2 budgets
  licences.

## Key Decisions

- **A new repository, `b3quant`.** Runs would otherwise drag the plugin's
  test suite, Preflight and git history along. smart_mcps is installed as the
  plugin; this document, the glossary seed and the research inbox are copied
  over. Rejected: a subfolder of smart_mcps.

- **The first loop delivers the ground, not a profit.** Data, costs, harness,
  baselines, findings and a ledger. A strategy clearing a Sharpe bar is a
  bonus, never the acceptance criterion — the ledger of twenty ruled-out
  ideas per track is what the second loop plans from. Rejected: "one strategy
  over threshold" (puts the optimizer in charge of defining profitable).

- **Research units verify the inbox; they do not re-run the briefs.** Each
  `research` unit lists its `docs/research/inbox/B<n>` answer in
  `focus_paths`, takes its claims as hypotheses, checks the ones the consumer
  unit depends on against primary sources (B3's own pages, papers, repos),
  and writes a Findings Artifact that says which held, which did not, and what
  was unreachable. Rejected: feeding the raw answers to coders (unverified
  claims, several already suspect); re-running Deep Research inside the run
  (ten minutes and real money per unit for the same text).

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
  slippage per side, always, stated in BRL per leg.** The human chose the
  harsher of the offered models. A one-tick variant is reported as an
  observation so the gap is visible, never used to keep a candidate. B5's
  arithmetic makes the consequence explicit: a pair pays eight ticks per
  round trip, so pair candidates need ≥ 4 ticks of spread volatility just to
  break even — the harness reports that threshold next to every pair score.
  Rejected: one tick (the human's own experience says fills are worse than
  that for a retail account); costs in "ticks of spread" (legs have different
  tick values).

- **Cost-aware labels for every classification model.** A forecast label's
  threshold is never below the round-trip cost of acting on it (four ticks on
  a single instrument), or the label is transaction-centric (does a round trip
  entered on this forecast clear costs). B2 showed one-tick labels produce
  accuracy that is economically empty. Rejected: FI-2010-style one-tick
  thresholds (inflate skill, fail the net-P&L guard).

- **Instrument universe: WIN and WDO traded, the ~20 most liquid IBOV stocks
  traded for pairs, everything else a feature.** USD/BRL spot, DI futures,
  ES/NQ, DXY, Brent and iron-ore proxies, the macro calendar and B3 regime
  flags are covariates only. Rejected: WIN+WDO only (no equity pairs); the
  full IBOV (heaviest ingestion for a loop whose point is the pipeline).

- **Free data first, one bounded book purchase, priced by quotes.** MT5
  ticks/M1, COTAHIST, the derivatives daily bulletin and — if R4 finds them
  still published — B3's public trade files with participant codes are
  ingested first. Book data is bought for a chosen window only after R4 has
  quotes in hand from UP2DATA ON DEMAND and at least two licensed
  distributors (dxFeed, Cedro, Enfoque or Tryd), for both event-level and
  one-second L2 snapshot granularity, with licence terms for personal
  research; the purchase is a human decision raised as an escalation, bounded
  at roughly R$ 2,000 as a *target*: if every quote exceeds it, the fallback
  order is a shorter window, then snapshots instead of events, then the
  human's own platform export. Rejected: free only (the book track is the
  human's main edge and would be starved); pay whatever it costs; deciding
  granularity before the quotes exist.

- **Split backtest stack, with a kill test.** Vectorised research (polars,
  vectorbt-class tooling) for forecast skill and swing strategies;
  NautilusTrader for the intraday strategy harness where fill realism
  decides. R21 confirms Nautilus loads our order-level schema into its L3
  book and defines WIN/WDO before any coder depends on it; if it cannot
  without an adapter larger than a thin engine, the refinement switches the
  intraday harness to a thin in-house replayer. Either way R27 ships both
  paths for the breakout baseline on the same sessions, and if their P&L and
  drawdown are statistically indistinguishable the Track Verdict records that
  Nautilus is optional for loop 2 (B8's own reversing fact). Rejected:
  Nautilus everywhere (every worker pays the learning cost); own engine
  everywhere (fills we invent are fills we cannot trust).

- **Compute is one consumer GPU and about a terabyte.** Tick-level book work
  for months, M1 for years, DeepLOB-class training feasible (hours per run on
  FI-2010-sized data per B2) but budgeted per evaluation. No cloud GPU in
  loop 1.

- **Gradient boosting is the first candidate in every model loop.** B2 and B4
  agree: trees over good features are the strongest cost-adjusted baseline at
  both horizons, and deep or foundation models have not beaten them net of
  costs in public work. The second candidate per loop is chosen by the
  track's research unit (a DeepLOB/TLOB-class network for intraday; a
  fine-tuned foundation model or an LSTM-class sequence model for swing).
  Rejected: a foundation model as the swing baseline (no net-of-cost evidence
  anywhere); three candidates per loop (twenty evaluations is too few to
  separate them).

- **Reinforcement learning is gated, and B6 confirms the gate.** Loop 1 ships
  a research unit and a gym-style environment over the harness; RL training
  (sizing, entry/exit policy fed by forecast signals) starts only when a
  forecast model beats the classical baseline on the harness. Rejected: an RL
  track from loop 1 (no evidence it beats a tuned rule at tens of runs on one
  GPU; documented reward-hacking patterns); deferring entirely (the
  environment is cheap and the research is needed to plan loop 2).

- **Macro calendar in, news NLP out.** IBGE's release calendar, Bacen's Copom
  dates, B3's holiday table and a US calendar source are features with
  publication instants; a research unit maps Brazilian news sources, licences
  and costs; no scraping or LLM feature extraction in loop 1. Rejected: news
  NLP now (a licensing and cost problem on top of a data problem, and thin
  prior art for intraday index products).

- **Classical scope: breakout and pairs coded, chart patterns researched.**
  Support/resistance breakout on prior-session and opening-range levels and
  pairs/spread trading (same-sector stock pairs, WIN–WDO) are coded, scored
  baselines; head-and-shoulders and other chart patterns get a research unit
  and a go/no-go, no coder in loop 1 — B7 already leans no-go. WIN–cash and
  WDO–DOL spreads are not coded (arbitraged away at retail cost). Rejected:
  coding all three (rounds spent on the weakest evidence); dropping patterns.

- **Loop-1 budget: about two weeks unattended, twenty evaluations per
  optimize loop.** The recipe's validated dials (patience 4, three
  consecutive reverts escalate); usage-limit pauses resume on their own.

- **Perplexity access is an environment fact, never a repo fact.** The
  `research` recipe's workers call `smart-mcps-perplexity`, and the
  orchestrator hands every worker the run-driver's own environment
  (`orchestrator/execution/sessions.py` builds the worker env from
  `os.environ`). So the run-driver's shell exports `PERPLEXITY_API_KEY`
  before `run`, from a gitignored `.envrc` on a local machine or an
  environment secret on a cloud container; the key is never committed, never
  in a plan, never in a `Run:` line. Rejected: a config-file key (would be
  committed by accident); passing it per unit (every research unit needs it).

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
  `docs/brainstorms/`, the nine answers under `docs/research/inbox/`, the
  glossary seed as `CONTEXT.md`, an `.envrc.example` naming
  `PERPLEXITY_API_KEY` with `.envrc` gitignored, and a `README` that states
  the non-goals (no live trading, no LLM trader).
- R2. `data-lake` — **A content-hashed parquet lake with a dataset
  manifest.** Every dataset lives under `data/<source>/<instrument>/ <granularity>/` as parquet with UTC timestamps (session times are
  America/Sao_Paulo and stored as UTC plus a session-date column), and is
  listed in `data/MANIFEST.json` with source, licence, instrument, granularity,
  date range, row count and sha256. A dataset absent from the manifest does
  not exist for the harness.
- R3. `free-ingest` — **Ingest the free sources into the lake.** MT5 M1 bars
  and ticks for WIN and WDO (the export runs on the human's machine through
  their broker's MT5 and is a driver-run item; its first manifest entry
  settles how far back the history really reaches), COTAHIST daily for the
  stock universe (fixed-width layout, two implied decimals, unadjusted — the
  parser carries a corporate-action adjustment table), daily WIN/WDO bars
  from whichever derivatives source R4 confirms (COTAHIST is not assumed to
  carry them), and B3's public trade files with participant codes for every
  session still downloadable if R4 finds them live; for each the parser, the
  schema test and a manifest entry. WIN and WDO are also published as
  continuous contracts with the roll rule (volume-based, roll date recorded)
  and a back-adjustment flag; raw per-contract series are kept.
- R4. `book-data-research` — **A research unit prices historical B3 offers
  and book data and refines the book-ingest unit.** Starts from
  `docs/research/inbox/B1-data-sources-book-purchase.md`. Questions, in
  order: are B3's public `NEGOCIOS*` trade files (buyer/seller participant
  codes) still published, for which segments, and how many sessions back;
  which derivatives daily file carries WIN/WDO bars; what UP2DATA ON DEMAND
  actually delivers under "intraday data" (bars, trades, offers, snapshots),
  in what format, at what price for a six- and a twelve-month WIN+WDO window;
  the same from at least two licensed distributors (dxFeed, Cedro, Enfoque,
  Tryd), for event-level and one-second L2 snapshot granularity, with licence
  terms for personal research; whether any historical tape carries broker
  codes; whether Profit's book replay exports. The Findings Artifact ends in
  a purchase recommendation against the R$ 2,000 target with the fallback
  order (shorter window, snapshots, platform export) and a Spec Refinement of
  R5 naming the format to parse. The purchase itself is an escalation the
  human answers; quotes that need a B3 login or a phone call are driver-run
  items the human completes.
- R5. `book-ingest` — **Ingest the purchased or otherwise obtained book data
  into an event-level schema both stacks read.** Order events (add, modify,
  cancel, trade) with instrument, timestamp, side, price, quantity, order id,
  and broker code when present; an L2 snapshot reconstruction at a declared
  depth and cadence; schema tests that replay a day and check the
  reconstructed top of book against the trade tape. Partitioned by instrument
  and session day (event-level book is tens of GB per year). If no book data
  is obtained, the unit lands with the schema and tests against the MT5 tick
  stream and the public trade files, and reports the gap in its manifest
  entry.
- R6. `exogenous-features` — **A point-in-time feature store for covariates.**
  USD/BRL spot, DI futures curve points, ES/NQ, DXY, Brent and an iron-ore
  proxy at daily and, where free, M1; the macro calendar — IBGE's release
  calendar (IPCA, IPCA-15, GDP, PNAD, with their UTC publication instants),
  Bacen's Copom decision and minutes dates, FOMC, payroll and Treasury-auction
  instants from a named US calendar source, B3 holidays, special sessions,
  auction and circuit-breaker flags — as event features with the publication
  time, never the reference time. Every feature carries the timestamp it
  became known at, and a test proves no feature at time t uses data published
  after t.
- R7. `cost-model` — **B3 fees from an ingested table plus two ticks per side,
  in BRL per leg.** Emoluments, registration and settlement fees per contract
  and per share from a versioned fee table in the harness; brokerage zero
  (discount broker); slippage two ticks per side on WIN and WDO, two ticks on
  stocks, converted to BRL per leg from each instrument's tick value; a
  one-tick variant computed and reported as an observation; for a pair, the
  round-trip cost and the break-even spread volatility it implies reported
  next to the score. The fee table, tick values and slippage constants are
  harness paths.

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
  naive baseline, on cost-aware labels.** Objective: out-of-sample skill score
  (one minus the ratio of the model's loss to the naive loss, where the loss
  is pinball loss for return quantiles or log-loss for triple-barrier or
  trend classes, declared per unit), direction up, minimum effect declared
  per unit. A classification label's threshold is at least the round-trip
  cost of acting on it, or the label is transaction-centric; the harness
  refuses a label spec below that. Guards: net P&L of a fixed rule applied to
  the forecast is not below the same rule on the naive forecast; the leakage
  probe (R12) passes; evaluation wall clock.
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

Every unit below lists its inbox answer in `focus_paths`, treats its claims as
hypotheses, and reports per claim: held, failed, or unreachable from the
worker's network.

- R14. `research-swing-forecast` — **Multi-day forecasting on daily and
  hourly bars.** From `inbox/B4`. Verify the cost-adjusted gradient-boosting
  result (purged expanding-window CV, costs deducted) and the 2010–2025
  futures/FX benchmark's break-even buffers for VSN+LSTM and xLSTM; confirm
  that no foundation-model paper reports net-of-cost results on daily
  returns; look once more for any WIN/WDO or Ibovespa daily study with a
  walk-forward protocol. Decide R24's second candidate (fine-tuned foundation
  model versus LSTM-class sequence model) and the feature set the first
  candidate starts from. Refines R24.
- R15. `research-intraday-forecast` — **One-to-five-minute forecasting from
  trades and book.** From `inbox/B2`. Verify the gradient-boosting-versus-
  deep head-to-heads (the crypto LOB study, the inference–compute frontier),
  TLOB's declining-predictability figure and its spread-relative labels,
  Briola et al.'s operational labels; estimate training wall clock on one
  consumer GPU for a DeepLOB/TLOB-class network at our data size; recommend
  R25's label design and horizon under four ticks of round-trip cost. Refines
  R25.
- R16. `research-book-microstructure` — **Icebergs, big-player imprints and
  order-flow signals.** From `inbox/B3`. Verify the microprice, OFI and
  queue-imbalance results and their reported half-lives on futures; confirm
  the VPIN critique; establish whether B3 exposes iceberg semantics or stable
  order ids in any data R4 found (that decides mechanical versus heuristic
  detection); find any Brazilian study of participant-level flow persistence;
  document what Nelogica's Motion Tracker and TR – Acúmulo de Agressão
  actually compute. Refines R26.
- R17. `research-pairs-spread` — **Pairs and spread trading on B3.** From
  `inbox/B5`. Verify the Brazilian cointegration studies' periods, universes
  and cost treatment; confirm WIN–cash and WDO–DOL are arbitraged at retail
  cost and WIN–WDO is not; redo B5's break-even arithmetic in BRL per leg
  with real tick values; set the half-life and volatility screens R23 uses.
  Refines R23.
- R18. `research-chart-patterns` — **Support/resistance and chart-pattern
  evidence.** From `inbox/B7`, whose sources are mostly practitioner
  material. Search the primary literature (Lo–Mamaysky–Wang successors after
  2010, opening-range and prior-session breakout studies with walk-forward
  protocols and costs) for anything B7 missed; settle the go/no-go on coding
  pattern detectors in loop 2 with the evidence named; hand R22 the level
  definitions, time-of-day filter and sizing rule with the best support.
  Refines R22.
- R19. `research-rl` — **Reinforcement learning on top of a forecast.** From
  `inbox/B6`. Verify the reward-hacking taxonomy and the FinRL-Meta leakage
  critique; collect the environment-design patterns (per-window environment
  factory, past-only features, training-statistics normalisation, risk term
  in the reward) with their sources; specify R28's state, action, reward and
  episode and its leakage test. Refines R28.
- R20. `research-news-macro` — **Brazilian news and macro data as features.**
  From `inbox/B9`. Verify the IBGE calendar endpoint and its timestamp
  precision, find Bacen's own Copom calendar in its open data, name a US
  calendar source with publication instants (free first), confirm B3's
  holiday and special-session table; price the paid news feeds and document
  InfoMoney, CVM and Bacen access for loop 2. Refines R6; findings only
  otherwise, no news consumer in loop 1.
- R21. `research-backtest-engine` — **Can NautilusTrader replay B3 trades and
  book from our schema?** From `inbox/B8`. The narrowed question: load one
  session of R5's order-level events into Nautilus's L3 book type and its
  trade tape, define WIN and WDO (tick size, multiplier, session hours,
  expiry, roll) as instruments, run a market order through the partial-fill
  model deterministically, and measure replay time; report the adapter's real
  size. If it fails, say exactly where. Refines R27.

### Baselines, models and loops

- R22. `baseline-breakout` — **A support/resistance breakout strategy on WIN
  and WDO at five-minute bars, scored.** Levels from prior-session high and
  low and the opening range, breakout entry with a stop and a time exit, a
  time-of-day filter and volatility-targeted size, parameters seeded from the
  human's own practice and R18's findings; scored by R13 under R11 as the
  intraday classical baseline every later strategy must beat.
- R23. `baseline-pairs` — **Pairs and spread baselines, scored.** Daily
  same-sector cointegration pairs over the stock universe (Johansen or
  Engle–Granger, half-life and spread-volatility screens from R17, z-score
  entry/exit) and the WIN–WDO spread at five-minute bars with a static hedge
  ratio; both scored under R11 with costs in BRL per leg and the break-even
  threshold reported. WIN–cash and WDO–DOL are not coded. A Kalman hedge
  ratio is a candidate for the strategy optimize loop, not a baseline.
- R24. `model-swing` — **Swing forecast candidates and their optimize loop.**
  A gradient-boosting model over R6 features as the first candidate and the
  second candidate R14 names, for one-to-five-day returns; an `optimize` loop
  of twenty evaluations under R10 with the harness of R8.
- R25. `model-intraday` — **Intraday forecast candidates and their optimize
  loop.** Gradient boosting over order-flow features (multi-level microprice,
  aggressor OFI, queue imbalance, volume clocks) with the cost-aware label R15
  recommends as the first candidate, and one DeepLOB/TLOB-class network on
  the book data of R5 as the second; one-to-five-minute horizon; an
  `optimize` loop of twenty evaluations under R10.
- R26. `book-signals` — **Iceberg and big-player detectors as features.** The
  three B3 signals (multi-level microprice, aggressor OFI, broker-tagged
  aggressor imbalance for a small persistent set) and a synthetic iceberg
  heuristic implemented over R5's events, each scored by how much it improves
  R25's forecast skill when added, the iceberg detector's precision measured
  against the reconstructed book, VPIN computed only as a diagnostic
  observation. Broker-tagged signals land only if a tape with codes exists
  (R4); otherwise the unit records that gap. Lands as a feature library and a
  findings table, not a strategy.
- R27. `intraday-strategy-harness` — **The intraday strategy harness over
  NautilusTrader or the thin replayer, with the kill test.** Reads R5 and R3,
  applies R7, exposes the R13 entry point, replays a full session
  deterministically; the engine follows R21's refinement. Runs R22 on the
  same sessions through both the chosen engine and a thin trades-plus-L2
  replayer with fixed two-tick slippage and reports whether P&L and drawdown
  differ beyond noise; the Track Verdict carries that answer.
- R28. `rl-env` — **A gym-style environment over the harness, gated.** One
  environment per walk-forward window built by a factory from R9's fold set;
  state from forecasts and position computed from the past only, actions
  sizing and entry/exit, reward net P&L under R7 with a drawdown term,
  episodes the fold's test windows; normalisation statistics from the
  training window; it ships with a random-policy test, a fixed-rule-policy
  test and a leakage test (a test-window environment cannot read a training
  window's data) and no trained agent. Training starts only when a R24 or R25
  champion beats R22/R23 under R11.
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
  driver, the data gaps found, which inbox claims failed verification, and
  the recommended loop-2 scope. It is the seed of the next brainstorm.
- R33. `driver-checklist` — **Driver-run items the plan carries.**
  `PERPLEXITY_API_KEY` is exported in the run-driver's shell before `run`
  (every research unit fails without it); the MT5 export ran and its manifest
  entry hashes; the book quotes that need a login or a call were obtained; a
  candidate editing `eval/` is discarded unscored; the leakage probe crashes a
  candidate with shifted features; the held-out year is absent from every
  fold set an optimize loop used; the two-tick cost model in BRL per leg is
  in every ledger row; the purchase escalation was raised before any book
  parquet appeared.

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
- **Paid data beyond the bounded book purchase; cloud GPUs; paid calendar or
  news APIs** (a paid US calendar source is allowed only if R20 finds no free
  one with publication instants).
- **Coded chart-pattern detectors** (head-and-shoulders and the like) in
  loop 1; research and a go/no-go only (R18).
- **WIN–cash-index and WDO–DOL spread strategies** (arbitraged away at retail
  cost per B5; R17 may reopen with evidence).
- **A trained RL agent** in loop 1; the environment only (R28).
- **Re-running the Perplexity briefs inside the run.** The inbox is the
  starting material; research units verify.
- **Any change to the orchestrator or the recipes.** Gaps found are filed as
  findings against smart_mcps, not fixed from this repo.

## Open Questions

- Which broker's MT5 the export runs through, and therefore how far back the
  M1 and tick history reaches; settled when the human runs R3's export.
- Whether B3 still publishes the daily `NEGOCIOS*` trade files with
  participant codes and for how many sessions back, and which derivatives
  daily file carries WIN/WDO bars; R4 answers both from the human's network
  (b3.com.br was unreachable from the container and from Perplexity).
- The exact fee table (emoluments, registration, settlement by contract and
  by investor type) is read from B3's published schedule by R7's coder, not
  decided here.
- Whether any purchasable tape carries broker codes; R4 answers it and R16
  and R26 scale with the answer.
- Whether Nautilus's L3 book accepts our order-level schema without a large
  adapter; R21 answers it and R27 follows.

## Next Step

Create the `b3quant` repository, copy this document, the glossary seed and
`docs/research/inbox/`, export `PERPLEXITY_API_KEY` in that session's shell
(`.envrc` from `.envrc.example`, or an environment secret on a cloud
container), then run
`/orchestrator-plan docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md`
there. Each research unit's `focus_paths` names its inbox answer; the briefs
in `docs/research/2026-10-02-b3quant-perplexity-briefs.md` stay as the
record of what was asked and are re-run only if a track's question changes.
