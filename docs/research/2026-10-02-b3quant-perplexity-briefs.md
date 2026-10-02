# b3quant — Perplexity briefs for the loop-1 research tracks

**Date:** 2026-10-02
**Purpose:** the nine questions, one per research track, that seed each
`research` unit's `recipe_args` in the plan built from
`docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md`.
Each brief maps to one requirement there.

**Status (2026-10-02, later the same day):** all nine were run from the cloud
container through `smart-mcps-perplexity` with `--context-size high` — B1,
B2, B3, B4 and B6 with `research` (sonar-deep-research, nine to ten minutes
each), B5, B7, B8 and B9 with `reason` (sonar-reasoning-pro, one to three
minutes each). The raw answers are committed under `docs/research/inbox/` as
`B<n>-<slug>.md`, each headed with the mode and wall clock. The first draft of
this file assumed the container could not reach `api.perplexity.ai`; it can,
given `PERPLEXITY_API_KEY` in the environment. `b3.com.br` is still
unreachable from here, and Perplexity's own retrieval did not reach it
either, so B1's pricing questions stay open for the human's network.

The briefs are kept verbatim below so a track can be re-run when its question
changes. Re-running is not part of the orchestrator run: the plan's research
units read the inbox answer named in their `focus_paths` and verify its
claims against primary sources.

## Which mode to use

| Mode in the Perplexity app      | CLI equivalent | Use it when                                                                                         |
| ------------------------------- | -------------- | --------------------------------------------------------------------------------------------------- |
| **Deep Research** (long report) | `research`     | the question is a landscape: many papers, vendors or methods to enumerate and compare, dates matter |
| **Pro search with reasoning**   | `reason`       | the question is already framed with named options and needs a recommendation with trade-offs        |
| **Normal Pro search**           | `ask`          | one fact or one list, answerable from a few pages                                                   |

Deep Research was used for B1, B2, B3, B4 and B6; reasoning for B5, B7, B8
and B9. To re-run one brief from the CLI, write its fenced text to a file and
pass it with `--file`:

```sh
smart-mcps-perplexity research "Answer the brief in the attached file in full, with sources (URL, year) for every claim." --file B1.md --context-size high
```

Each brief below is self-contained on purpose: Perplexity has no memory
across runs.

______________________________________________________________________

## B1 · data sources and the book purchase (R4) — Deep Research

```
I am building a historical-only research pipeline for Brazilian exchange B3
futures (mini index WIN, mini dollar WDO) and the ~20 most liquid IBOV stocks,
on one desktop with ~1 TB disk. I need historical data, not real time.

Enumerate, as of 2026, every way to obtain historical B3 market data at these
granularities, with sources and dates:
1. Daily bars (COTAHIST and anything else free).
2. Intraday 1-minute bars and tick-by-tick trades ("negócios") for WIN, WDO and
   stocks: free options (MetaTrader 5 via Brazilian brokers — how far back,
   which brokers; Profit/Nelogica; Tryd; others), and paid options.
3. Order-book data: offers-level events ("ofertas", add/modify/cancel) or L2
   snapshots. B3 used to publish free daily NEG/OFER_CPA/OFER_VDA files; when
   did that stop, and what replaced it? What do UP2DATA ON DEMAND, DATAWISE
   and third-party vendors (list them) sell, in what format, at what price per
   month or per instrument, with what minimum order and what licence for
   personal research?
4. Broker codes on trades: does historical times-and-trades data from any
   source carry buyer and seller participant (corretora) codes? Which sources?

For each source give: granularity, instruments covered, history depth, file
format and schema notes, approximate size per month for WIN+WDO, price in BRL,
licence restrictions, and a URL. Finish with a recommendation for a 6-12 month
WIN+WDO order-book purchase under roughly R$ 2,000, and say explicitly which
facts you could not verify.
```

## B2 · intraday forecasting from trades and book (R15) — Deep Research

```
Survey, as of 2026, deep learning and gradient-boosting methods for 1-to-5-
minute price forecasting from limit order book and trade data: DeepLOB (2019),
TLOB, LiT, LOBFrame, LOBERT, the 2026 scaling-law study on LOB models, and any
others with public code. For each: input representation (raw levels, OFI,
microprice, volume clocks), label design (k-step mid-price change with
thresholds, triple-barrier), horizon, dataset (FI-2010, NASDAQ LOBSTER,
others), sample size, reported performance versus a naive or logistic
baseline, and compute per training run (GPU hours). Then answer:
- What evidence exists of transfer beyond NASDAQ stocks, especially to index
  or FX futures with a single tick size and a deep book?
- Do gradient-boosted trees over order-flow features match the deep models at
  these horizons? Cite head-to-head comparisons.
- What label designs survive transaction costs of about two ticks per side?
- Which published results failed to replicate, and why?
Cite papers with year and code links; mark anything from blogs as such.
```

