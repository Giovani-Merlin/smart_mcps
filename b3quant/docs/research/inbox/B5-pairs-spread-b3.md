# B5 · pairs and spread trading on B3 (R17)

**Date:** 2026-10-02  
**Mode:** `smart-mcps-perplexity reason` (sonar-reasoning-pro), `--context-size high`, wall clock 164s  
**Brief:** `docs/research/2026-10-02-b3quant-perplexity-briefs.md` § B5  
**Status:** raw Perplexity answer, unverified — the `research` unit for R17 verifies the claims that matter against primary sources. Inline citation numbers were stripped by the CLI; URLs that survived are in the text.

---

Brazilian evidence supports daily, cointegration‑based stock pairs on B3 and macro‑driven intraday futures spreads (especially WIN–WDO), while pure structural spreads like WIN–cash index and WDO–DOL are largely arbitraged away at realistic retail costs; under a two‑tick‑per‑side assumption, a profitable pair generally needs spread volatility of several ticks and half‑lives below a few dozen days, making daily stock pairs and WIN–WDO the most defensible baselines to implement first. Published Brazilian work rarely reports detailed Sharpe ratios or fully realistic cost assumptions, and there is essentially no B3‑specific Kalman‑filter performance evidence, so the harness should treat Kalman models as an experimental overlay on top of these better‑documented cointegration baselines.  

---

## 1. B3 stock pairs: cointegration vs Kalman hedge ratios

### 1.1 Cointegration‑based pairs on B3 (daily and intraday)

Key Brazilian academic/practitioner studies using cointegration on B3 stocks are:

1. **“Composition of portfolios by pairs trading with volatility criteria in the Brazilian market” – Revista Contabilidade e Finanças, 2021**  
   - Uses January 2016–December 2018 daily data for the 90 most liquid B3 stocks, generating 5,927,400 possible stock pairs.  
   - Applies the Augmented Dickey–Fuller residual test to identify cointegrated pairs and finds 126,350 cointegrated pairs over the sample.  
   - Compares portfolio construction rules and reports that portfolios formed from more volatile cointegrated pairs (20‑day volatility criterion) deliver “superior performance” relative to alternatives, indicating economically meaningful risk‑adjusted returns, although the abstract and summary tables do not clearly separate Sharpe ratios by pair type or fully describe slippage assumptions.  
   - The design is explicitly **daily**, not intraday, and focuses on generic liquid stocks rather than ON/PN or units vs components.  

2. **Caldeira & Moura (2013) – cointegration‑based pairs trading on the Brazilian equity market (UFRGS working paper referenced in Lume thesis)**  
   - A UFRGS thesis on pairs trading explicitly cites Caldeira & Moura (2013) as an empirical application of cointegration‑based pairs trading to the Brazilian equity market.  
   - The referenced study applies the Engle–Granger style cointegration approach to B3 stocks, constructing long–short, market‑neutral portfolios, but the thesis summary does not reproduce detailed Sharpe figures or a full transaction‑cost model.  
   - Available descriptions emphasise cointegration as an effective selection mechanism relative to simpler correlation or distance metrics, implying improved risk‑adjusted performance versus benchmarks, though exact Sharpe ratios for Brazilian data are not visible in the accessible summary.  

3. **“Portfólio de Pares via Cointegração de Alta Frequência” – UFRGS, mid‑2020s**  
   - This thesis explicitly describes a market‑neutral investment strategy (“estratégia de investimento… neutra ao mercado”) based on high‑frequency cointegration, indicating an intraday or ultra‑short‑horizon application to B3 assets.  
   - The snippet confirms the use of cointegration at high frequency and a neutral‑to‑market construction, but does not show specific performance metrics or the structure of transaction costs (commissions vs slippage), so Sharpe ratios and post‑cost returns cannot be inferred from the available extract.  

4. **“Arbitragem Estatística, Estratégia Long‑Short Pairs…” – Anpec article**  
   - This paper explains the generic Brazilian view of statistical‑arbitrage pairs: selling a relatively overvalued stock and buying an undervalued stock with the expectation that the price distortion will correct.  
   - It articulates the long–short, market‑neutral concept but the snippet does not tie the discussion to specific B3 datasets, Sharpe ratios, or intraday horizons.  

