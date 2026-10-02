# B7 · chart patterns and support/resistance evidence (R18)

**Date:** 2026-10-02  
**Mode:** `smart-mcps-perplexity reason` (sonar-reasoning-pro), `--context-size high`, wall clock 84s  
**Brief:** `docs/research/2026-10-02-b3quant-perplexity-briefs.md` § B7  
**Status:** raw Perplexity answer, unverified — the `research` unit for R18 verifies the claims that matter against primary sources. Inline citation numbers were stripped by the CLI; URLs that survived are in the text.

---

The available post‑2010 evidence does not show a robust, cost‑adjusted predictive edge from head‑and‑shoulders–style chart‑pattern detectors on liquid index or FX futures, whereas there is at least some transparent out‑of‑sample work on intraday breakout and prior‑high/low strategies, albeit with edges that largely vanish once realistic costs are applied. Given a 2‑tick‑per‑side cost on WIN and WDO at 5‑minute and daily horizons, the balance of evidence favors investing your engineering time in systematic breakout parameterisation and rigorous walk‑forward testing rather than in building a Lo–Mamaysky–Wang–style chart‑pattern detector.  

---

## 1. Kernel‑regression chart‑pattern detection since Lo, Mamaysky & Wang (2000)

1. The Lo, Mamaysky and Wang (2000) framework introduced kernel regression–based detection of canonical patterns (including head‑and‑shoulders) and showed in‑sample predictability primarily on equities, not futures, using mid‑20th‑century U.S. data; the current set of post‑2010 futures references is dominated by practitioner work rather than direct, peer‑reviewed successors.  
2. A representative post‑2010 attempt to build a chart‑pattern‑based futures trading algorithm is the “Fractal Formation and Trend Trading Strategy in Futures Market” paper (scribd.com/document/428834535/chaos, 2015), which proposes a Mandelbrot/Williams‑style fractal formation as a basic indicator and describes a trend‑trading algorithm built around that pattern.  
3. The fractal‑pattern paper positions its formation as an alternative to combinations of moving averages, Elliott waves, and MACD, but the publicly available version is descriptive and does not document a large‑scale, cost‑adjusted walk‑forward evaluation on liquid index or FX futures comparable to modern quantitative standards.  
4. Contemporary chart‑pattern compilations such as “Book 10 Top Chart Patterns” (scribd.com/document/356433337/Book-10-Top-Chart-Patterns, 2025) and online adaptations of “Technical Analysis of the Futures Markets” (onetrend.app/books/en/technical-analysis-of-futures-markets.html, 2026) still treat head‑and‑shoulders, M‑tops/W‑bottoms, triangles and gaps as qualitatively “reliable” but present only illustrative charts and rules of thumb, not out‑of‑sample, cost‑adjusted performance statistics on futures.  
5. The practical testing work that is available tends to cut against a strong chart‑pattern edge: the “Strategy Lab #3 — The Signals That Don’t Work” series (algorithmictoken.substack.com/p/strategy-lab-3-the-signals-that-dont, 2026) reports that a range of popular technical signals and pattern‑like triggers fail to retain statistically meaningful profitability once subjected to rolling parameter selection and 3‑month out‑of‑sample windows, particularly after including commissions and slippage.  
6. Similarly, the “intraday-cost-study” project (github.com/padmesharavindan5-dot/intraday-cost-study, 2026) emphasizes that when intraday strategies are evaluated with realistic costs and walk‑forward testing, the resulting performance is “indistinguishable from zero,” leading to the explicit conclusion of “no demonstrable edge after costs” rather than guaranteed losses.  

**Implication for WIN/WDO:**  
For B3 index and FX futures, where tick size and two‑tick‑per‑side cost create a meaningful hurdle, the absence of well‑documented, post‑2010 futures studies showing chart‑pattern detectors with persistent, out‑of‑sample edge strongly suggests that a Lo–Mamaysky–Wang‑style head‑and‑shoulders detector is unlikely to survive realistic cost and regime changes at 5‑minute horizons.  

---

## 2. Support and resistance definitions with out‑of‑sample evidence

### 2.1 Prior‑session high/low

1. The NinjaTrader educational note “How to Identify Intraday Support and Resistance Levels in Your Trading” (ninjatrader.com/futures/blogs/how-to-identify-intraday-support-and-resistance-levels-in-your-trading/, 2023) highlights prior‑day high, low and close (OHLC) as key levels where buying or selling pressure previously exhausted and suggests that these levels “often act as strong support and resistance” intraday, providing standard indicator tools to mark them.  
2. A data‑driven example is the “nifty-daily-moves-and-gaps” project (github.com/ashwanthkumar/nifty-daily-moves-and-gaps, 2026), which analyzes index futures behavior around prior‑day high (PDH) and prior‑day low (PDL) and reports that entering on the first 5‑minute breach of PDH/PDL with a 2.3% stop‑loss and 0.3% trailing stop produces more profitable outcomes than waiting for end‑of‑day confirmation, implying an intraday breakout edge anchored on prior‑session levels.  
3. The same project demonstrates that this edge is tied to short‑horizon momentum following the breach, not to static holding of positions, reinforcing that prior‑session high/low levels are most useful as breakout triggers rather than as passive “zones” for range‑trading.  