## B3 · book microstructure, icebergs and big-player imprints (R16) — Deep Research

```
I have (or will buy) historical order-book events and a trade tape for B3
futures WIN and WDO, possibly with buyer/seller broker codes on each trade.
Survey, with citations and years:
1. Iceberg-order detection methods: the data each needs (full event stream vs
   L2 snapshots), the detection rule, reported precision, and public code.
2. Order-flow signals with predictive evidence at 1-5 minute horizons: order
   flow imbalance (Cont et al.), microprice, VPIN, trade-classification
   methods for futures, queue-imbalance models, and their reported half-lives.
3. What broker-tagged flow enables: academic work on participant-level flow
   persistence, "follow the informed trader" strategies, herding detection,
   and any Brazilian studies using B3 participant codes (search Portuguese:
   "rastreador de players", "fluxo de corretoras", "agressão", "ofertas
   iceberg", "B3 dados de corretora").
4. What Brazilian retail platforms (Profit, Tryd, Nelogica tools) claim to
   detect, and whether any of it is documented or reproducible.
Conclude with the three signals most likely to add forecast skill at a
2-tick cost, and what data each requires.
```

## B4 · swing forecasting and foundation models (R14) — Deep Research

```
Survey, as of 2026, methods for 1-to-5-day return forecasting on index futures
and FX futures, with emphasis on what works out of sample net of costs:
- Gradient boosting over engineered features (what features, what horizon,
  reported out-of-sample R² or skill).
- Time-series foundation models (Chronos-2, TimesFM-2.5, Moirai-2, TiRex):
  zero-shot vs fine-tuned results on financial returns; the 2025-2026 papers
  reporting negative out-of-sample R² versus CatBoost/LightGBM; any positive
  intraday or multi-asset results.
- Constituent-to-index forecasting: evidence that forecasting the parts (index
  weights, USD/BRL, interest-rate futures) improves an index forecast.
- Brazil-specific work on IBOV, WIN or WDO forecasting (search Portuguese too:
  "previsão Ibovespa aprendizado de máquina", "mini índice previsão").
For each result give the dataset, period, horizon, evaluation protocol
(walk-forward or not), the baseline it beat, and whether costs were charged.
Flag any study without a walk-forward or purged evaluation.
```

## B5 · pairs and spread trading on B3 (R17) — reasoning

```
Context: a research harness on B3 charging two ticks of slippage per side plus
B3 fees, daily and 5-minute data for the ~20 most liquid IBOV stocks and the
WIN, WDO, DOL and DI futures. Evaluate, with citations:
1. Cointegration-based and Kalman-hedge-ratio pairs on B3 stocks at daily and
   intraday horizons: which published Brazilian studies exist, what Sharpe
   they report, and whether costs were charged. Which pair types (ON/PN of the
   same issuer, units vs components, same-sector) have the best evidence?
2. Futures spreads: the structural relations WIN-WDO, WDO-DOL, WIN-cash index
   (arbitrage via ETFs), DI-WDO; which are tradable at retail cost and which
   are arbitraged away.
3. The cost threshold: at two ticks per side, what half-life and spread
   volatility does a pair need to be worth trading? Show the arithmetic.
Recommend which two baselines to code first and why.
```

## B6 · reinforcement learning on top of a forecast (R19) — Deep Research

```
Survey, as of 2026, reinforcement learning for trading where the agent is a
sizing/entry/exit policy fed by forecast signals, not an end-to-end
price predictor: execution RL on LOB simulators (JAX-LOB, mbt-gym, ABIDES),
market-making RL, FinRL and FinRL-Meta contests and their critiques,
Eureka-style reward design, and offline RL on historical tapes. For each: the
environment design (state, action, reward, episode), the evaluation protocol,
the baseline it beat, reproducibility status, and compute. Then answer:
- Is there evidence RL beats a tuned rule layer at a budget of tens of
  training runs on one consumer GPU?
- How do practitioners build a gym-style environment over a walk-forward
  backtester without leaking the test windows into training?
- Which failure modes (reward hacking, non-stationarity, overfitting to one
  regime) are documented, and which mitigations have evidence?
Cite papers with year and code.
```

## B7 · chart patterns and support/resistance evidence (R18) — reasoning

