# B8 · backtest engine for B3 (R21)

**Date:** 2026-10-02  
**Mode:** `smart-mcps-perplexity reason` (sonar-reasoning-pro), `--context-size high`, wall clock 83s  
**Brief:** `docs/research/2026-10-02-b3quant-perplexity-briefs.md` § B8  
**Status:** raw Perplexity answer, unverified — the `research` unit for R21 verifies the claims that matter against primary sources. Inline citation numbers were stripped by the CLI; URLs that survived are in the text.

---

NautilusTrader is the best fit for intraday order‑book strategies on your B3 futures data, paired with a vectorised bar‑research stack (vectorbt + backtesting.py + polars) for 1‑minute work; a thin in‑house replayer alone leaves important microstructure and risk questions under‑modelled.  
The single fact that would reverse this recommendation is robust evidence that your 1–5 minute strategies’ P&L and risk are statistically indistinguishable when backtested with a microstructure‑aware Nautilus setup versus a simple 2‑tick‑slippage replayer on the same trade/L2 data.

---

## 1. NautilusTrader as an intraday strategy harness

### Instrument definition for B3 futures

NautilusTrader’s backtesting stack is organized around venues and instruments, with a backtest venue configuration that sets OMS type, account type, book type and other simulation parameters, including starting balances and fill models. Instruments are defined separately and tied to venues, allowing you to specify contract metadata such as tick size, contract value, and trading session behavior through configuration rather than hard‑coding them into strategies.  

The backtesting docs explicitly highlight deterministic “fill prices and matching” behavior as a core concept, indicating that the engine is designed to replay historical markets in a way that preserves the original book while applying instrument and venue rules.  

Session hours and market‑hours behavior can be controlled via fill models such as `MarketHoursFillModel`, which explicitly references normal versus one‑tick‑wider synthetic spreads during market hours versus off‑hours. Expiry and roll would be handled at the instrument layer (per‑contract definitions) and/or via your data catalog and scenario definitions, with the backtesting docs listing “fill prices and matching” and “fill models” as separate configuration topics in the backtest workflow.  

(Practical detail such as the exact B3 calendar, contract multipliers, and roll logic is not spelled out in the snippets above; implementing that would rely on your own domain configuration in the instrument and data layers.)

### Loading custom order‑book event and trade data

NautilusTrader’s core concept documentation emphasizes an order book abstraction and a `simulate_fills(order)` API for the book that returns price/quantity tuples for a given order against the recorded book state. This design assumes book events (quotes, trades, etc.) are ingested into a canonical order‑book object, which then becomes the reference for matching and fill simulation during backtests.  

Fill models are configured at the backtest venue level (e.g. `fill_model=DefaultFillModel(...)`) and operate against the venue’s book type (such as `BookType.L1_MBP`), which shows that the engine expects you to plug in your own historical book data and trade stream to drive simulations. This implies a clear separation between data ingestion (wrangling your B3 order‑book events and trades from polars/parquet into the Nautilus book representation) and the matching/fill logic.  

The presence of multiple book types and a dedicated order‑book API in the Python bindings, along with the Rust‑side book methods, makes NautilusTrader suitable for deep‑book replay when you implement a custom adapter that maps your B3 events into its canonical `OrderBook` representation.

### Fill models, queue/partial fills, and determinism

NautilusTrader ships with a suite of configurable fill models that control how limit and market orders interact with the book, slippage assumptions, and synthetic liquidity. The fill‑model reference lists a range of models including `DefaultFillModel`, `BestPriceFillModel`, `OneTickSlippageFillModel`, `ProbabilisticFillModel`, `TwoTierFillModel`, `ThreeTierFillModel`, `LimitOrderPartialFillModel`, `SizeAwareFillModel`, `CompetitionAwareFillModel`, `VolumeSensitiveFillModel`, and `MarketHoursFillModel`, each encoding specific behaviors about how much volume is available at best price versus worse levels, and how fills are split or tiered.  

