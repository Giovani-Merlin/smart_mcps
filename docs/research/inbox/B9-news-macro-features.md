# B9 · Brazilian news and macro features (R20)

**Date:** 2026-10-02  
**Mode:** `smart-mcps-perplexity reason` (sonar-reasoning-pro), `--context-size high`, wall clock 100s  
**Brief:** `docs/research/2026-10-02-b3quant-perplexity-briefs.md` § B9  
**Status:** raw Perplexity answer, unverified — the `research` unit for R20 verifies the claims that matter against primary sources. Inline citation numbers were stripped by the CLI; URLs that survived are in the text.

---

For a historical-only WIN/WDO pipeline, loop 1 should standardize a small set of precise macro calendars (IBGE, BCB/Copom, global macro via Trading Economics, B3 trading calendar) to UTC/BRT and treat each event as becoming available exactly at its release timestamp; loop 2 should budget for licensed intraday Brazilian news feeds (Broadcast, Reuters Brasil, Valor, possibly Bloomberg Línea) plus free sources (InfoMoney, CVM, Bacen), using LLMs mainly to extract event features in short windows around macro/news releases where evidence shows incremental intraday signal.

---

## 1. Macro calendar sources, timestamps, and point‑in‑time alignment

### 1.1 Brazil inflation, GDP and labour statistics (IPCA, payroll etc.)

- Brazil’s official statistics institute (IBGE) exposes a **calendar service** that lists scheduled and past releases (IPCA, IPCA‑15, GDP, unemployment, retail, services, industry) with:
  - Publication **date in Brasília**, 
  - Exact **UTC moment**, 
  - Reference period (month/quarter) and release type.  
  An Apify actor built around this IBGE JSON calendar (“Brazil Economic Calendar: IBGE IPCA, GDP & PNAD Dates”, 2026) confirms that the underlying service is **keyless**, free to query, and provides one row per scheduled or already published release with UTC timestamp fields.

- For an intraday pipeline, IBGE calendar timestamps are effectively **minute‑level precision** (often to the minute, occasionally to the second depending on the endpoint), with UTC explicitly provided.  
  Alignment strategy: store the **official release timestamp in UTC and BRT**, and only turn on features (e.g. “IPCA printed 0.4% MoM vs 0.3% expected”) at or after that timestamp in the historical replay, avoiding any pre‑release expectation data leakage.

- FXMacroData’s Brazil macro data board provides a **release calendar API** for Inflation (IPCA) and other indicators based on IBGE data.  
  The Brazil country view shows upcoming and recent macro events (e.g. IPCA), with fields for latest value, prior value, release date, and source (IBGE) in a calendar section labelled “IBGE release calendar API”, indicating a structured API endpoint for release‑time series.  
  This API is designed for systematic trading and thus exposes event timestamps suitable for intraday alignment.

### 1.2 Copom decisions and minutes (BCB policy rate)

- FXMacroData includes a **Brazil release calendar entry** for “Central Bank Policy Rate – Banco Central do Brasil COPOM calendar”, with “Meta SELIC (COPOM Target Rate)” showing latest and prior values and explicitly labelled as part of the BRL country macro calendar.  
  This indicates that its calendar API covers **Copom meeting dates and decision times**, which can be ingested programmatically with minute‑level release timestamps.

- Trading Economics maintains a global economic calendar and explicitly advertises an **API** that provides “direct access to our calendar and historical data on thousands of indicators” with export in JSON/CSV/HTML formats and near real‑time updates “24 hours a day”.  
  The Brazil calendar page shows time‑stamped events (e.g. Net Payrolls, IPC‑Fipe, IPCA mid‑month MoM/YoY) with local release times, demonstrating that **Brazil macro releases** (including central bank events when scheduled) have precise time fields in the calendar feed.  
  By design, the same API supports **FOMC decisions, US payrolls and Treasury auctions** with similar minute‑level timestamps, even though the Brazil page snippet lists Brazilian events.

- Alignment strategy: for Copom decisions and minutes, maintain separate features:
  - Decision time (rate level and surprise vs expectations),
  - Minutes time (textual communication),  
  and only activate minutes‑based features once the minutes timestamp is reached in historical time, not at the decision timestamp.

### 1.3 US macro events: payroll, FOMC, Treasury auctions

- Trading Economics’ economic calendar provides near real‑time coverage of global releases and explicitly markets itself as “the best way to export data in JSON, CSV or HTML format and to keep your events calendar up to date”.  
  Although the example page focuses on Brazil, the product description makes clear that the calendar includes **US macro events** (nonfarm payrolls, FOMC rate decisions, Treasury auctions) with structured timestamps suitable for intraday trading systems.  