```
Context: a research harness on B3 futures WIN and WDO at 5-minute and daily
bars, two ticks of cost per side. Assess the evidence, with citations:
1. Lo, Mamaysky and Wang (2000) kernel-regression pattern detection and its
   successors: have head-and-shoulders and similar patterns shown
   out-of-sample predictive content on futures after 2010, net of costs?
2. Support and resistance: which definitions (prior-session high/low, pivot
   points, volume-profile nodes, round numbers) have published out-of-sample
   evidence on index or FX futures, at what horizon?
3. Breakout strategies on intraday futures: reported results with walk-forward
   evaluation and costs.
Give a go/no-go on coding a chart-pattern detector versus spending the same
effort on breakout parameterisation, with the reasoning.
```

## B8 · backtest engine for B3 (R21) — reasoning

```
Context: a Python research repo with polars parquet datasets of B3 futures
(WIN, WDO) trades, order-book events (add/modify/cancel/trade with order ids)
and L2 snapshots, plus 1-minute bars; one desktop. Compare, as of 2026:
1. NautilusTrader for the intraday strategy harness: how to define B3 futures
   instruments (tick size, multiplier, session hours, expiry and roll), how to
   load custom order-book event and trade data (data catalog, wrangler
   classes), its fill models (queue position, partial fills), determinism,
   and typical replay speed for one session of a deep book. Estimate the
   adapter effort in days.
2. vectorbt (open source vs PRO), backtesting.py, polars-native approaches
   for the vectorised research path on bars.
3. A thin in-house replayer (trades + L2 snapshots, fixed 2-tick slippage):
   what it cannot model that Nautilus can, and whether that matters at
   1-5 minute horizons with small sizes.
Recommend one of: Nautilus for intraday + vectorised for the rest; thin
in-house everywhere; Nautilus everywhere. Name the single fact that would
reverse the recommendation.
```

## B9 · Brazilian news and macro features (R20) — reasoning

```
Context: a historical-only research pipeline on B3 futures WIN and WDO at
1-5 minute and multi-day horizons; loop 1 ingests only a macro calendar,
loop 2 may add news features. Assess, with sources and prices:
1. Macro calendar sources with publication timestamps (Copom decisions and
   minutes, IPCA, payroll, FOMC, Treasury auctions, B3 holidays and auction
   flags): free APIs or files, their timestamp precision, and how to align
   them point-in-time.
2. Brazilian financial news archives with timestamps: Valor, Broadcast
   (Estadão), InfoMoney, Reuters Brasil, CVM filings, Bacen communications;
   licence terms for research, historical depth, cost, and whether an API or
   bulk export exists.
3. Prior art on LLM-extracted event features for intraday index or FX futures
   (not daily stock sentiment): what signal, what horizon, what evidence.
Recommend what to ingest in loop 1 (calendar only) and what to budget for
news in loop 2.
```

______________________________________________________________________

## What the answers changed

The brainstorm's "What the nine briefs established" section carries the
digest; the one-line verdicts:

| Brief | Verdict that reached the brainstorm                                                                                                                                             |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B1    | No public pricing anywhere; purchase is priced by quotes (UP2DATA ON DEMAND plus two distributors). Lead: B3's public `NEGOCIOS*` files with participant codes may still exist. |
| B2    | Gradient boosting over order-flow features is the intraday baseline; one-tick labels are economically empty; no evidence of transfer to futures.                                |
| B3    | Three signals: multi-level microprice, aggressor OFI, broker-tagged aggressor imbalance. Iceberg detection heuristic unless order ids exist. VPIN is a diagnostic.              |
| B4    | Gradient boosting first for swing; foundation models have no net-of-cost evidence; no WIN/WDO daily study exists.                                                               |
| B5    | Eight ticks per pair round trip, ≥ 4 ticks of spread volatility to break even; WIN–WDO tradable, WIN–cash and WDO–DOL arbitraged away; no B3 Kalman evidence.                   |
| B6    | No evidence RL beats a tuned rule at tens of runs on one GPU; the gate stands; environment design patterns named.                                                               |
| B7    | No-go on pattern detectors, go on breakout parameterisation; the answer's own sources are weak, so R18 checks the primary literature.                                           |
| B8    | Nautilus for intraday plus a vectorised stack, with a kill test against a thin replayer; custom L3 ingestion for B3 is still unverified.                                        |
| B9    | Free timestamped calendars exist (IBGE, B3 holidays, Bacen); news feeds are licensed and deferred to loop 2.                                                                    |