These models correspond to realistic microstructure assumptions such as partial fills at best price and additional volume at worse prices (e.g. `LimitOrderPartialFillModel` and tiered models) and competition for liquidity (`CompetitionAwareFillModel` and `VolumeSensitiveFillModel`). For example, tiered models distribute an order across multiple price levels (like 10 units at best, remaining size one tick worse), which approximates partial fills and non‑linear book shape.  

The fill‑price and matching documentation states that simulated fills do not mutate the historical book (“Order Book Immutability”) and that a fixed `random_seed` in a fill model makes probabilistic decisions repeatable. This directly addresses determinism: given the same input data, configuration, and seed, the engine will produce identical fills for probabilistic models (e.g., probabilistic choice between best price and one‑tick‑worse).  

Supported order types include `MARKET`, `MARKET_TO_LIMIT`, `LIMIT`, `STOP_MARKET`, `STOP_LIMIT`, `MARKET_IF_TOUCHED`, and `LIMIT_IF_TOUCHED`, each with defined matching rules such as walking crossed book levels for market‑style orders or resting remaining quantity at the first fill price for `MARKET_TO_LIMIT`. Market‑style orders and certain triggered orders can be configured to fill any residual quantity one tick worse, further controlling slippage at execution.  

Altogether, these features enable modeling partial fills, competition for book liquidity, different order types, and slippage patterns with deterministic behavior under fixed seeds—capabilities which go well beyond a fixed 2‑tick slippage rule.

### Replay speed and adapter effort

NautilusTrader’s design exposes order books and fill simulation via Python and Rust, and its marketing emphasizes performance for backtesting and intraday simulation, with book objects and matching logic implemented in compiled code rather than pure Python loops. Given this architecture, replaying a single full session of deep‑book data on a modern desktop should be comparatively fast: the hot path (book updates and fill simulation) runs in compiled code, while Python orchestrates the strategy logic.  

Typical adapter effort to map your existing polars/parquet B3 datasets into NautilusTrader’s book and trade abstractions would likely be on the order of a few focused days for an experienced Python developer familiar with your data schema and the library’s APIs (this estimate is based on practical experience rather than specific documentation).

---

## 2. Vectorised research: vectorbt (open vs PRO), backtesting.py, and polars‑native

### vectorbt (open‑source) for bar‑based research

The open‑source vectorbt library is described as a backtesting engine that “packs thousands of configurations into NumPy arrays, accelerates the hot path with Numba and Rust, and runs them all at once, turning hours of grid search into seconds.” It explicitly “takes a radically different approach to backtesting: instead of looping through bars one strategy at a time, it packs configurations into arrays,” making it well‑suited to vectorised research on bar data such as your 1‑minute OHLCV bars.  

Vectorbt is positioned as the community edition of the broader VectorBT PRO ecosystem. It integrates with NumPy, pandas, and similar scientific Python tools, giving you a natural environment for research where strategies are expressed as array operations over bar data rather than event‑driven intraday simulations.

### VectorBT PRO extensions

VectorBT PRO is presented as “a high‑performance, production‑grade Python engine for backtesting, algorithmic trading, and quantitative research,” representing trading systems as multidimensional arrays and evaluating large parameter spaces “at C speed” while integrating with NumPy, pandas, Plotly and the wider Python scientific stack. The PRO documentation emphasizes a full workflow to “acquire and align data, build indicators and signals, simulate portfolios, validate across time and parameters, and analyze the result through one connected workflow.”  

Compared to open‑source vectorbt, PRO adds features such as automated cross‑validation, combining indicators from multiple timeframes, splitting backtests into batches to reduce memory use, simulating limit orders and stop ladders, portfolio optimization, and ongoing/streaming simulations that continue an existing portfolio as new data arrives. These capabilities make PRO attractive if you need production‑grade tooling for multi‑timeframe research, more realistic limit‑order simulation on bar data, and large‑scale parameter sweeps beyond what the open edition offers.