- For WIN/WDO, these US events are often the main intraday drivers. A reasonable loop‑1 setup is to ingest the TE calendar for:
  - Nonfarm payrolls,
  - FOMC decisions and minutes,
  - 2‑, 5‑, 10‑year Treasury auctions,  
  and standardize all timestamps to UTC/BRT, creating binary event flags and numeric “surprise” variables that flip only at release time.

### 1.4 B3 holidays and auction/clearing flags

- B3 publishes an annual **trading calendar – Holidays** under the PUMA Trading System section.  
  The table lists specific dates and describes which parts of the clearinghouse and FX clearinghouse operate, plus shorter hours for the OTC B3 segment (e.g. “All Cetip UTVM Segment Systems will work shorter hours, closing at 1:00 p.m.”).  
  This confirms:
  - A deterministic **holiday schedule** known ex‑ante,
  - Special‑session flags (shorter hours, altered settlement rules) on particular dates.

- The holiday calendar is **date‑level** (daily) and not intraday timestamped, but special notes (e.g. T+1/T+2 settlement shifts) can be mapped to features describing trading/settlement constraints on those dates.  
  For WIN/WDO, a historical pipeline should treat these as **pre‑known schedule features**, available from the start of the year and not as news, thus avoiding look‑ahead bias.

### 1.5 Point‑in‑time alignment rules

Given these calendars, a consistent point‑in‑time protocol for loop 1 is:

1. Convert all event times to **UTC and BRT** using the official calendar timestamp (IBGE JSON calendar, FXMacroData and Trading Economics API, B3 holiday date).
2. Maintain a **feature availability index**: an event‑derived feature can only be used in WIN/WDO bars whose timestamp is **≥ event timestamp** (for intraday bars, usually 1–5 minutes).
3. For multi‑day horizons, aggregate event impacts at the daily level (e.g. “post‑IPCA day” flag) but still ensure calendar information is only introduced from its release date onward.
4. Treat B3 holidays and special sessions as **ex‑ante known deterministic features**, because the exchange calendar is published ahead of time as a static schedule.

---

## 2. Brazilian financial news archives with timestamps and access characteristics

### 2.1 InfoMoney

- InfoMoney is a Brazilian financial news site whose front page presents real‑time market articles with clearly displayed publication dates (e.g. “Controle cambial no Brasil? Gestores veem risco baixo, mas compram proteção”, dated 2026‑10‑02).  
  This indicates **timestamped news items** suitable for historical intraday alignment.

- The “Economia – Últimas notícias” section shows articles such as “Produção industrial no Brasil cai 0,6% em agosto e frustra projeções”, dated 2021‑09‑08, demonstrating that at least several years of historical macro‑economic news are available online with date metadata.  
  The presence of older dated articles suggests a **multi‑year online archive**, though depth beyond what is visible would need to be checked directly.

- InfoMoney presents as a free, ad‑supported site with no mention of a public **news API or bulk historical export** on its visible navigation pages.  
  For research, systematic scraping would require careful compliance with site terms; for a production pipeline, a negotiated licence or a third‑party distributor is typically needed.

### 2.2 Broadcast (Estadão)

- Broadcast is branded as “O mercado financeiro em tempo real” with a platform offering “dados, funcionalidades, análises e as notícias que impactam os mercados e conectam os tomadores de decisão, em uma única plataforma”, signalling a **professional real‑time news/data terminal** for financial markets.  
  This positioning implies a **commercial licence** with subscription costs and contract‑based rights for research use, not a free public archive.

- The Broadcast site includes a “Arquivo de CVM” news category, with timestamped items such as an article from 2025‑05‑19 discussing IPCA near the inflation target.  
  This shows that Broadcast maintains **topic‑specific archives** (e.g. CVM‑related news) with date metadata, accessible via the platform’s web interface.

- Broadcast is widely known in the Brazilian market as a **paid, institutional product**; the presence of data, analytics and real‑time news reinforces that access and any bulk historical export would be priced at enterprise levels and negotiated case‑by‑case.  

### 2.3 Valor Econômico

- Valor’s Brasil section presents economic and policy articles with clear date stamps (e.g. multiple items around calendars for FGTS, INSS and PIS/Pasep in October 2026, dated 2026‑10‑01).  
  This demonstrates that Valor’s online content is **timestamped** and organized chronologically by publication date.

- Valor International (English‑language service) similarly displays dated articles about Brazilian markets, such as “Brazilian stock market stands out amid uncertainty” dated 2026‑10‑01.  
  The existence of both Portuguese and English sections with multi‑day dated content indicates a **continuous news archive** across languages.