### 2.2 Pivot points (HLC‑based)

4. NinjaTrader’s article also formalizes classic floor‑trader pivot points as the average of the previous day’s high, low and close, with derived support (S1–S3) and resistance (R1–R3) levels, and frames them as intraday zones where price often reacts.  
5. While pivot‑point usage is widespread, the referenced material presents conceptual definitions and chart examples rather than large‑sample, cost‑adjusted out‑of‑sample performance metrics on index or FX futures.  

### 2.3 Swing highs/lows, gaps, and clustered levels

6. The “Support & Resistance in Futures Charts” chapter (scribd.com/document/457482622/Getting-Started-with-Technical-Analysis-3, 2025) discusses support and resistance zones formed by concentrations of relative highs and lows, emphasizing prior swing highs/lows and price zones with multiple turning points as more significant than single spikes.  
7. The same chapter notes that these concentration zones can be applied on weekly and daily futures charts to anticipate likely reaction levels, yet again provides primarily chart examples rather than rigorous statistical evidence.  
8. “Book 10 Top Chart Patterns” (scribd.com/document/356433337/Book-10-Top-Chart-Patterns, 2025) similarly describes unfilled price gaps as strong support/resistance and highlights M‑tops, W‑bottoms, double tops/bottoms and head‑and‑shoulders as patterns often associated with market turning points.  

### 2.4 Volume‑related levels and FAQs

9. The “Support and Resistance: FAQs for Futures Traders” article (damnpropfirms.com/prop-firms/support-resistance-faqs-futures-traders/, 2026) emphasizes prior swing highs and lows as “some of the clearest indicators” of support and resistance and notes that areas where selling or buying previously outweighed the other side often coincide with high traded volume, making them natural candidates for support/resistance levels.  
10. This FAQ, like the book chapters, reflects practitioner consensus (swing highs/lows and volume clusters matter) but does not supply an explicit out‑of‑sample backtest of volume‑profile nodes or round numbers on index or FX futures.  

**Implication for WIN/WDO:**  
Among the surveyed definitions, prior‑session high/low levels have the clearest documented intraday breakout usefulness (via the Nifty futures PDH/PDL study) at 5‑minute resolution, while pivot points, clustered swing highs/lows, gaps, and volume‑heavy zones are well‑motivated conceptually but lack transparent cost‑adjusted out‑of‑sample performance reports on index or FX futures; this points to PDH/PDL‑style levels as the strongest starting point for systematic support/resistance research on WIN and WDO.  

---

## 3. Intraday breakout strategies on futures with walk‑forward and costs

### 3.1 Opening‑range and session‑range breakouts

1. The “Intraday Mean Reversion and Breakout Models” article (openalgo.in/quant/intraday-mean-reversion-breakout, last updated 2026) describes the opening‑range breakout (ORB) as a “cleanest expression” of intraday trend: mark the high and low of the first 15–30 minutes, then trade the first decisive break of that range.  
2. A similar concept is presented in TMGM’s “Best Profitable Day Trading Strategies for Beginners & Pros” (tmgm.com/en/academy/trading-academy/best-profitable-day-trading-strategies, 2026), which recommends entering long when price breaks above the opening‑range high and short when it breaks below the opening‑range low, with stop‑losses at the opposite end of the range.  
3. The “London Breakout Strategy: 2,839 Days of NQ Futures Data” study (tradingstats.net/intraday-bias-session-analysis/, 2026) defines Asia and London session highs and lows on NQ futures (20:00–02:00 ET and 02:00–08:00 ET) and then evaluates breakout trades taken at 08:00 ET using those ranges, explicitly noting the long sample of 2,839 days and discussing how these session ranges translate into intraday bias and breakout behavior.  

### 3.2 Momentum‑filtered intraday breakouts

4. The “Intraday Momentum Breakout Strategy: A Volatility‑Targeted…” research report (misango.me/static/Papers/Intraday_Momentum_Paper/Intraday_Momentum_Research_Report.pdf, last updated 2026) specifies an intraday breakout framework on futures that combines price action with a 50‑period moving‑average direction filter, volatility targeting and a restricted trading window (09:30–15:00 ET), aiming to capture intraday momentum while controlling risk.  
5. This report’s focus on volatility targeting and time‑of‑day restrictions reflects a common pattern across more serious intraday breakout work: entries during the most liquid hours, position sizing keyed to volatility, and avoidance of low‑volatility “dead zones” where breakouts fail more often.  
6. The Tradeify article “Intraday Futures Volatility Analysis for Prop Firm Trading” (tradeify.co/post/intraday-futures-volatility-analysis-prop-firm-trading, 2026) supports this view by showing that the 09:30 ET open produces the widest intraday swings and highlighting that mid‑day stretches (11:30–13:30 ET) are where breakouts fail most frequently in the analyzed futures data.  