### backtesting.py for bar‑level strategy prototyping

Backtesting.py is described as “a small and lightweight, blazing fast backtesting framework that uses state‑of‑the‑art Python structures and procedures (Python 3.6+, Pandas, NumPy, Bokeh).” The library’s documentation states that it is “a Python framework for inferring viability of trading strategies on historical (past) data” and that it can “backtest any financial instrument for which you have access to historical candlestick data.”  

It wraps a pandas `DataFrame` (OHLCV data) and a `Strategy` subclass into a backtest object, providing methods like `Backtest.run()` and `Backtest.optimize()` to execute and optimize strategies. Features include “blazing fast execution,” a built‑in optimizer based on SAMBO, and a library of composable base strategies and utilities.  

Backtesting.py’s typical usage pattern calls your `Strategy.next()` once per bar, where you place orders via helper methods (e.g., `self.buy()`/`self.sell()`), and the engine handles fills according to its internal rules and your commission/execution parameters. This makes it straightforward for 1‑minute bars and medium‑frequency strategies that do not need detailed order‑book simulation, while still providing rapid parameter sweeps and visualizations through Bokeh.

### Polars‑native vectorised approaches

Polars itself is a fast columnar DataFrame library with an expression engine that is well‑suited to vectorised calculations on large datasets; using polars directly for backtesting means writing your own bar‑based logic (signals, positions, P&L) as expressions and groupby operations over your 1‑minute bars. (This is based on general knowledge of polars and not on the specific search snippets.)  

In practice, polars can act as the underlying data engine while vectorbt or backtesting.py handle strategy orchestration, or you can build a custom bar‑backtester directly in polars if you are comfortable designing your own portfolio and execution logic.

---

## 3. Thin in‑house replayer (trades + L2 snapshots, fixed 2‑tick slippage)

### What a thin replayer cannot model vs Nautilus

A thin in‑house replayer that only consumes trades and L2 snapshots with a fixed 2‑tick slippage rule typically lacks:

1. Queue position and competition modeling  
   NautilusTrader’s fill models explicitly incorporate competition and volume sensitivity through models such as `CompetitionAwareFillModel` (configurable fraction of large size at best) and `VolumeSensitiveFillModel` (placing a percentage of internal volume at best). A simple 2‑tick rule does not account for where your order sits in the queue or how other participants’ orders affect your fill probability.  

2. Partial fills and tiered liquidity  
   NautilusTrader provides `LimitOrderPartialFillModel` and tiered models (`TwoTierFillModel`, `ThreeTierFillModel`) that allocate parts of your order across price levels, modeling partial fills at best price and additional volume one tick worse. A fixed slippage model cannot reproduce such nuanced fill splits, especially when top‑of‑book volume is limited relative to your order size.  

3. Order‑type behavior and triggers  
   Supported order types in NautilusTrader include market, market‑to‑limit, limit, stop‑market, stop‑limit, market‑if‑touched, and limit‑if‑touched, each with defined matching rules (e.g., walking crossed levels after triggering, resting remaining size at first fill price). A thin replayer generally treats orders as simple market or limit orders with uniform slippage, ignoring trigger conditions, resting logic, and the interaction of stops and touches with the evolving book.  

4. Deterministic probabilistic fills tied to book state  
   NautilusTrader’s “Order Book Immutability” ensures that simulated fills do not edit historical book, and a fixed `random_seed` in fill models makes probabilistic decisions repeatable. This means you can combine probabilistic microstructure assumptions (e.g., stochastic slippage) with deterministic reproducibility for research and risk analysis, which a simplistic replayer rarely offers.  

5. Rich configuration for market hours and synthetic spreads  
   Models like `MarketHoursFillModel` extend the simulation with different spread assumptions inside and outside market hours, including normal or one‑tick‑wider synthetic spreads. A fixed 2‑tick slippage ignores how spreads and liquidity change intra‑day or in different regimes, limiting your ability to test strategies across realistic liquidity conditions.  