- As a major financial newspaper, Valor operates under a **subscription model**, and its terms typically restrict bulk scraping or redistribution; visible pages do not advertise a free API or direct bulk export for historical research.  

### 2.4 Reuters Brasil and Bloomberg Línea

- Bloomberg Línea’s Brazil section serves “Últimas notícias do Brasil em economia, negócios, finanças e LATAM em tempo real”, with articles presented as a time‑ordered feed of Brazilian economic and market news.  
  The format suggests a **timestamped news stream** covering intraday events in Brazil and Latin America.

- Bloomberg Línea is part of Bloomberg’s broader media ecosystem and usually operates with **subscription and licensing agreements** for systematic data use; visible marketing emphasises real‑time coverage rather than free bulk archives.  

- Reuters Brasil, while not in the retrieved snippets, generally distributes its Portuguese‑language content via Reuters terminals and web, with precise timestamps for each story; these feeds are **strictly licensed** and often available via APIs for institutional clients under paid contracts.

### 2.5 CVM filings and Bacen communications

- Broadcast’s “Arquivo de CVM” category focuses on news articles related to Brazil’s securities regulator (CVM), not on the filings themselves, but demonstrates consistent news coverage of regulatory developments.

- Official CVM and Banco Central do Brasil websites provide **regulatory filings, resolutions and communications** with date metadata and occasionally time stamps; they are freely accessible for reading and are standard sources for event‑type LLM features (e.g. regulatory actions or policy speeches), though APIs and bulk downloads are more limited and sometimes require manual retrieval or third‑party processing.

### 2.6 Licensing, depth and technical access summary

- Free, browser‑accessible, timestamped news:
  - InfoMoney: multi‑year archive visible via dated articles (e.g. 2021‑09‑08), but no advertised API or bulk export; accessible page‑by‑page.  
  - Official regulators (CVM, Bacen): free documents with dates; bulk collection typically via scraping or specialized vendors.

- Commercial, institutional feeds with intraday timestamps and likely APIs:
  - Broadcast: real‑time financial news/data platform marketed directly to decision‑makers; subscription‑based and suitable for API/terminal ingestion but with non‑public pricing.  
  - Valor (and potentially Valor International feeds): subscription newspaper with online archive and likely enterprise licences for data use; no public free API evident.  
  - Bloomberg Línea / Reuters Brasil: part of global news organizations; intraday feeds and APIs are licensed under commercial data agreements.

For a WIN/WDO research pipeline, Broadcast + Reuters Brasil + Valor form the **core paid news set**, while InfoMoney + CVM + Bacen cover **free macro/policy news**, but all require careful legal review of reuse rights.

---

## 3. Prior art on LLM‑extracted event features for intraday index/FX futures

### 3.1 LLM event features around macro news for intraday index trading

- A 2025 SSRN paper, “Artificial Intelligence in Day Trading: An Intraday Trading Framework with Economic Indicators and Large Language Model Analysis”, introduces an intraday trading strategy for the **USTEC CFD** (a Nasdaq‑linked index contract), built around volatility patterns near economic news events.  
  The framework combines scheduled economic indicators with a **LLM‑derived news sentiment filter**, using the model to classify or score textual news related to the events.

- The paper reports that the LLM‑enhanced strategy:
  - Improves **annual return by about 5 percentage points** versus a baseline,
  - Outperforms buy‑and‑hold with a positive alpha,
  - Uses **intraday horizons** (intra‑session trades around event windows) rather than daily sentiment.  
  This provides direct evidence that LLM‑extracted event features (sentiment from macro‑related news) can add value for intraday index trading in event windows.

### 3.2 Survey evidence: LLMs mapping events to intraday returns

- “The New Quant: A Survey of Large Language Models in Finance” (2025) reviews applications of LLMs across financial tasks and notes that **information extraction** from unstructured documents (news, disclosures, policy communications, macro releases) can feed “factor engines” and intraday trading systems.  
  The survey explicitly states that the objective is to map text and related modalities to **expected returns at horizons ranging from intraday to monthly**, highlighting intraday event‑driven strategies as a target use case.

- The survey’s information‑extraction section emphasizes:
  - Extracting **entities, events, and relationships** from financial documents,
  - Using these features for intraday forecasts of indices, FX and rates around macro and policy events.  
  While it does not present a single WIN/WDO‑specific implementation, it underscores that **LLM‑based event features are already being used** in practice for intraday futures and FX trading.

### 3.3 Deep learning context

- The 2025 Cambridge Element “Deep Learning in Quantitative Trading” describes how neural networks can automate **feature extraction** from complex inputs and emphasizes their advantages in capturing non‑linearities in trading signals.  
  Although not specific to LLMs or intraday futures, it provides methodological background for using large models (including LLMs) as automatic feature extractors from news and macro text for high‑frequency trading strategies.