### 3.3 Walk‑forward evaluation and transaction costs

7. The “Strategy Lab #3 — The Signals That Don’t Work” series uses rolling in‑sample calibration and explicit out‑of‑sample windows (e.g., 3 months) to test intraday technical signals and concludes that many widely‑used breakout‑style triggers fail to produce statistically robust performance once tested in this walk‑forward manner.  
8. The “intraday-cost-study” GitHub project is more explicit: after including realistic commissions and slippage in intraday strategies and evaluating them with walk‑forward testing, its author reports that results are “indistinguishable from zero” and insists that the honest claim is “no demonstrable edge after costs,” rather than guaranteed losses.  
9. Combined, these practitioner research efforts show that while intraday breakout strategies can look attractive in naive backtests, much of the apparent edge is eroded by parameter instability and transaction costs, making net profitability highly sensitive to cost structure and trade frequency.  

**Implication for WIN/WDO with 2 ticks per side:**  
For B3 mini index (WIN) and mini FX (WDO) futures, intraday breakout strategies built on opening ranges, prior‑session ranges or momentum filters will face a substantial performance drag from a two‑tick‑per‑side cost, especially at 5‑minute resolutions where trade counts are high; nonetheless, breakout frameworks have clearer, testable rules and documented walk‑forward methodologies than chart‑pattern detectors, making them more suitable targets for a research harness that honestly accounts for costs.  

---

## 4. Go/no‑go decision: chart‑pattern detector vs breakout parameterisation

**Chart‑pattern detector (Lo–Mamaysky–Wang successors)**  

1. Available post‑2010 futures‑related sources focus on descriptive pattern catalogues (head‑and‑shoulders, M/W patterns, triangles, gaps) and fractal formations rather than on cost‑adjusted, out‑of‑sample validation of pattern detectors in index/FX futures.  
2. Where intraday technical signals and pattern‑like triggers have been subjected to walk‑forward testing with realistic commissions and slippage, the documented conclusion is that profits are “indistinguishable from zero,” with no demonstrable edge after costs.  
3. Implementing a Lo–Mamaysky–Wang kernel‑regression detector for multiple patterns on WIN and WDO would require non‑trivial engineering (template construction, smoothing, pattern libraries, regime handling) in exchange for a hypothesis that currently lacks strong futures‑specific, post‑2010 supporting evidence.  

**Breakout parameterisation (ranges and momentum)**  

4. In contrast, breakout strategies have clearly specified rule sets—opening‑range breakout, prior‑session range breakout (e.g., PDH/PDL), and session‑range breakouts like the London/Asia ranges—documented in public studies and write‑ups on index futures.  
5. There is at least some evidence that prior‑session high/low levels can provide intraday edges when used as breakout triggers with disciplined stops, as in the Nifty futures PDH/PDL analysis, though this edge must still be tested net of costs on B3 instruments.  
6. Methodologically, breakout research in the surveyed sources already uses walk‑forward windows, time‑of‑day filters, volatility targeting and explicit cost modelling, providing a template that you can adapt directly to WIN and WDO at 5‑minute and daily bars.  

**Recommendation for your harness (WIN/WDO, 5‑minute and daily, 2 ticks per side)**  

7. Given the lack of strong, post‑2010, cost‑adjusted evidence for head‑and‑shoulders and related chart‑pattern detectors on futures, and the explicit walk‑forward studies showing many technical signals collapsing to zero edge after costs, coding a sophisticated pattern‑recognition engine for WIN/WDO is a **no‑go** for a first research iteration.  
8. The comparatively clearer definition, existing rule sets and documented walk‑forward frameworks for opening‑range and prior‑range breakout strategies make systematic breakout parameterisation a **go**, with the caveat that you should expect only modest or zero net edge after a two‑tick‑per‑side cost and design the harness to test this honestly rather than to confirm a foregone profit expectation.  
9. Concretely, the most evidence‑aligned plan is to spend your engineering effort on:  
   - Prior‑session high/low and opening‑range breakout rules (PDH/PDL, ORB, session‑range) adapted to WIN/WDO.  
   - Volatility‑targeted position sizing and time‑of‑day filters inspired by the intraday momentum breakout research and volatility‑analysis work.  
   - Strict walk‑forward evaluation with realistic tick‑based costs and slippage, following the practices highlighted in the intraday‑cost and Strategy Lab studies.  

Under current evidence, this path maximizes learning about the true, cost‑adjusted behavior of WIN and WDO and minimizes investment in complex chart‑pattern detection machinery that has little demonstrated out‑of‑sample payoff on liquid futures.