6. Integrated venue/account configuration and OMS behavior  
   The backtest venue configuration includes OMS type (e.g., netting), account type, book type, starting balances, and fill model, tying together book behavior, execution, and portfolio accounting. A minimal replayer often decouples these concerns or omits them entirely, leading to backtests that model price moves but not realistic portfolio accounting under different execution regimes.

### Does this matter at 1–5 minute horizons with small sizes?

For small order sizes relative to typical top‑of‑book volume and 1–5 minute horizons, the impact of precise queue position, competition, and fractional fills on overall P&L is often modest compared to directional risk and spread/fee drag (this conclusion is based on general trading practice rather than specific documentation). At such horizons, bar‑based models with a simple slippage rule frequently capture most of the edge and risk characteristics relevant to your strategies, especially if holding times span multiple minutes and you avoid trading at liquidity extremes.  

Where detailed microstructure modeling becomes critical is when:

- Your clip sizes are large relative to displayed depth.  
- Your edge relies on very short holding times (seconds) or microstructure patterns.  
- Execution quality (fill vs miss, partial vs full, queue position) significantly drives outcome variability.  

If your intended B3 futures strategies on WIN/WDO at 1–5 minute horizons use small sizes and do not rely on such microstructure nuances, the thin replayer’s limitations may not materially affect backtest conclusions—though they still reduce confidence when you scale up size or move closer to the microsecond/millisecond regime.

---

## 4. Recommendation and the single fact that would reverse it

### Recommended stack

Given your context—B3 futures (WIN, WDO) with rich intraday datasets (order‑book events, trades, L2 snapshots) and 1‑minute bars—the most balanced approach is:

1. Use NautilusTrader for intraday, event‑level strategies  
   - It provides a structured backtest venue configuration with OMS, account type, book type, starting balances, and configurable fill models.  
   - Its order‑book and fill APIs (`simulate_fills`, book objects) are designed for deep‑book replay and deterministic matching under fixed random seeds.  
   - Fill models encode partial fills, competition, volume sensitivity, market‑hours behavior, and various order types, giving you realistic intraday execution modeling that your thin replayer cannot match.  

2. Use a vectorised bar‑research stack for higher‑level research  
   - vectorbt (open‑source) gives you array‑based backtesting and parameter sweeps over 1‑minute bars, turning large grid searches into seconds via NumPy, Numba, and Rust.  
   - VectorBT PRO adds limit‑order/stop‑ladder simulation, multi‑timeframe indicator combination, portfolio optimization, streaming simulations, and more, extending the research workflow if you need those capabilities.  
   - backtesting.py offers a lightweight, pandas‑centric OHLCV backtesting framework with fast execution, a built‑in optimizer, and composable strategy utilities, ideal for rapid prototyping and small research projects on 1‑minute bars.  
   - Polars can serve as the underlying data engine for both intraday and bar‑based research, or as the basis for custom vectorised backtests, while NautilusTrader and vectorbt/backtesting.py provide higher‑level execution and strategy orchestration.  

This combination lets you model microstructure and execution for intraday strategies where it matters (using NautilusTrader) while preserving the speed and flexibility of vectorised research on 1‑minute bars, using tools optimized for array‑based backtesting and parameter sweeps (vectorbt and backtesting.py).

### Single fact that would reverse the recommendation

The single fact that would reverse this recommendation is:

> A robust, statistically sound study on your own B3 futures strategies showing that backtests run with NautilusTrader’s microstructure‑aware order‑book and fill models (including partial fills and competition models) produce P&L, drawdown, and risk metrics that are indistinguishable from results obtained using a thin in‑house replayer with trades + L2 snapshots and fixed 2‑tick slippage at your target sizes and 1–5 minute horizons.

If that fact were established (i.e., microstructure‑rich simulations add no measurable predictive or risk‑management value for your strategies vs the thin replayer), then the simpler stack—using a thin in‑house replayer and vectorised bar‑based tools everywhere—would become the rational choice.