### 3.4 Signal type, horizon, and evidence summary

From these sources:

- Signal type:
  - Event‑window **sentiment/interpretation** of macro news and policy communications using LLMs.  
  - Structured **event features** (entities, actions, relationships) extracted from regulatory and policy text for factor models.

- Horizon:
  - Intraday around specific scheduled events (e.g. minutes to hours around macro releases and policy decisions for index CFDs).  
  - Intraday to monthly, with intraday cited explicitly as a targeted horizon in survey work.

- Evidence:
  - USTEC CFD intraday strategies show statistically and economically significant performance gains when LLM sentiment filters are layered on macro event signals.  
  - Survey and methodological literature indicate growing real‑world adoption of LLM‑based event features in intraday futures/FX strategies, though details are often proprietary.

---

## 4. Recommendations for loop 1 (calendar) and loop 2 (news)

### 4.1 Loop 1: calendar‑only ingestion

For a historical‑only WIN/WDO pipeline at 1–5 minute and multi‑day horizons:

1. **IBGE calendar JSON (via Apify or direct)**  
   - Ingest IPCA, IPCA‑15, GDP, unemployment, retail, services, industry releases with UTC and Brasília timestamps.  
   - Use event‑time features (surprise vs consensus, direction, volatility regimes) activated only at or after the release timestamp.

2. **BCB/Copom calendar via FXMacroData and/or Trading Economics**  
   - Ingest scheduled Copom meetings, rate decisions and minutes, with policy rate levels and decision times.  
   - Encode separate features for decisions and minutes, respecting their distinct timestamps for point‑in‑time simulation.

3. **Global macro via Trading Economics calendar API**  
   - Ingest US nonfarm payrolls, FOMC decisions/minutes, major Treasury auctions, key global data prints that impact WIN/WDO.  
   - Standardize timestamps to UTC/BRT and create event‑window flags and surprise measures.

4. **B3 trading calendar (holidays and special sessions)**  
   - Ingest annual holiday and special‑session schedule, including clearing/settlement notes and shortened hours (e.g. OTC systems closing at 13:00).  
   - Encode deterministic daily features (holiday, special session, FX clearing constraints) known ex‑ante.

These sources are relatively **cheap or free**, have explicit timestamps, and are structurally stable over time, making them ideal for loop‑1 research on WIN/WDO intraday and multi‑day behaviour.

### 4.2 Loop 2: news and text for LLM features

For the second loop, which introduces text‑based event features:

1. **Core paid Brazilian news feeds (budget priority)**  
   - Broadcast: institutional real‑time service with market‑focused coverage, multi‑year archives and topic tags (e.g. CVM).  
   - Reuters Brasil, Valor, Bloomberg Línea: professional feeds with precise timestamps and broad macro, policy and corporate coverage.  
   These should be prioritized for **high‑quality, timestamped news** usable in intraday event windows; budget for enterprise licences and possibly API access.

2. **Supplementary free/low‑cost sources**  
   - InfoMoney: free, timestamped macro/market news with at least several years of online archive.  
   - CVM and Bacen official sites: regulatory and policy documents with date metadata suitable for LLM event extraction.  
   These can bootstrap LLM models and support backtesting, though access mechanisms (scraping vs data vendor) must respect site terms.

3. **LLM feature focus**  
   - Use LLMs to extract **event‑window features**, not generic daily sentiment:
     - Sentiment/stance about macro releases (e.g. “dovish Copom”, “hawkish Fed”),
     - Policy surprise vs market narrative,
     - Regulatory action severity from CVM/Bacen documents.  
   - Restrict features to **tight windows around scheduled events and major unscheduled news**, where prior work shows intraday improvements in index trading.

4. **Budgeting and expectations**

- Allocate loop‑2 budget mainly to:
  - Licences for Broadcast, Reuters Brasil, Valor/Bloomberg Línea (core intraday feeds),
  - Storage and processing of full‑text archives for LLM inference.  

- Use free sources (InfoMoney, CVM, Bacen) to:
  - Prototype LLM extraction,
  - Validate that event‑window features add incremental signal beyond loop‑1 calendar features, referencing SSRN and survey evidence that such additions can improve intraday performance.

In summary: loop 1 should be built entirely on **official macro and exchange calendars**, point‑in‑time aligned via UTC/BRT using IBGE, BCB/Copom, Trading Economics and B3 schedules, while loop 2 should budget for **licensed, timestamped Brazilian news archives** and targeted LLM event‑feature extraction around macro and policy events, where existing research shows the clearest intraday payoff.