5. **QuantInsti project “Statistical Arbitrage: Pair Trading in the Brazilian Stock Market” – 2021 blog**  
   - Implements a statistical‑arbitrage pairs strategy on B3, explicitly modeling cointegration via Johansen tests within each sector of the Brazilian market.  
   - Selects only pairs that cointegrate at at least 90% significance, whose spread does not change sign, and whose half‑life is at most 60 days, directly tying tradability to mean‑reversion speed.  
   - The project focuses on **same‑sector B3 pairs** and illustrates profitable backtests, but the accessible summary emphasises half‑life, cointegration and qualitative performance without giving a full table of Sharpe ratios or a detailed slippage model.  

**Sharpe ratios and cost treatment.**  
Across these Brazilian sources, cointegration‑based stock pairs are reported as delivering “superior performance” versus simple alternatives, but publicly accessible summaries rarely provide clean, per‑pair Sharpe ratios or fully realistic intraday cost assumptions:

- The RCF 2021 paper clearly states that volatility‑selected cointegrated portfolios outperform other selection criteria, but it does not present a breakdown of Sharpe ratios by pair type in the section visible, nor does it explicitly describe slippage beyond generic transaction costs.  
- The UFRGS thesis referencing Caldeira & Moura (2013) confirms that cointegration‑based pairs trading was empirically applied to the Brazilian stock market, but the summary does not reproduce Sharpe figures or detail whether tick‑level bid–ask costs were deducted.  
- The high‑frequency cointegration thesis and the QuantInsti project both stress market‑neutrality and half‑life constraints, but the excerpts do not show a complete cost model, suggesting that slippage and intraday spread widening are at best approximated.  

As a result, the **credible takeaway** is that Brazilian cointegration studies find economically meaningful, market‑neutral returns on daily B3 data, with same‑sector pairs and fast‑reverting spreads (half‑life ≤ 60 days) showing the strongest evidence of tradability, but quantitative Sharpe and post‑slippage metrics remain under‑reported.  

### 1.2 Pair types: ON/PN, units vs components, same‑sector

Considering pair selection in the cited Brazilian work:

1. **Same‑sector stock pairs**  
   - The QuantInsti B3 project explicitly builds pairs within each sector (e.g., banks, commodities, utilities), then runs Johansen cointegration tests and filters by half‑life ≤ 60 days, directly indicating that sectoral similarity and fast mean reversion are central to its positive performance.  
   - The RCF 2021 large‑scale study considers all pairs among the 90 most liquid B3 stocks—5,927,400 combinations—and finds 126,350 cointegrated pairs, but the snippet does not partition results by sector or issuer type.  
   - Together, these sources show that **same‑sector pairs with demonstrably short half‑lives** are the best‑documented B3 pairs for daily cointegration‑based trading, even though detailed Sharpe ratios per pair are not reported in the excerpts.  

2. **ON/PN shares of the same issuer**  
   - ON and PN shares of a Brazilian issuer represent equity in the same firm with nearly identical cash‑flow fundamentals but different voting rights, making them natural cointegration candidates in theory; practice in Brazil often treats such pairs as structurally linked with low basis risk.  
   - However, none of the identified Brazilian studies summarised above explicitly report a systematic ON/PN pairs program with published Sharpe ratios or detailed cost assumptions, so **strong published evidence** for ON/PN pairs on B3 is lacking in the accessible literature.  

3. **Units vs components (e.g., units vs separate common/preferred lines)**  
   - The available Brazilian cointegration papers and theses focus on generic liquid stocks and sectoral groupings rather than units vs components structures, and no thesis or article in the retrieved set specifically studies unit/component spreads on B3 with reported Sharpe ratios.  
   - Any claim that unit/component pairs are superior would therefore rely more on structural reasoning than on published backtests; the current literature provides **better evidence for same‑sector pairs** than for units vs components in Brazil.  

Overall, the **best‑documented pair type** for B3 is **same‑sector stock pairs** with demonstrably short half‑lives, as in the QuantInsti project and the RCF 2021 cointegration study. ON/PN and unit/component pairs are intuitively attractive but lack comparable published, Sharpe‑documented Brazilian studies in the accessible sources.  

### 1.3 Kalman‑filter hedge ratios on B3

The question asks specifically about **Kalman‑filter hedge‑ratio pairs on B3** (daily and intraday). The available sources show:

- International quantitative literature describes Kalman filtering as a way to estimate a **time‑varying hedge ratio** between two assets, treating the hedge ratio as a hidden state in a linear Gaussian model.  
- The Portfolio Optimization book’s section on Kalman pairs trading (2025) outlines a state‑space model in which the hedge ratio \(\gamma_t\) evolves slowly, and the spread \(z_t = y_{1t} - \gamma_t y_{2t}\) is modeled as a mean‑reverting residual, but the examples are generic and not tied to B3 specifically.  
- Arbitragelab’s documentation explains a Kalman‑filter scheme that updates hedge ratios over time using a random‑walk transition for \(\beta_t\) in a linear regression, again in a global context rather than Brazilian data.  
- Other tutorials (Kalman‑filter.com, QuantFrame, SliceMatrix) demonstrate dynamic hedge‑ratio estimation and backtesting on generic equity/futures pairs, without referencing B3 or Brazilian tick costs.  

Crucially, **none of the identified Brazilian‑focused sources (RCF 2021, UFRGS theses, QuantInsti B3 project, Anpec paper)** explicitly report using a Kalman filter to estimate hedge ratios on B3 stocks, nor do they publish Sharpe ratios for Brazilian Kalman‑filtered pairs. The Brazilian literature so far is predominantly **cointegration with static hedge ratios**, with Kalman‑filter pairs trading appearing only in global methodological references and code libraries.  

---

## 2. B3 futures spreads: WIN, WDO, DOL, DI

### 2.1 Structural relationships of the contracts

Contract definitions and practitioner commentary establish the structural relations among the main B3 futures:  

1. **WIN (mini Ibovespa) vs cash Ibovespa (index/ETFs)**  
   - WIN is the mini Ibovespa futures contract whose underlying is the Ibovespa equity index, while IND is the full‑size Ibovespa future; both track the same index level.  
   - The cash Ibovespa index can be approximated by ETF baskets (e.g., Brazilian index ETFs) and constituent stocks; index‑arbitrage desks can buy the cash basket/ETF and sell WIN when the futures basis deviates from fair value (carry‑adjusted index level), or vice versa.  
   - Because this is a classical index‑arbitrage relationship, **persistent mispricings between WIN and the cash index are rapidly arbitraged away** by institutional and high‑frequency traders, leaving little room for a retail stat‑arb edge at a two‑tick‑per‑side cost beyond very short‑lived microstructure noise.  

2. **WDO (mini dollar) vs DOL (full‑size dollar)**  
   - WDO is the mini dollar futures contract and DOL is the standard dollar future; both reference the BRL/USD exchange rate with different contract sizes, but identical underlying.  
   - This shared underlying creates a tight mechanical linkage: any significant price discrepancy between WDO and DOL relative to their contract‑size ratio is an almost risk‑free arbitrage for professional desks.  
   - As a result, **WDO–DOL spreads are structurally arbitraged**, and systematic retail trading of this spread at the harness’s slippage cost is unlikely to yield durable profits outside of microsecond‑level execution advantages.  

3. **WIN–WDO (index vs FX)**  
   - Practitioner material for Brazilian intraday trading highlights WIN and WDO as a key macro pair: the Ibovespa responds to global equities, FX and interest rates, while the dollar future responds to DXY, emerging‑market FX and local rate differentials.  
   - Cohen (2026) explicitly lists WIN–WDO as an intraday pair of interest, noting a historically negative but regime‑dependent correlation between the equity index and FX, driven by shifts in risk appetite, rates and commodities.  
   - This is a **macro‑correlation pair, not a strict arbitrage**: there is no mechanical equality condition forcing WIN and WDO to move together, so spreads reflect changing macro views rather than mispricings. Retail traders can therefore design relative‑value or hedged directional strategies on WIN–WDO, and the spread is not fully arbitraged away by basis‑trading desks.  

4. **DI (interest‑rate futures) vs WDO (dollar futures)**  
   - DI futures represent Brazilian interest‑rate expectations, while WDO represents BRL/USD FX; practitioner commentary emphasizes that the equity index WIN reacts to DI moves and that WDO responds to global and local rate differentials.  
   - DI‑WDO therefore captures the interaction of local interest‑rate expectations and FX, but **there is no simple mechanical arbitrage condition** tying DI and WDO prices together; instead, macro desks may trade DI–FX relationships based on views about carry, risk premia and monetary policy.  

### 2.2 Which spreads are tradable vs arbitraged at retail cost?

Under a retail cost assumption of two ticks of slippage per side:

1. **WIN–cash index via ETFs**  
   - The WIN–cash index basis is a classic index‑arbitrage relationship; institutional desks with low latency and low cost continuously trade this spread, forcing the futures price to stay close to the cash index plus carry.  
   - At a two‑tick‑per‑side assumption and retail latency, this spread is **effectively arbitraged away**; persistent mispricings large enough to overcome 2 ticks per leg on entry and exit are rare and short‑lived, making it an unattractive baseline for a retail statistical‑arbitrage harness.  

2. **WDO–DOL (mini vs full dollar)**  
   - With identical underlying FX and different contract sizes, professional arbitrageurs align WDO and DOL prices, exploiting any mispricing via simultaneous long/short positions scaled to contract value.  
   - For a retail trader paying two ticks per side, systematic WDO–DOL spread trading is **not a robust stat‑arb opportunity**; most exploitable discrepancies will be smaller than the effective round‑trip cost.  

3. **WIN–WDO (index vs FX)**  
   - WIN–WDO is driven by macro correlations rather than strict parity; Cohen explicitly identifies this pair as a meaningful intraday relationship whose sign and magnitude vary with macro regimes.  
   - Because there is no structural arb desk forcing WIN–WDO to a tight band, and the spread reflects evolving macro views, **WIN–WDO remains tradable at retail costs** as a relative‑value or hedged directional pair, provided the strategy accounts for macro shifts and imposes reasonable half‑life and volatility filters.  

4. **DI–WDO (rates vs FX)**  
   - DI–WDO combines interest‑rate and FX exposures, with both contracts influenced by monetary policy expectations and global risk appetite, but without a mechanical equality constraint.  
   - This makes DI–WDO a **macro spread rather than a pure arbitrage**; retail stat‑arb is possible in principle but requires a robust macro model and careful consideration of margin and basis risk, with less empirical evidence available than for WIN–WDO.  

In short: **WIN–cash and WDO–DOL are structurally arbitraged spreads**, while **WIN–WDO and DI–WDO are macro‑correlation spreads that are still tradable at retail cost**, albeit with higher model and regime risk.  

---

## 3. Cost threshold: two ticks per side, required half‑life and volatility

Assume the harness charges **two ticks of slippage per side per order** and each pair trade involves **two legs (long one asset, short another)**. A full round‑trip (enter and exit) then incurs:

- Entry: 2 ticks on long leg + 2 ticks on short leg = 4 ticks  
- Exit: same again = 4 ticks  
- Total round‑trip cost \(C = 8\) ticks (in spread‑tick units).  

### 3.1 Break‑even volatility for a simple mean‑reversion rule

Consider a simple spread‑trading rule:

- Define the spread \(S_t\) between two assets and its rolling standard deviation \(\sigma\) in tick units.  
- Enter when the z‑score reaches \(+k\) (e.g., \(k = 2\)), meaning \(S_0 = k\sigma\), and exit when the spread reverts to its mean (approximate exit at \(S_T \approx 0\)).  

Ignoring drift and risk, the **gross expected price move** per leg is about \(k\sigma\) ticks from entry to exit. With two legs, gross spread PnL in tick units is approximately:

\[
\text{Gross PnL} \approx k\sigma
\]

The strategy breaks even when gross PnL equals total round‑trip cost \(C = 8\) ticks:

\[
k\sigma = C = 8
\Rightarrow \sigma = \frac{8}{k}
\]

For a typical choice \(k = 2\):

\[
\sigma_{\text{break-even}} = \frac{8}{2} = 4 \text{ ticks}
\]

So **a pair needs spread volatility of at least ~4 ticks** over the entry/exit horizon to **break even** against 8 tick round‑trip costs; volatility higher than 4 ticks is required to generate positive expected returns.  

### 3.2 Linking half‑life to annualised Sharpe

Let the spread follow an Ornstein–Uhlenbeck‑style mean‑reverting process with half‑life \(h\) (in trading days for daily data):

- Half‑life \(h\) relates to the mean‑reversion rate \(\lambda\) via \(\lambda = \ln(2)/h\).  
- A simple threshold strategy that enters once per mean‑reversion cycle will generate roughly \(N \approx 252/h\) trades per year on daily data.  

Approximate the **per‑trade expected net PnL** (in ticks) as:

\[
\Delta = k\sigma - C
\]

and assume per‑trade PnL volatility is of order \(\sigma\) ticks. A crude **single‑trade Sharpe** is then:

\[
s_1 \approx \frac{\Delta}{\sigma} = \frac{k\sigma - C}{\sigma} = k - \frac{C}{\sigma}
\]

The **annualised Sharpe** scales roughly with \(\sqrt{N}\):

\[
S_{\text{annual}} \approx s_1 \sqrt{N} = \left(k - \frac{C}{\sigma}\right)\sqrt{\frac{252}{h}}
\]

Plugging in \(k = 2\), \(C = 8\) and realistic \(\sigma\) values:

1. **Example 1: \(\sigma = 5\) ticks, \(h = 5\) days**  
   - Net per‑trade PnL: \(\Delta = 2 \times 5 - 8 = 2\) ticks.  
   - Single‑trade Sharpe: \(s_1 \approx 2/5 = 0.4\).  
   - Trades per year: \(N \approx 252/5 \approx 50\).  
   - Annualised Sharpe: \(S_{\text{annual}} \approx 0.4\sqrt{50} \approx 0.4 \times 7.07 \approx 2.8\).  

   A pair with **5‑tick volatility and 5‑day half‑life** is very attractive even with 8 tick round‑trip costs.  

2. **Example 2: \(\sigma = 5\) ticks, \(h = 20\) days**  
   - \(N \approx 252/20 \approx 12.6\).  
   - \(S_{\text{annual}} \approx 0.4\sqrt{12.6} \approx 0.4 \times 3.55 \approx 1.4\).  

   Still acceptable, but less compelling: **longer half‑life reduces the number of trades and Sharpe**.  

3. **Example 3: \(\sigma = 5\) ticks, \(h = 60\) days**  
   - \(N \approx 252/60 \approx 4.2\).  
   - \(S_{\text{annual}} \approx 0.4\sqrt{4.2} \approx 0.4 \times 2.05 \approx 0.8\).  

   With a **60‑day half‑life**, Sharpe falls below 1, highlighting why the QuantInsti B3 project cuts pairs with half‑lives greater than 60 days.  

These calculations show that, **at two ticks per side (8 ticks per round‑trip)**:

- Spread volatility must be **meaningfully above 4 ticks** to move beyond break‑even.  
- Half‑life should ideally be **well below 60 days** (for daily horizons) to achieve Sharpe > 1, with **5–20 day half‑lives** offering much more attractive Sharpe potential.  

The same logic applies to intraday 5‑minute data, with “days” replaced by appropriate intraday time units; spreads with very short intraday half‑lives and sufficient tick volatility can generate many trades per day, compensating for per‑trade costs.  

---

## 4. Recommended baselines to code first

Given the literature and cost structure, the most defensible starting baselines for your B3 harness are:

1. **Daily cointegration‑based stock pairs among the ~20 most liquid IBOV stocks (same‑sector focus)**  
   - This directly leverages the strongest Brazilian evidence: daily cointegration‑based portfolios on liquid B3 stocks with volatility‑based selection criteria and filters on half‑life.  
   - It is straightforward to implement:  
     1) Screen all pairs among the 20 liquid IBOV names for cointegration (ADF or Johansen).  
     2) Filter by half‑life (e.g., \(h \leq 60\) days) and sufficient spread volatility (e.g., \(\sigma > 4\) ticks given the 8‑tick cost).  
     3) Backtest simple z‑score entry/exit rules, then layer more complex risk controls.  
   - This baseline is well aligned with both the RCF 2021 and QuantInsti B3 designs and will give you immediately interpretable results relative to published Brazilian findings.  

2. **Intraday futures spread between WIN and WDO (5‑minute data)**  
   - WIN–WDO is explicitly highlighted by Brazilian practitioners as a meaningful intraday macro pair, with historically negative but regime‑dependent correlation.  
   - Unlike WIN–cash and WDO–DOL, WIN–WDO is not a mechanically arbitraged spread, so it retains residual relative‑value opportunities at retail cost, especially when modeled as a mean‑reverting spread or joint factor exposure.  
   - With 5‑minute data, you can:  
     1) Estimate a dynamic or static hedge ratio between WIN and WDO (starting with static, then experimenting with Kalman).  
     2) Construct the spread, measure intraday half‑life and volatility, and apply the cost‑aware filters derived above.  
     3) Evaluate whether intraday mean‑reversion in the spread is strong enough to beat the 8‑tick round‑trip hurdle.  

Once these two baselines are stable, a **third, more experimental baseline** would be to overlay a **Kalman‑filter hedge‑ratio** on the daily stock pairs and intraday WIN–WDO spread, using global methodological references for implementation while carefully backtesting under B3 cost assumptions, since no B3‑specific Kalman performance studies currently exist.
