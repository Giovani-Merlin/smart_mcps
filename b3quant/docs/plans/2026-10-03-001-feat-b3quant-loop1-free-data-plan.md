---
title: b3quant loop 1 — the ground on free data
type: feat
date: 2026-10-03
origin: docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md
---

# b3quant loop 1 — the ground on free data

## Objective

Deliver the ground of the B3 research programme on data that costs nothing:
a content-hashed parquet lake fed by the human's MT5 export and COTAHIST
(R1–R3), a point-in-time feature store (R6), the conservative cost model
(R7), the hash-protected walk-forward Evaluation Harness with its two KPI
Contracts and leakage probes (R8–R13), all nine research tracks verifying
their Perplexity inbox answer (R4, R14–R21), the breakout and pairs
baselines scored under the strategy contract (R22, R23), the swing and the
trades-only intraday forecast loops under the forecast contract (R24, R25
without its deep LOB candidate), the intraday strategy harness with its
engine kill test (R27), the gated RL environment (R28), the rule layer and
its loop (R29), and the closing Track Verdicts (R32). Loop dials (R31) are
the optimize units' declared `evaluations` and wall clocks; the driver
checklist (R33) is the `Run (driver):` items below.

**Deferred to plan 2, by decision** (`Decision · one plan or two at the order-book boundary`): `R5. book-ingest`, `R26. book-signals`, the
DeepLOB/TLOB-class second candidate of `R25. model-intraday`, and
`R30. forecast-combiner`; with them, R3's clause "B3's public trade files with participant codes … if R4 finds them live" (the parser belongs beside the broker-tagged signals it feeds) and R25's depth-dependent features (queue imbalance, multi-level microprice), which need an order book. Plan 2 is written once `U9. book-data-research`
has quotes in hand and the human has decided the purchase; its units need
the format, depth and price only that unit can find.

## What we already know (resolved context)

**Where this repo lives.** `b3quant/` is a subfolder of smart_mcps until the
human splits it into its own repository; every path in this plan is relative
to `b3quant/`, and every orchestrator command is run from inside it (`--repo`
defaults to the cwd): `cd b3quant && uv run --project .. smart-mcps-orchestrate …`.
The plugin's CLIs (`smart-mcps-orchestrate`, `smart-mcps-perplexity`) come
from the parent checkout's environment.

**The machine.** The run executes on the human's Windows desktop under WSL2
(Ubuntu, GPU passed through, about a terabyte). MetaTrader 5 runs on the
Windows side; its export is copied into `data/raw/mt5/` on the WSL side
before the run starts (`Decision · the MT5 export runs before the run`).
`data/` is `[workspace] data_dirs` in `.orchestrator/config.toml` (committed
on purpose; see `.gitignore`), so every worker worktree sees it uncommitted.

**What exists today** (tracked): `README.md`, `CONTEXT.md` (the glossary:
Instrument Universe, Continuous Contract, Dataset Manifest, Order Book, Tape,
Aggressor, Participant Code, Big-Player Imprint, Iceberg Order, Cost Model,
Fold Set, Held-out Year, Forecast Skill, Net Sharpe, Leakage Probe, Track,
Strategy, Track Verdict), `.envrc.example`, `.orchestrator/config.toml`
(`[recipes] enabled = ["research", "run", "evaluate", "optimize"]`,
`[workspace] data_dirs = ["data"]`, `[escalation] enabled = true`),
`docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md`,
`docs/research/2026-10-02-b3quant-perplexity-briefs.md`, and the nine raw
Perplexity answers `docs/research/inbox/B1…B9-*.md`. No Python exists yet:
every code path below is prospective.

**The recipes this plan uses** (smart_mcps `orchestrator/recipes/`):

- `research` (`ResearchArgs`: `question`, `output` under `docs/research/*.md`,
  `focus_paths`, `size`). The worker grounds in codegraph, writes a brief to
  `.coder-scratch/`, queries `smart-mcps-perplexity` (`WebSearch` fallback
  recorded as `provider_fallback`), and commits a Findings Artifact whose
  every finding carries a source. It may return one `spec_refinement`
  `{target_task, refinement}` for a task of a group that depends on it. It
  needs `PERPLEXITY_API_KEY` in the run-driver's shell (workers inherit the
  orchestrator's environment, `orchestrator/execution/sessions.py`).
- `evaluate` (`EvaluateArgs` = `RunArgs` + `kpi: KpiContract` +
  required `measurements`): runs `commands` as confined Run Children, hashes
  `kpi.harness_paths` at first use, reads the KPI and guards off the
  measurements JSON (top-level scalars only) into an `EvaluationRecord` that
  never gates the unit.
- `optimize` (`OptimizeArgs`: `commands`, `measurements`, `kpi`,
  `evaluations` 1–100, `allow_write`): one candidate, one evaluation, one
  keep / promising / inconclusive / discard / crash decision per round,
  recorded in an Attempt Ledger; `files:` is the mutable region and may not
  overlap `kpi.harness_paths`. `KpiContract` = `key`, `direction` (`min`|`max`),
  `min_effect`, `guards: [{key, direction, max_regression}]`,
  `harness_paths` (non-empty), `smoke`, `good_enough`
  (`docs/orchestrator-kpi-harness.md` in smart_mcps).
- Non-`code` units carry no `slice` and are always their own group.

**Worker sandbox facts that shape the `Run:` lines.** Workers are
Landlock-confined to their worktree plus `data/`; the Bash allowlist grants
`uv`, `python`, `pytest`, `ruff`, `git`, `ls`, `cat`, `grep`, `test`,
`curl`, `mkdir`, `cp`, `mv` by name on `PATH` (`orchestrator/config.py`,
`_BASE_ALLOWED_TOOLS`). No `Run:` line names `/tmp`; scratch goes to
`.coder-scratch/`. `plan-check` requires every extension-bearing path a
`Run:` command reads to exist or be in some unit's `files`, so commands below
name data *directories* (`data/raw/mt5`), never data files; the `Pass:`
clauses name the files.

**Layout this plan commits to** (prospective):

| Path                 | Role                                                                                                                    |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `src/b3quant/`       | the package: `cli.py` (`b3quant` console script), `lake/`, `ingest/`, `features/`                                       |
| `eval/`              | **the harness, hash-protected**: costs, folds, probes, contracts, the intraday engine, `cli.py` behind `b3quant eval`   |
| `models/<track>/`    | forecast candidates; each exposes `candidate.py` with `fit_predict(train, test, horizon) -> predictions`                |
| `strategies/<name>/` | strategy candidates; each exposes `candidate.py` with `target_position(frame) -> positions` and `params.toml`           |
| `rl/`                | the gym-style environment, no agent                                                                                     |
| `scripts/`           | the MT5 export (Windows) and the driver's held-out evaluation                                                           |
| `data/raw/mt5/`      | the human's export (uncommitted, data dir): `WIN_ticks.csv`, `WIN_M1.csv`, `WDO_ticks.csv`, `WDO_M1.csv`, `export.json` |
| `data/raw/cotahist/` | `COTAHIST_A<year>.TXT` files downloaded by `b3quant fetch cotahist` (data dir)                                          |
| `data/lake/`         | parquet datasets `<source>/<instrument>/<granularity>/`; `data/lake/MANIFEST.json` is the Dataset Manifest              |
| `data/eval/`         | measurements and ledger outputs that must outlive a group                                                               |
| `docs/research/`     | Findings Artifacts (research outputs); `docs/research/inbox/` the raw answers                                           |
| `docs/verdicts/`     | the Track Verdicts document                                                                                             |

**The candidate entry-point contract.** `b3quant eval <candidate-dir> --contract <forecast|strategy> --folds <fold-set> --out <measurements.json>`
imports `<candidate-dir>/candidate.py`, runs the leakage probes, scores each
fold of `eval/folds/<fold-set>.json` under `eval/costs.py`, and writes the
measurements JSON (`forecast_skill` or `net_sharpe` as the KPI key, every
guard key, `harness_hash`, `dataset_hashes`, `fold_set`, `wall_clock_s`). It
exits non-zero on a crash or a failed probe and never on a bad score. The
held-out fold set `holdout` is refused unless `B3QUANT_HOLDOUT=1` is set,
which only the run-driver sets.

## Decisions

- **`b3quant/` is a subfolder of smart_mcps for now.** The cloud session
  cannot create repositories; the human splits it out later. Paths and
  commands are written so nothing changes at the split except the
  `--project ..` prefix. Rejected: planning in smart_mcps's own `docs/plans/`
  (validated against the wrong tree).
- **Two plans, split at the order-book boundary.** Plan 1 is every unit that
  runs on free data; plan 2 (R5, R26, the deep LOB candidate, R30) is planned
  after `U9. book-data-research` returns quotes. Rejected: one plan with
  degraded landings (rounds spent on units whose data does not exist).
- **The MT5 export is a precondition of the run, and its script is the first
  code unit.** `scripts/mt5_export.py` runs on Windows (MetaTrader5 Python
  package); the human runs it once; `data/raw/mt5/` exists before `run`.
  Rejected: exporting mid-run as a driver item (the parser's coder would test
  on synthetic data).
- **Daily WIN/WDO bars are aggregated from the MT5 M1 export.** This removes
  the "which derivatives daily file carries WIN/WDO" dependency from loop 1;
  `U9` still asks it for the longer history. Rejected: scraping B3's daily
  bulletin from a worker (b3.com.br is reachable from WSL2 but the format is
  unverified).
- **The harness boundary is `eval/**` plus `src/b3quant/cli.py`.** Everything
  that turns a candidate into a number lives there, including the intraday
  execution engine (`eval/intraday/`), so every `evaluate`/`optimize` unit's
  `harness_paths` is the same two globs and no optimize unit's `files:` ever
  overlaps it. Candidates live outside `src/` (`models/`, `strategies/`) so
  the mutable region is a directory, not a module inside the package.
- **The Dataset Manifest lives at `data/lake/MANIFEST.json`**, beside the parquet it indexes, rather than R2's `data/MANIFEST.json`: one directory to share, hash and verify; the semantics are unchanged.
- **Costs in BRL per leg, two ticks per side, fee table in `eval/fees.toml`.**
  Tick values: WIN 0.2 BRL per point × 5-point tick = 1.00 BRL per tick per
  contract; WDO 10 BRL per point × 0.5-point tick = 5.00 BRL per tick per
  contract; stocks 0.01 BRL per tick per share. The coder verifies these and
  the fee schedule against B3's published pages from WSL2 (`curl` is
  allowed) and the driver re-checks (R33).
- **Cost-aware labels are enforced by the forecast contract.** A
  classification candidate declares its label threshold in ticks; the
  contract refuses one below four ticks on a single instrument. Regression
  candidates (quantiles, pinball loss) are unaffected.
- **Gradient boosting (LightGBM) is the first candidate in both model
  loops.** The second swing candidate is chosen by `U10` (fine-tuned
  foundation model or an LSTM-class model); the intraday loop has one
  candidate in plan 1 (tape-only features) and gains the deep LOB candidate
  in plan 2.
- **Research units verify their inbox answer; they do not re-run the
  briefs.** Each `question` names the inbox file in `focus_paths` and asks
  for held / failed / unreachable per claim. Every consumer unit
  `depends_on` its research unit so a `spec_refinement` can be folded in
  before launch.
- **Fold sets are named files under `eval/folds/`**: `daily` (swing, 1–5
  day horizons, 12-month test windows rolling from 2016), `intraday-5m`
  (breakout, pairs spread, rule layer: 3-month test windows over the MT5
  span), `intraday-1m` (intraday forecast: same windows at M1), each with
  purge = longest label horizon and a 5-session embargo, and `holdout` (the
  last 12 months of every dataset, driver-only). The MT5 span decides the
  intraday fold sets; the fold writer reads `data/lake/MANIFEST.json` and
  refuses to write a set whose dataset hash it has not seen.
- **Loop dials (R31):** `evaluations: 20` on every optimize unit; wall clocks
  20 min (swing, rule layer) and 45 min (intraday M1); patience and revert
  escalation are the recipe's validated defaults. At most one GPU-training
  loop at a time — in plan 1 none trains on the GPU unless `U10` picks a
  sequence model, in which case `U23` is the only GPU loop.
- **Units downstream of an optimize loop share no files or tags with units upstream of it.** The grouper isolates every recipe unit; a code group that both feeds and consumes the same loop is a cycle no dependency-respecting split can cut (`group` refused the first draft on exactly this). So `U27`, `U29` and `U30` share no files with upstream units and consume only tags that recipe units or other downstream units implement (`champion-swing`, `candidate-forecast-rule`), never a tag of an upstream code unit; `U29` writes `rl/README.md` rather than the shared `README.md`.
- **Perplexity access is an environment fact.** `PERPLEXITY_API_KEY` is
  exported in the run-driver's WSL2 shell (`.envrc` from `.envrc.example`);
  never in a plan, a config or a `Run:` line.

No ADR: none of the above is both hard to reverse and surprising.

## Units

### U1. repo-skeleton — a uv-managed package the orchestrator can run fast checks on

- **Summary**: `pyproject.toml` (uv, Python 3.12, polars, pyarrow, numpy, lightgbm, scikit-learn, statsmodels, pytest, ruff) with a `b3quant` console script, the `src/b3quant` package skeleton, and a tests directory, so Preflight has `uv run pytest -q` and `uv run ruff check .` to run.
- **Goal**: `uv sync` succeeds offline-tolerant (lock committed); `uv run b3quant --help` lists the subcommands later units fill (`fetch`, `ingest`, `features`, `folds`, `eval`); `uv run pytest -q` passes with one skeleton test; `uv run ruff check .` is clean; `README.md` gains a "Running the pipeline" section naming the data dir layout from the plan.
- **Recipe**: —
- **Files**: `pyproject.toml` *(new, small)*, `uv.lock` *(new, large)*, `src/b3quant/__init__.py` *(new, small)*, `src/b3quant/cli.py` *(new, small)*, `tests/__init__.py` *(new, small)*, `tests/test_skeleton.py` *(new, small)*, `README.md`
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: implements `b3quant-cli` / —
- **Verification**:
  - Run: `uv run b3quant --help` Pass: prints a usage line and the subcommand names `fetch`, `ingest`, `features`, `folds`, `eval`.
  - Run: `uv run pytest -q` Pass: 1 passed.
  - Run: `uv run ruff check .` Pass: `All checks passed!`.
  - Run: `uv run python -c "import polars, lightgbm, statsmodels; print(polars.__version__)"` Pass: prints a version (the real libraries are installed in the project venv, not mocked).

### U2. mt5-export — the Windows-side MT5 export script the human runs once before the run

- **Summary**: `scripts/mt5_export.py` dumps WIN and WDO ticks and M1 bars (and DOL, IND and the stock universe's M1 where the broker serves them) from a logged-in MetaTrader 5 terminal into `data/raw/mt5/` as CSV plus an `export.json` manifest naming broker, account server, earliest and latest timestamp per file; it has a `--self-test` mode that writes a two-session synthetic sample in the same schema without MT5 so the parser's tests have a fixture.
- **Goal**: On Windows with the `MetaTrader5` package and a logged-in terminal, `python scripts/mt5_export.py --out <dir> --symbols WIN$ WDO$` (the broker's continuous aliases, with a `--symbols` override for per-contract names) writes `WIN_ticks.csv`, `WIN_M1.csv`, `WDO_ticks.csv`, `WDO_M1.csv` and `export.json` using `copy_ticks_range` / `copy_rates_range` in month-sized chunks, resuming from the last timestamp present; timestamps are written as UTC ISO-8601 with the terminal's server offset recorded in `export.json`. `docs/runbooks/mt5-export.md` tells the human what to install, how to find their broker's symbol names, how long it takes, and where to copy the output on the WSL side. `--self-test` runs on Linux without MT5 and writes the same five files for two synthetic sessions.
- **Recipe**: —
- **Files**: `scripts/mt5_export.py` *(new, medium)*, `docs/runbooks/mt5-export.md` *(new, small)*, `tests/test_mt5_export_selftest.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `mt5-raw-schema` / `b3quant-cli`
- **Verification**:
  - Run: `mkdir -p .coder-scratch/out && uv run python scripts/mt5_export.py --self-test --out .coder-scratch/out/mt5-selftest` Pass: the directory holds `WIN_ticks.csv`, `WIN_M1.csv`, `WDO_ticks.csv`, `WDO_M1.csv`, `export.json`; the M1 files have columns `time_utc,open,high,low,close,tick_volume,real_volume,spread`; the tick files have `time_utc,time_msc,bid,ask,last,volume,flags`.
  - Run: `uv run pytest tests/test_mt5_export_selftest.py -q` Pass: the self-test output round-trips through the CSV reader with monotone timestamps and no session crossing midnight UTC unmarked.
  - Run (driver): `ls data/raw/mt5` Pass: the five real files exist on the WSL side before `run` starts, and `export.json` names the broker and the earliest timestamp (the human ran the script on Windows; this is R33's "the MT5 export ran" item).

### U3. data-lake — the content-hashed parquet lake and its Dataset Manifest

- **Summary**: `src/b3quant/lake/` writes datasets to `data/lake/<source>/<instrument>/<granularity>/part-*.parquet` with UTC timestamps plus a `session_date` column (America/Sao_Paulo), and maintains `data/lake/MANIFEST.json` with source, licence, instrument, granularity, date range, row count and sha256 per dataset; a dataset absent from the manifest does not exist for any reader.
- **Goal**: `write_dataset(frame, source, instrument, granularity, licence)` validates the schema (a `ts_utc` datetime column with UTC zone, `session_date` date), writes sorted parquet, hashes the written bytes and upserts the manifest entry; `read_dataset(...)` refuses a dataset whose on-disk hash differs from the manifest; `b3quant lake list` prints the manifest as a table; `b3quant lake verify` rehashes everything and exits non-zero on a mismatch.
- **Recipe**: —
- **Files**: `src/b3quant/lake/__init__.py` *(new, small)*, `src/b3quant/lake/manifest.py` *(new, medium)*, `src/b3quant/lake/paths.py` *(new, small)*, `src/b3quant/cli.py` *(new in U1)*, `tests/test_lake.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `dataset-manifest` / `b3quant-cli`
- **Verification**:
  - Run: `uv run pytest tests/test_lake.py -q` Pass: writing a frame then flipping one byte of a parquet part makes `read_dataset` raise naming the dataset and both hashes.
  - Run: `uv run b3quant lake list --root .coder-scratch/lake` Pass: after the test wrote one dataset there, prints one row with its sha256 and row count.
  - Run: `uv run python -c "import pyarrow.parquet as pq, glob; print(pq.read_metadata(glob.glob('.coder-scratch/lake/**/part-*.parquet', recursive=True)[0]).num_rows)"` Pass: the real pyarrow reader reports the same row count that `lake list` printed for the dataset the test wrote.

### U4. free-ingest — MT5 ticks and M1, COTAHIST daily, continuous contracts, into the lake

- **Summary**: `b3quant ingest mt5 --raw data/raw/mt5` parses the export into `mt5/<WIN|WDO>/{ticks,m1,daily}` datasets (daily aggregated from M1 by session), `b3quant fetch cotahist --years 2010-2025` downloads the annual COTAHIST files from B3 into `data/raw/cotahist/` and `b3quant ingest cotahist` parses the 245-byte fixed-width layout into `cotahist/<ticker>/daily` for the Instrument Universe (top-20 by median daily financial volume over the last twelve months of data, written to the manifest as the universe), and `b3quant ingest continuous` publishes `WIN` and `WDO` continuous series with a volume-based roll rule, the roll dates recorded, and a back-adjustment flag.
- **Goal**: The real ingest writes into the shared data dir `data/lake` (the lake every later unit reads; `data/` is writable in every worktree as the data layer), while the unit tests use `.coder-scratch/lake`. Every dataset lands through U3 with a licence string; MT5 timestamps are converted from the server offset in `export.json` to UTC; the COTAHIST parser handles type-01 rows, two implied decimals, `TPMERC` 010 only, `CODBDI` 02 only, and carries a corporate-action adjustment table (`src/b3quant/ingest/adjustments.toml`, hand-maintained, splits only) applied on read as a flag; the continuous-contract writer rolls when the next contract's 5-session volume exceeds the front's, writes `roll_dates` into the manifest entry, and keeps the raw per-contract series. Schema tests run on the U2 self-test fixture and on a committed 200-row COTAHIST sample.
- **Recipe**: —
- **Files**: `src/b3quant/ingest/__init__.py` *(new, small)*, `src/b3quant/ingest/mt5.py` *(new, medium)*, `src/b3quant/ingest/cotahist.py` *(new, medium)*, `src/b3quant/ingest/continuous.py` *(new, medium)*, `src/b3quant/ingest/adjustments.toml` *(new, small)*, `src/b3quant/ingest/fetch.py` *(new, small)*, `src/b3quant/cli.py` *(new in U1)*, `tests/fixtures/cotahist_sample.txt` *(new, small)*, `tests/test_ingest_mt5.py` *(new, medium)*, `tests/test_ingest_cotahist.py` *(new, medium)*, `tests/test_continuous.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U2, U3
- **Slice**: —
- **Implements / Consumes**: implements `lake-datasets` / `mt5-raw-schema`, `dataset-manifest`
- **Verification**:
  - Run: `uv run pytest tests/test_ingest_cotahist.py tests/test_ingest_mt5.py tests/test_continuous.py -q` Pass: all pass; the COTAHIST sample's `PREULT` of `0000000003850` reads as 38.50.
  - Run: `uv run b3quant ingest mt5 --raw data/raw/mt5 --lake data/lake` Pass: on the real export in the data dir, writes into the shared lake `mt5/WIN/m1`, `mt5/WDO/m1`, `mt5/WIN/ticks`, `mt5/WDO/ticks`, `mt5/WIN/daily`, `mt5/WDO/daily` and prints each dataset's date range; the earliest M1 date matches `export.json`.
  - Run: `uv run b3quant fetch cotahist --years 2016-2025 --raw data/raw/cotahist` Pass: downloads the real annual files from B3 (WSL2 reaches b3.com.br) into the data dir, and `uv run b3quant ingest cotahist --raw data/raw/cotahist --lake data/lake` writes at least 20 tickers' daily datasets and a `universe` entry listing 20 tickers into the shared lake.
  - Run: `uv run b3quant ingest continuous --lake data/lake` Pass: in the shared lake, `WIN` and `WDO` continuous datasets exist with a non-empty `roll_dates` list in the manifest and `back_adjusted: false`.

### U5. exogenous-features — a point-in-time feature store for covariates and the macro calendar

- **Summary**: `src/b3quant/features/` builds `data/lake/features/<name>/daily` datasets where every row carries `known_at_utc`, from free sources: USD/BRL PTAX and the Selic target from Bacen's SGS API, DI1 settlement from B3's daily bulletin, ES, NQ, DXY and Brent daily closes from a free CSV endpoint, VALE3 from COTAHIST as the iron-ore proxy, IBGE's release calendar, Bacen's Copom calendar, FOMC and payroll dates, and B3's holiday table — each as the source `U16. research-news-macro` confirmed.
- **Goal**: `b3quant features build --lake data/lake` writes every feature with `known_at_utc` = publication instant (never the reference period), a `features/calendar/events` dataset with `event`, `known_at_utc`, `value`, `consensus` (null when no free consensus exists), and a `point_in_time(frame, at)` reader that returns only rows known before `at`; `tests/test_features_pit.py` proves no feature at time t uses a row published after t by constructing a frame with a future-published row and asserting the reader hides it. Where a source `U16` found is paid, the feature is written empty with `source: "unavailable"` in the manifest and the Findings Artifact is cited.
- **Recipe**: —
- **Files**: `src/b3quant/features/__init__.py` *(new, small)*, `src/b3quant/features/store.py` *(new, medium)*, `src/b3quant/features/calendar.py` *(new, medium)*, `src/b3quant/features/covariates.py` *(new, medium)*, `src/b3quant/cli.py` *(new in U1)*, `tests/test_features_pit.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U3, U4, U16
- **Slice**: —
- **Implements / Consumes**: implements `feature-store` / `dataset-manifest`
- **Verification**:
  - Run: `uv run pytest tests/test_features_pit.py -q` Pass: the point-in-time reader hides a row whose `known_at_utc` is after the query instant, and the monotonicity test on `known_at_utc` passes.
  - Run: `uv run b3quant features build --lake data/lake --only calendar` Pass: fetches IBGE's real release calendar and Bacen's real Copom dates over the network and writes at least 24 calendar events with UTC instants for the last twelve months into the shared lake.
  - Run: `uv run b3quant features build --lake data/lake --only ptax` Pass: the real Bacen SGS call returns daily PTAX and the dataset's manifest entry names the series id and the licence.
  - Run: `uv run b3quant features build --lake data/lake` Pass: every feature in the Summary's list is written to the shared lake or recorded `source: "unavailable"` with the Findings Artifact cited; `uv run b3quant lake verify --root data/lake` exits 0 afterwards.

### U6. cost-model — B3 fees from a versioned table plus two ticks per side, in BRL per leg

- **Summary**: `eval/fees.toml` (emoluments, registration and settlement per contract and per share, by instrument, versioned by date) and `eval/costs.py` charge every fill `fees + 2 ticks × tick_value` per leg in BRL, expose a one-tick variant as an observation, and report for a two-leg pair the round-trip cost and the break-even spread volatility it implies.
- **Goal**: `cost_per_leg(instrument, qty, price, ticks=2) -> BRL` uses `eval/fees.toml` and `eval/instruments.toml` (tick size, tick value, multiplier, session hours for WIN, WDO, DOL, IND, stocks); `apply_costs(fills) -> net_pnl_series`; `pair_breakeven(leg_a, leg_b, entry_z=2) -> {round_trip_brl, breakeven_sigma_brl}`; the fee table carries a `source_url` and `as_of` per entry that the coder fills from B3's published fee pages reached from WSL2.
- **Recipe**: —
- **Files**: `eval/__init__.py` *(new, small)*, `eval/fees.toml` *(new, small)*, `eval/instruments.toml` *(new, small)*, `eval/costs.py` *(new, medium)*, `tests/test_costs.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U1
- **Slice**: —
- **Implements / Consumes**: implements `cost-model` / —
- **Verification**:
  - Run: `uv run pytest tests/test_costs.py -q` Pass: one WIN contract round trip at two ticks per side costs `2 × (fees + 2 × 1.00 BRL)`; one WDO contract uses 5.00 BRL per tick; the one-tick variant is strictly smaller and reported separately.
  - Run: `uv run python -c "import tomllib; d=tomllib.load(open('eval/fees.toml','rb')); print(sorted(d))"` Pass: prints the instrument keys `WIN`, `WDO`, `DOL`, `IND`, `stock`, each with `as_of` and `source_url`.
  - Run: `uv run python -c "import tomllib, urllib.request as u; d=tomllib.load(open('eval/fees.toml','rb')); print(u.urlopen(d['WIN']['source_url'], timeout=30).status)"` Pass: prints `200` (the fee page the table cites is reachable from the run machine).
  - Run (driver): `uv run python -c "import tomllib; print(tomllib.load(open('eval/fees.toml','rb'))['WIN'])"` Pass: the driver compares the printed emolument and registration values against B3's fee page on the run date and records the match in the run notes (R33: the two-tick cost model is what every ledger row charges).

### U7. harness-core — folds, probes, the harness hash and the `b3quant eval` command

- **Summary**: `eval/` gains the fold writer (`b3quant folds build --lake data/lake`) that writes and commits the four purged, embargoed walk-forward fold sets keyed by dataset hash (`eval/folds/daily.json`, `intraday-5m.json`, `intraday-1m.json`, `holdout.json`) with the Held-out Year carved out into `holdout`, the leakage probes (shuffled labels, future-shifted features, timestamp monotonicity, point-in-time) that crash a scoring run naming the probe, the harness hash over `eval/**` and `src/b3quant/cli.py`, and `b3quant eval <candidate-dir> --contract <forecast|strategy> --folds <set> --out <json>` which is the only way a number is produced.
- **Goal**: `eval/folds.py` builds `daily` (12-month test windows rolling from 2016 over `cotahist`/`features`/`mt5 daily`), `intraday-5m` and `intraday-1m` (3-month windows over the MT5 span) with purge = the longest label horizon declared by the set and a 5-session embargo, and writes `holdout` as the last twelve months of every dataset; a fold file records the dataset hashes it was built from and `b3quant eval` refuses a fold set whose hashes no longer match `data/lake/MANIFEST.json`. `eval/probes.py` runs before every scoring pass; `eval/cli.py` imports `<candidate-dir>/candidate.py`, loads the contract module `eval/contracts/<name>.py` named by `--contract` (shipped by U8; with none present it exits 3 naming the missing module), runs the probes, and writes the measurements JSON with `harness_hash`, `dataset_hashes`, `fold_set`, `wall_clock_s`, `params`; it also accepts `--copy-to <path>` (write a second copy of the JSON, creating the directory), `--param key=value` (repeatable; forwarded to the candidate and echoed under `params`), `--print-harness-hash` (print the hash and exit 0 without scoring) and `--engine-ab` (reserved for U26); `--folds holdout` exits 2 unless `B3QUANT_HOLDOUT=1`. `eval/holdout.toml` states the boundary rule. The fold sets are built from the real shared lake and committed, so every downstream worker sees them.
- **Recipe**: —
- **Files**: `eval/folds.py` *(new, medium)*, `eval/folds/daily.json` *(new, small)*, `eval/folds/intraday-5m.json` *(new, small)*, `eval/folds/intraday-1m.json` *(new, small)*, `eval/folds/holdout.json` *(new, small)*, `eval/holdout.toml` *(new, small)*, `eval/probes.py` *(new, medium)*, `eval/hash.py` *(new, small)*, `eval/cli.py` *(new, medium)*, `src/b3quant/cli.py` *(new in U1)*, `tests/test_folds.py` *(new, medium)*, `tests/test_probes.py` *(new, medium)*, `tests/test_eval_cli.py` *(new, medium)*, `tests/fixtures/candidates/constant/candidate.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U3, U4, U6
- **Slice**: —
- **Implements / Consumes**: implements `eval-cli`, `fold-sets` / `dataset-manifest`, `cost-model`
- **Verification**:
  - Run: `uv run pytest tests/test_folds.py tests/test_probes.py tests/test_eval_cli.py -q` Pass: every fold's test window starts after its train window plus purge plus embargo; no fold in any non-holdout set overlaps the holdout boundary; a candidate reading a feature shifted one step into the future makes `b3quant eval` exit non-zero with `probe: future-shift` on stderr.
  - Run: `uv run b3quant folds build --lake data/lake` Pass: writes the four fold files under `eval/folds/`, each naming the dataset hashes from the real `data/lake` manifest; `daily` has at least 6 folds, `intraday-5m` and `intraday-1m` at least 4, and `holdout` one window covering the last twelve months of every dataset.
  - Run: `uv run b3quant eval --print-harness-hash` Pass: prints a 64-hex hash, and printing it twice gives the same value.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval tests/fixtures/candidates/constant --contract forecast --folds holdout --out .coder-scratch/out/h.json` Pass: exits 2 with `holdout is driver-only` on stderr (the check runs before any contract is loaded; no `B3QUANT_HOLDOUT` in the worker's environment).

### U8. kpi-contracts — forecast skill against naive and net walk-forward Sharpe

- **Summary**: `eval/contracts/forecast.py` scores a forecast candidate as one minus the ratio of its out-of-sample loss to the naive baseline's (pinball loss for return quantiles, log-loss for direction classes with a cost-aware threshold of at least four ticks), with the net-P&L-of-a-fixed-rule guard; `eval/contracts/strategy.py` scores a strategy candidate's annualised net-of-costs Sharpe across the fold set's test windows with max-drawdown, trade-count, turnover and deflated-Sharpe / PBO observations, and a forecast-skill guard when the candidate declares a model input.
- **Goal**: Measurements keys written: forecast contract — `forecast_skill` (KPI), `rule_pnl_vs_naive` (guard, BRL), `label_threshold_ticks`, `loss`, `naive_loss`; strategy contract — `net_sharpe` (KPI), `max_drawdown` (guard, fraction), `trade_count` (guard, per window minimum), `turnover`, `deflated_sharpe`, `pbo`, `forecast_skill` (guard, when declared), `one_tick_net_sharpe` (observation). `eval/naive.py` defines the naive forecast (zero return / class prior) and the fixed rule (sign of the forecast, one contract, time exit at the horizon). A classification candidate declaring `label_threshold_ticks < 4` is refused with a message naming the decision.
- **Recipe**: —
- **Files**: `eval/contracts/__init__.py` *(new, small)*, `eval/contracts/forecast.py` *(new, medium)*, `eval/contracts/strategy.py` *(new, medium)*, `eval/naive.py` *(new, small)*, `tests/test_contracts.py` *(new, medium)*, `tests/fixtures/candidates/naive_copy/candidate.py` *(new, small)*, `tests/fixtures/candidates/flat/candidate.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U7
- **Slice**: —
- **Implements / Consumes**: implements `forecast-kpi`, `strategy-kpi` / `eval-cli`, `cost-model`
- **Verification**:
  - Run: `uv run pytest tests/test_contracts.py -q` Pass: a candidate that copies the naive forecast scores `forecast_skill == 0` within 1e-9; a flat strategy scores `net_sharpe == 0` and `trade_count == 0`; a classification candidate with `label_threshold_ticks = 1` is refused.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval tests/fixtures/candidates/flat --contract strategy --folds intraday-5m --out .coder-scratch/out/s.json --copy-to .coder-scratch/out/copy/s.json --param mode=flat` Pass: on the real lake, writes every key listed in the Goal with `deflated_sharpe` and `pbo` present as numbers, the copy is byte-identical to the original, and `params.mode` is `"flat"` in both.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval tests/fixtures/candidates/constant --contract forecast --folds daily --out .coder-scratch/out/m.json` Pass: on the real lake in `data/lake`, writes a JSON whose `forecast_skill` is approximately 0 for the constant candidate and whose `harness_hash` equals the one `uv run b3quant eval --print-harness-hash` prints.

### U9. book-data-research — price historical B3 order-book data and find the free broker-tagged tape

- **Summary**: A `research` unit verifies `docs/research/inbox/B1-data-sources-book-purchase.md`, establishes whether B3's public `NEGOCIOS*` trade files with participant codes are still published and how deep, which derivatives daily file carries WIN/WDO, what UP2DATA ON DEMAND and two licensed distributors actually sell for a six- and a twelve-month WIN+WDO window (event-level and one-second L2 snapshots, format, licence, price), and writes the purchase recommendation plan 2 is planned from.
- **Goal**: The Findings Artifact at `docs/research/r4-book-data-findings.md` lists, per inbox claim, held / failed / unreachable with a source, ends with a purchase recommendation against the R$ 2,000 target with the fallback order (shorter window, snapshots, platform export), names the schema fields each quoted product carries, and carries no `spec_refinement` (its consumer is in plan 2). Quotes that need a login or a phone call are listed as open items for the human, with the exact page or contact.
- **Recipe**: research
  - question: Verify docs/research/inbox/B1-data-sources-book-purchase.md against primary sources (B3's own pages first, then vendor pages) and answer, with sources: (1) Are B3's public daily trade files (NEGOCIOSAVISTA and any derivatives equivalent, with buyer/seller participant codes) still published, where, in what format, and how many sessions back? (2) Which B3 daily file carries WIN and WDO daily bars and settlement? (3) What does UP2DATA ON DEMAND deliver under "intraday data" for WIN and WDO, in what format, and what does a 6- and a 12-month window cost? (4) The same from at least two licensed distributors covering futures (dxFeed, Cedro, Enfoque or Tryd), for event-level order-book data and for one-second L2 snapshots, with licence terms for personal research. (5) Does any purchasable tape carry broker codes? End with a purchase recommendation against a R$ 2,000 target and the fallback order (shorter window, snapshots, platform export).
  - output: docs/research/r4-book-data-findings.md
  - focus_paths: [docs/research/inbox/B1-data-sources-book-purchase.md, docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md]
  - size: large
- **Files**: `docs/research/r4-book-data-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r4-book-data-findings.md` Pass: at least 10 (every finding sourced).
  - Run: `grep -n "Recommendation" docs/research/r4-book-data-findings.md` Pass: a recommendation section exists naming a vendor or UP2DATA ON DEMAND, a window, a granularity and a price or "quote pending" with the contact.
  - Run (driver): `test -n "$PERPLEXITY_API_KEY" && echo set` Pass: prints `set` in the run-driver's shell before `run` (R33; the value is never printed).
  - Run (driver): `grep -n "NEGOCIOS" docs/research/r4-book-data-findings.md` Pass: the driver confirms the artifact states whether the public trade files are live, by opening the cited B3 URL from WSL2.

### U10. research-swing-forecast — what beats naive at one to five days, net of costs

- **Summary**: A `research` unit verifies `docs/research/inbox/B4-swing-forecasting-foundation-models.md` and decides `U22. model-swing`'s second candidate and starting feature set.
- **Goal**: `docs/research/r14-swing-forecast-findings.md` verifies the cost-adjusted gradient-boosting study (purged expanding-window CV, costs deducted), the 2010–2025 futures/FX benchmark's break-even buffers for VSN+LSTM and xLSTM, and the absence of net-of-cost foundation-model results on daily returns; searches once more for any WIN/WDO or Ibovespa daily study with walk-forward evaluation; and returns a `spec_refinement` for `u22-model-swing` naming the second candidate (fine-tuned foundation model or LSTM-class sequence model, with the library and the GPU minutes per fold it expects) and the feature list the LightGBM candidate starts from.
- **Recipe**: research
  - question: Verify docs/research/inbox/B4-swing-forecasting-foundation-models.md against the primary papers and answer, with sources: which 1–5-day return forecasting method on index or FX futures has the strongest out-of-sample, net-of-cost evidence; whether any time-series foundation model (Chronos-2, TimesFM-2.5, Moirai-2, TiRex) reports net-of-cost results on daily returns; whether any WIN, WDO or Ibovespa daily study uses a walk-forward protocol; and which second candidate (a fine-tuned foundation model or an LSTM-class sequence model) a LightGBM-first loop with one consumer GPU and 20 evaluations should carry, with the feature list the LightGBM candidate should start from.
  - output: docs/research/r14-swing-forecast-findings.md
  - focus_paths: [docs/research/inbox/B4-swing-forecasting-foundation-models.md]
  - size: medium
- **Files**: `docs/research/r14-swing-forecast-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r14-swing-forecast-findings.md` Pass: at least 8.
  - Run: `grep -n -i "second candidate" docs/research/r14-swing-forecast-findings.md` Pass: one section names the chosen second candidate and its expected GPU minutes per fold.

### U11. research-intraday-forecast — label design and horizon for a trades-only intraday model under four ticks of cost

- **Summary**: A `research` unit verifies `docs/research/inbox/B2-intraday-forecasting-lob.md` and refines `U24. model-intraday-tape`'s label design, horizon and feature set for a gradient-boosting candidate over trade-tape features.
- **Goal**: `docs/research/r15-intraday-forecast-findings.md` verifies the gradient-boosting-versus-deep head-to-heads, TLOB's declining-predictability figure and spread-relative labels, and Briola et al.'s operational labels; states what order-flow features are computable from MT5 ticks (bid, ask, last, volume, flags) without an order book; and returns a `spec_refinement` for `u24-model-intraday-tape` with the label (threshold in ticks or transaction-centric), the horizon in minutes, and the feature list.
- **Recipe**: research
  - question: Verify docs/research/inbox/B2-intraday-forecasting-lob.md against the primary papers and answer, with sources: which label design (k-step mid-price change with a tick threshold, spread-relative trend, or transaction-centric) and which horizon in the 1–5 minute range survive a round-trip cost of four ticks on a liquid index future; which order-flow features (aggressor-signed volume, OFI from quotes, microprice from top-of-book, volume clocks) are computable from a tick stream carrying bid, ask, last and volume but no order-book depth; and what a LightGBM candidate over those features should start from.
  - output: docs/research/r15-intraday-forecast-findings.md
  - focus_paths: [docs/research/inbox/B2-intraday-forecasting-lob.md]
  - size: medium
- **Files**: `docs/research/r15-intraday-forecast-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r15-intraday-forecast-findings.md` Pass: at least 8.
  - Run: `grep -n -i "label" docs/research/r15-intraday-forecast-findings.md` Pass: a recommendation names one label design and a threshold of at least four ticks or a transaction-centric rule.

### U12. research-book-microstructure — which order-flow signals are real, and what iceberg detection needs

- **Summary**: A `research` unit verifies `docs/research/inbox/B3-book-microstructure-icebergs.md` for plan 2's book-signals unit: microprice, OFI and queue-imbalance evidence and half-lives on futures, the VPIN critique, whether B3 exposes iceberg semantics or stable order ids, any Brazilian participant-level study, and what Nelogica's Motion Tracker and TR – Acúmulo de Agressão compute.
- **Goal**: `docs/research/r16-book-microstructure-findings.md` with held / failed / unreachable per claim, a ranked list of signals with the data each needs (tape only, L2 snapshots, event stream with order ids, broker codes), and no `spec_refinement` (its consumer is in plan 2).
- **Recipe**: research
  - question: Verify docs/research/inbox/B3-book-microstructure-icebergs.md against the primary papers and B3's own documentation and answer, with sources: the evidence and reported half-lives for microprice, order-flow imbalance and queue-imbalance signals on futures at 1–5 minute horizons; whether VPIN keeps incremental predictive power after controlling for volume and volatility; whether B3 exposes iceberg semantics or stable order ids in any historical data product; any Brazilian study of participant-level (broker code) flow persistence; and what Nelogica's Motion Tracker and "TR – Acúmulo de Agressão" compute. Rank the signals by expected forecast skill at a two-tick cost and name the minimum data each needs.
  - output: docs/research/r16-book-microstructure-findings.md
  - focus_paths: [docs/research/inbox/B3-book-microstructure-icebergs.md]
  - size: medium
- **Files**: `docs/research/r16-book-microstructure-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r16-book-microstructure-findings.md` Pass: at least 8.
  - Run: `grep -n -i "VPIN" docs/research/r16-book-microstructure-findings.md` Pass: the VPIN verdict is stated with its source.

### U13. research-pairs-spread — which pairs survive eight ticks per round trip on B3

- **Summary**: A `research` unit verifies `docs/research/inbox/B5-pairs-spread-b3.md` and refines `U20. baseline-pairs` with the cointegration test, the half-life and spread-volatility screens in BRL, and the WIN–WDO hedge-ratio method.
- **Goal**: `docs/research/r17-pairs-spread-findings.md` verifies the Brazilian cointegration studies' periods, universes and cost treatment; confirms WIN–cash and WDO–DOL are arbitraged at retail cost and WIN–WDO is not; redoes the break-even arithmetic in BRL per leg with real tick values; and returns a `spec_refinement` for `u20-baseline-pairs` with the screens and the hedge-ratio method.
- **Recipe**: research
  - question: Verify docs/research/inbox/B5-pairs-spread-b3.md against the primary studies and answer, with sources: which Brazilian pairs-trading studies report out-of-sample results with costs, on what universe and period; whether WIN versus the cash index and WDO versus DOL are arbitraged away at a retail cost of two ticks per side and whether WIN–WDO remains tradable; the break-even spread volatility and half-life for a two-leg pair paying eight ticks per round trip, computed in BRL per leg with WIN at 1.00 BRL per tick and WDO at 5.00 BRL per tick per contract and stocks at 0.01 BRL per share; and which screens (cointegration test, half-life bound, volatility floor) and hedge-ratio method a daily same-sector stock-pairs baseline and a 5-minute WIN–WDO spread baseline should use.
  - output: docs/research/r17-pairs-spread-findings.md
  - focus_paths: [docs/research/inbox/B5-pairs-spread-b3.md]
  - size: medium
- **Files**: `docs/research/r17-pairs-spread-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r17-pairs-spread-findings.md` Pass: at least 6.
  - Run: `grep -n -i "break-even\|breakeven" docs/research/r17-pairs-spread-findings.md` Pass: the break-even arithmetic appears in BRL with the tick values named.

### U14. research-chart-patterns — primary evidence for breakout levels, and the pattern-detector go/no-go

- **Summary**: A `research` unit checks the primary literature that `docs/research/inbox/B7-chart-patterns-support-resistance.md` missed and refines `U18. baseline-breakout` with the level definitions, time-of-day filter and sizing rule with the best out-of-sample support; it settles the loop-2 go/no-go on coding pattern detectors.
- **Goal**: `docs/research/r18-chart-patterns-findings.md` names peer-reviewed post-2010 studies (or their absence) on Lo–Mamaysky–Wang successors on futures, on opening-range and prior-session breakouts with walk-forward protocols and costs; gives a go/no-go with the evidence; and returns a `spec_refinement` for `u18-baseline-breakout` with level definitions, filter and sizing.
- **Recipe**: research
  - question: The inbox answer docs/research/inbox/B7-chart-patterns-support-resistance.md cites mostly practitioner material. Search the primary literature and answer, with sources: is there peer-reviewed post-2010 evidence of net-of-cost predictive content for head-and-shoulders-class chart patterns (Lo, Mamaysky and Wang successors) on index or FX futures; which support/resistance definitions (prior-session high/low, opening range, pivots, volume nodes, round numbers) have out-of-sample evidence on futures at 5-minute to daily horizons, with costs; which intraday breakout studies use walk-forward evaluation with costs and what they find. Give a go/no-go on coding a chart-pattern detector in a later loop, and the level definitions, time-of-day filter and volatility-targeted sizing rule a WIN/WDO 5-minute breakout baseline should start from.
  - output: docs/research/r18-chart-patterns-findings.md
  - focus_paths: [docs/research/inbox/B7-chart-patterns-support-resistance.md]
  - size: medium
- **Files**: `docs/research/r18-chart-patterns-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r18-chart-patterns-findings.md` Pass: at least 6.
  - Run: `grep -n -i "go/no-go\|no-go" docs/research/r18-chart-patterns-findings.md` Pass: a go/no-go line exists with at least one primary source beside it.

### U15. research-rl — the environment design and the evidence behind the RL gate

- **Summary**: A `research` unit verifies `docs/research/inbox/B6-reinforcement-learning.md` and refines `U29. rl-env` with the state, action, reward, episode and leakage-test design that has evidence.
- **Goal**: `docs/research/r19-rl-findings.md` verifies the reward-hacking taxonomy, the FinRL-Meta leakage critique and the absence of evidence that RL beats a tuned rule at tens of runs on one GPU; collects the environment-design patterns (per-window environment factory, past-only features, training-statistics normalisation, risk term in the reward) with sources; and returns a `spec_refinement` for `u29-rl-env` with the concrete state vector, action space, reward formula and leakage test.
- **Recipe**: research
  - question: Verify docs/research/inbox/B6-reinforcement-learning.md against the primary papers and answer, with sources: is there evidence that an RL sizing/entry/exit policy fed by forecast signals beats a tuned rule layer at a budget of tens of training runs on one consumer GPU; which reward-hacking patterns are documented in trading RL and which mitigations have evidence; and how practitioners build a gym-style environment over a walk-forward backtester without leaking test windows (environment per window, past-only features, normalisation from training statistics). Specify the state vector, action space, reward formula with a risk term, episode boundaries and the leakage test a gated environment should ship with.
  - output: docs/research/r19-rl-findings.md
  - focus_paths: [docs/research/inbox/B6-reinforcement-learning.md]
  - size: medium
- **Files**: `docs/research/r19-rl-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r19-rl-findings.md` Pass: at least 6.
  - Run: `grep -n -i "reward" docs/research/r19-rl-findings.md` Pass: a reward formula with a risk term is written out.

### U16. research-news-macro — free calendar sources with publication instants, and what loop 2 would pay for news

- **Summary**: A `research` unit verifies `docs/research/inbox/B9-news-macro-features.md` and refines `U5. exogenous-features` with the exact free endpoints (IBGE release calendar, Bacen Copom calendar and SGS series, B3 holiday table, a US macro calendar) and their timestamp precision; it prices the Brazilian news feeds for loop 2.
- **Goal**: `docs/research/r20-news-macro-findings.md` names, per calendar source, the URL or API, the fields, the timestamp precision and the licence; names a free source with publication instants for FOMC, payrolls and Treasury auctions or states none exists; prices Broadcast, Valor, Reuters Brasil and Bloomberg Línea for research use; and returns a `spec_refinement` for `u5-exogenous-features` with the endpoint list and the series ids.
- **Recipe**: research
  - question: Verify docs/research/inbox/B9-news-macro-features.md against the sources themselves and answer, with URLs: the exact endpoint, fields, timestamp precision and licence of IBGE's release calendar (IPCA, IPCA-15, GDP, PNAD), Bacen's Copom calendar and the SGS series for PTAX USD/BRL and the Selic target, B3's holiday and special-session table, and a free source carrying publication instants for FOMC decisions, US payrolls and Treasury auctions (or the cheapest paid one if none is free); plus a free daily source for ES, NQ, DXY and Brent closes. Then price Broadcast, Valor, Reuters Brasil and Bloomberg Línea for personal research use and document InfoMoney, CVM and Bacen document access for a later loop.
  - output: docs/research/r20-news-macro-findings.md
  - focus_paths: [docs/research/inbox/B9-news-macro-features.md]
  - size: medium
- **Files**: `docs/research/r20-news-macro-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "https://" docs/research/r20-news-macro-findings.md` Pass: at least 10.
  - Run: `grep -n -i "ibge" docs/research/r20-news-macro-findings.md` Pass: the IBGE calendar endpoint URL and its timestamp field are named.

### U17. research-backtest-engine — can NautilusTrader replay our schema, and at what adapter cost

- **Summary**: A `research` unit verifies `docs/research/inbox/B8-backtest-engine.md` by reading NautilusTrader's current documentation and code for custom order-book and trade data loading, B3-style futures instrument definitions and fill models, and refines `U26. intraday-strategy-harness` with the engine choice and the adapter's real size.
- **Goal**: `docs/research/r21-backtest-engine-findings.md` states, with sources into the Nautilus docs and repository, whether one session of order-level events (add/modify/cancel/trade with order ids) loads into the L3 book type and a trade tape into its data catalog, how a WIN/WDO instrument (tick size, multiplier, session hours, expiry, roll) is defined, which fill model gives partial fills deterministically under a seed, and an estimate of the adapter in files and days; and returns a `spec_refinement` for `u26-intraday-strategy-harness` choosing Nautilus or the thin replayer for plan 1 (plan 1 has only the MT5 tick stream, so the choice is evaluated on trades-plus-quotes replay).
- **Recipe**: research
  - question: Verify docs/research/inbox/B8-backtest-engine.md against NautilusTrader's current documentation and source and answer, with URLs: how custom trade-tick and order-book data (L2 snapshots now; order-level events with order ids later) are loaded into its data catalog and book types; how a B3 futures instrument like WIN or WDO (tick size, tick value, multiplier, session hours, expiry and roll) is defined; which fill model yields partial fills and deterministic results under a fixed seed; typical replay speed for one session; and the adapter's size in files and days. Decide for a plan whose only intraday data is an MT5 tick stream (bid, ask, last, volume) whether an intraday strategy harness should use NautilusTrader or a thin trades-plus-quotes replayer with fixed two-tick slippage, and name the single fact that would reverse it.
  - output: docs/research/r21-backtest-engine-findings.md
  - focus_paths: [docs/research/inbox/B8-backtest-engine.md]
  - size: medium
- **Files**: `docs/research/r21-backtest-engine-findings.md` *(new, medium)*
- **Symbols**: —
- **Depends-on**: —
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `grep -c "nautilustrader" docs/research/r21-backtest-engine-findings.md` Pass: at least 4 (docs or repository URLs cited).
  - Run: `grep -n -i "decision\|recommend" docs/research/r21-backtest-engine-findings.md` Pass: one line chooses Nautilus or the thin replayer for plan 1 and names the reversing fact.

### U18. baseline-breakout — the support/resistance breakout strategy on WIN and WDO at five-minute bars

- **Summary**: `strategies/breakout/candidate.py` maps 5-minute bars to a target position from prior-session high/low and opening-range levels with a breakout entry, a stop, a time exit, a time-of-day filter and volatility-targeted size, parameters in `params.toml` seeded from the human's practice and `U14`'s refinement; it is the intraday classical baseline every later strategy must beat.
- **Goal**: `target_position(frame) -> positions` is deterministic, reads only columns available at bar close, never looks forward (the probes pass), trades at most one unit of size per instrument, and exposes `params.toml` (level set, breakout buffer in ticks, stop in ATR multiples, time exit in bars, session window, volatility target); `tests/test_breakout.py` builds a synthetic session where a breakout must trigger and one where the time-of-day filter must block it.
- **Recipe**: —
- **Files**: `strategies/__init__.py` *(new, small)*, `strategies/breakout/__init__.py` *(new, small)*, `strategies/breakout/candidate.py` *(new, medium)*, `strategies/breakout/params.toml` *(new, small)*, `tests/test_breakout.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4, U8, U14
- **Slice**: —
- **Implements / Consumes**: implements `candidate-breakout` / `strategy-kpi`, `lake-datasets`
- **Verification**:
  - Run: `uv run pytest tests/test_breakout.py -q` Pass: the synthetic breakout session yields a non-zero position after the level is crossed and the filtered session yields none.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval strategies/breakout --contract strategy --folds intraday-5m --out .coder-scratch/out/breakout.json` Pass: on the real MT5-derived 5-minute bars in `data/lake`, the leakage probes pass and the JSON carries `net_sharpe`, `trade_count` at least 20 per window, and `max_drawdown`.

### U19. eval-breakout — score the breakout baseline and record the bar

- **Summary**: An `evaluate` unit scores `strategies/breakout` under the strategy contract on `intraday-5m` and records the `EvaluationRecord` (net Sharpe, guards, harness hash) that every later strategy loop is measured against.
- **Goal**: The measurements JSON lands in `data/eval/breakout-baseline.json` as well as the declared `measurements` path; the record's `kpi_key` is `net_sharpe`; a bad score is not a failure.
- **Recipe**: evaluate
  - commands:
    - cmd: uv run b3quant eval strategies/breakout --contract strategy --folds intraday-5m --out .coder-scratch/measurements.json --copy-to data/eval/breakout-baseline.json
      wall_clock_min: 30.0
  - measurements: .coder-scratch/measurements.json
  - kpi:
    - key: net_sharpe
    - direction: max
    - min_effect: 0.1
    - harness_paths: [eval/\*\*, src/b3quant/cli.py]
    - smoke: uv run b3quant eval --print-harness-hash
  - commit_paths: []
- **Files**: —
- **Symbols**: —
- **Depends-on**: U18
- **Slice**: —
- **Implements / Consumes**: — / `candidate-breakout`
- **Verification**:
  - Run: `uv run b3quant eval --print-harness-hash` Pass: prints a 64-hex hash (the smoke command the recipe runs first).
  - Run (driver, sandbox-safe): `ls data/eval` Pass: after the group merges, `breakout-baseline.json` is listed in the data dir and carries `net_sharpe`, `max_drawdown`, `trade_count`, `harness_hash` and `fold_set: "intraday-5m"`.

### U20. baseline-pairs — daily same-sector cointegration pairs and the WIN–WDO five-minute spread

- **Summary**: `strategies/pairs/` screens the Instrument Universe for same-sector cointegrated pairs (test, half-life bound and spread-volatility floor from `U13`'s refinement), trades a z-score entry/exit on daily bars, and trades the WIN–WDO spread at five-minute bars with a static hedge ratio; both report the pair's round-trip cost and break-even volatility from `eval/costs.py`.
- **Goal**: `strategies/pairs/screen.py` writes the selected pairs with their test statistics, half-lives and volatilities to the candidate's output table; `candidate.py` exposes two variants selected by `params.toml` (`mode = "stocks-daily"` or `mode = "win-wdo-5m"`), each a deterministic `target_position(frame)`; WIN–cash and WDO–DOL are not implemented; a Kalman hedge ratio is left to the strategy loop.
- **Recipe**: —
- **Files**: `strategies/pairs/__init__.py` *(new, small)*, `strategies/pairs/candidate.py` *(new, medium)*, `strategies/pairs/screen.py` *(new, medium)*, `strategies/pairs/params.toml` *(new, small)*, `tests/test_pairs.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4, U8, U13
- **Slice**: —
- **Implements / Consumes**: implements `candidate-pairs` / `strategy-kpi`, `lake-datasets`
- **Verification**:
  - Run: `uv run pytest tests/test_pairs.py -q` Pass: two synthetic cointegrated series are selected by the screen and a synthetic random walk pair is rejected; the z-score rule enters at the configured threshold and exits at the mean.
  - Run: `mkdir -p .coder-scratch/out && uv run python strategies/pairs/screen.py --lake data/lake --out .coder-scratch/out/pairs.csv` Pass: on the real COTAHIST universe, prints at least one selected pair with half-life under the configured bound, or prints `no pair passed the screen` with the nearest miss's statistics.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval strategies/pairs --contract strategy --folds daily --out .coder-scratch/out/pairs.json` Pass: the probes pass and the JSON carries `net_sharpe`, `trade_count` and the pair cost observations.

### U21. eval-pairs — score the pairs baselines and record the bar

- **Summary**: An `evaluate` unit scores `strategies/pairs` in its stock-pairs mode under the strategy contract on `daily` and records the `EvaluationRecord`; the WIN–WDO mode is scored by a second command into its own file.
- **Goal**: `data/eval/pairs-stocks-baseline.json` and `data/eval/pairs-winwdo-baseline.json` exist after the unit; the declared measurements path carries the stock-pairs record (the KPI the record reads); the WIN–WDO one is an observation file for the Track Verdict.
- **Recipe**: evaluate
  - commands:
    - cmd: uv run b3quant eval strategies/pairs --contract strategy --folds daily --out .coder-scratch/measurements.json --copy-to data/eval/pairs-stocks-baseline.json
      wall_clock_min: 20.0
    - cmd: uv run b3quant eval strategies/pairs --contract strategy --folds intraday-5m --param mode=win-wdo-5m --out .coder-scratch/winwdo.json --copy-to data/eval/pairs-winwdo-baseline.json
      wall_clock_min: 30.0
  - measurements: .coder-scratch/measurements.json
  - kpi:
    - key: net_sharpe
    - direction: max
    - min_effect: 0.1
    - harness_paths: [eval/\*\*, src/b3quant/cli.py]
    - smoke: uv run b3quant eval --print-harness-hash
  - commit_paths: []
- **Files**: —
- **Symbols**: —
- **Depends-on**: U20
- **Slice**: —
- **Implements / Consumes**: — / `candidate-pairs`
- **Verification**:
  - Run: `uv run b3quant eval --print-harness-hash` Pass: prints a 64-hex hash.
  - Run (driver, sandbox-safe): `ls data/eval` Pass: after the merge, `pairs-stocks-baseline.json` and `pairs-winwdo-baseline.json` are both present and each names its `fold_set`.

### U22. model-swing — the LightGBM swing candidate and the second candidate U10 named

- **Summary**: `models/swing/candidate.py` fits a LightGBM quantile model for 1–5-day returns on WIN, WDO and the continuous series over `U5`'s features and lagged returns, with the feature list `U10` recommended; `models/swing/second.py` is the second candidate `U10` named, selectable by `params.toml`; both expose `fit_predict(train, test, horizon)`.
- **Goal**: `fit_predict` returns quantile forecasts (0.1, 0.5, 0.9) per instrument and horizon for every test row using only train rows and point-in-time features; a fixed seed makes it deterministic; the feature builder lives in `models/swing/features.py` and reads only `data/lake`; `tests/test_model_swing.py` proves the forecast for a test row does not change when later test rows are removed.
- **Recipe**: —
- **Files**: `models/__init__.py` *(new, small)*, `models/swing/__init__.py` *(new, small)*, `models/swing/candidate.py` *(new, medium)*, `models/swing/features.py` *(new, medium)*, `models/swing/second.py` *(new, medium)*, `models/swing/params.toml` *(new, small)*, `tests/test_model_swing.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4, U5, U8, U10
- **Slice**: —
- **Implements / Consumes**: implements `candidate-swing` / `forecast-kpi`, `feature-store`, `lake-datasets`
- **Verification**:
  - Run: `uv run pytest tests/test_model_swing.py -q` Pass: the no-lookahead test passes and the seed test produces identical forecasts twice.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval models/swing --contract forecast --folds daily --out .coder-scratch/out/swing.json` Pass: on the real lake, the probes pass and the JSON carries `forecast_skill`, `rule_pnl_vs_naive`, `loss`, `naive_loss`; the real `lightgbm` is the model fitted (its version is printed in the JSON's `candidate_env`).

### U23. optimize-swing — twenty evaluations of forecast skill on the swing candidates

- **Summary**: An `optimize` loop over `models/swing/**` scores every candidate commit with `b3quant eval --contract forecast --folds daily` and keeps or reverts on `forecast_skill` with the net-P&L-of-a-fixed-rule guard, writing the Attempt Ledger for the swing track.
- **Goal**: Twenty rounds, each one committed candidate change (features, hyper-parameters, the second candidate's settings, never `eval/`), one evaluation under 20 minutes, one decision; the champion merges at the end; the ledger carries every ruled-out idea with its measured delta.
- **Recipe**: optimize
  - commands:
    - cmd: uv run b3quant eval models/swing --contract forecast --folds daily --out .coder-scratch/measurements.json
      wall_clock_min: 20.0
  - measurements: .coder-scratch/measurements.json
  - kpi:
    - key: forecast_skill
    - direction: max
    - min_effect: 0.01
    - guards:
      - key: rule_pnl_vs_naive
        direction: max
        max_regression: 0.0
    - harness_paths: [eval/\*\*, src/b3quant/cli.py]
    - smoke: uv run b3quant eval --print-harness-hash
  - evaluations: 20
  - allow_write: []
- **Files**: `models/swing/candidate.py`, `models/swing/features.py`, `models/swing/second.py`, `models/swing/params.toml` *(all created by U22; the loop's mutable region)*
- **Symbols**: —
- **Depends-on**: U22
- **Slice**: —
- **Implements / Consumes**: implements `champion-swing` / `candidate-swing`
- **Verification**:
  - Run: `uv run b3quant eval --print-harness-hash` Pass: prints a 64-hex hash.
  - Run (driver): `cat .orchestrator/runs/<run>/groups/<g>/ledger.json` Pass: twenty rows (or fewer with an escalation recorded), every row's `measurements.harness_hash` equal, no row's candidate touching `eval/` (a candidate editing `eval/` is discarded unscored — R33).

### U24. model-intraday-tape — the LightGBM intraday candidate over trade-tape features

- **Summary**: `models/intraday/candidate.py` fits a LightGBM model for the 1–5-minute label `U11` recommended (threshold at least four ticks, or transaction-centric) on WIN and WDO over tape features computed from MT5 ticks in `models/intraday/features.py` (aggressor-signed volume, quote-based OFI, top-of-book microprice, volume clocks, realised volatility); plan 2 adds the order-book candidate.
- **Goal**: `fit_predict(train, test, horizon)` returns class probabilities or quantiles per `U11`'s label; feature computation is streaming over the tick dataset with a declared window and never reads a tick after the bar close; `label_threshold_ticks` is declared in `params.toml`; `tests/test_model_intraday.py` proves the features for a bar do not change when later ticks are removed.
- **Recipe**: —
- **Files**: `models/intraday/__init__.py` *(new, small)*, `models/intraday/candidate.py` *(new, medium)*, `models/intraday/features.py` *(new, large)*, `models/intraday/params.toml` *(new, small)*, `tests/test_model_intraday.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4, U8, U11
- **Slice**: —
- **Implements / Consumes**: implements `candidate-intraday` / `forecast-kpi`, `lake-datasets`
- **Verification**:
  - Run: `uv run pytest tests/test_model_intraday.py -q` Pass: the no-lookahead test and the aggressor-classification test (a tick at the ask is a buy) pass.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval models/intraday --contract forecast --folds intraday-1m --out .coder-scratch/out/intraday.json` Pass: on the real tick datasets, the probes pass, `label_threshold_ticks` is at least 4, and the JSON carries `forecast_skill` and `rule_pnl_vs_naive`.

### U25. optimize-intraday — twenty evaluations of forecast skill on the intraday candidate

- **Summary**: An `optimize` loop over `models/intraday/**` scores every candidate commit with `b3quant eval --contract forecast --folds intraday-1m` and keeps or reverts on `forecast_skill` with the fixed-rule net-P&L guard, writing the intraday track's Attempt Ledger.
- **Goal**: Twenty rounds under 45 minutes each; the champion merges; the ledger records what was ruled out, including every label or horizon change as its own row.
- **Recipe**: optimize
  - commands:
    - cmd: uv run b3quant eval models/intraday --contract forecast --folds intraday-1m --out .coder-scratch/measurements.json
      wall_clock_min: 45.0
  - measurements: .coder-scratch/measurements.json
  - kpi:
    - key: forecast_skill
    - direction: max
    - min_effect: 0.01
    - guards:
      - key: rule_pnl_vs_naive
        direction: max
        max_regression: 0.0
    - harness_paths: [eval/\*\*, src/b3quant/cli.py]
    - smoke: uv run b3quant eval --print-harness-hash
  - evaluations: 20
  - allow_write: []
- **Files**: `models/intraday/candidate.py`, `models/intraday/features.py`, `models/intraday/params.toml` *(all created by U24; the loop's mutable region)*
- **Symbols**: —
- **Depends-on**: U24
- **Slice**: —
- **Implements / Consumes**: implements `champion-intraday` / `candidate-intraday`
- **Verification**:
  - Run: `uv run b3quant eval --print-harness-hash` Pass: prints a 64-hex hash.
  - Run (driver): `cat .orchestrator/runs/<run>/groups/<g>/ledger.json` Pass: every row's `label_threshold_ticks` is at least 4 and every row charges the two-tick cost model (R33).

### U26. intraday-strategy-harness — the session replayer behind the strategy contract, with the engine kill test

- **Summary**: `eval/intraday/` replays a full session deterministically from the MT5 tick stream for any `strategies/*` candidate, applying `eval/costs.py` fills at two ticks of slippage, through the engine `U17` chose (NautilusTrader behind an adapter, or the thin trades-plus-quotes replayer, which ships in both cases); it runs the breakout baseline through both paths on the same sessions and reports whether P&L and drawdown differ beyond noise.
- **Goal**: `eval/intraday/replayer.py` (always) and, if chosen, `eval/intraday/nautilus_adapter.py` expose `replay(candidate, session) -> fills`; the strategy contract calls the engine named in `eval/intraday/engine.toml`; `b3quant eval --engine-ab strategies/breakout --sessions 20` writes `data/eval/engine-ab.json` with both engines' net P&L and drawdown per session and a paired-test p-value; `eval/intraday/instruments.py` defines WIN and WDO sessions, ticks and rolls for the engine.
- **Recipe**: —
- **Files**: `eval/intraday/__init__.py` *(new, small)*, `eval/intraday/replayer.py` *(new, large)*, `eval/intraday/nautilus_adapter.py` *(new, medium)*, `eval/intraday/instruments.py` *(new, small)*, `eval/intraday/engine.toml` *(new, small)*, `eval/cli.py` *(new in U7)*, `tests/test_intraday_harness.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U4, U7, U18, U17
- **Slice**: —
- **Implements / Consumes**: implements `intraday-engine` / `eval-cli`, `cost-model`, `candidate-breakout`
- **Verification**:
  - Run: `uv run pytest tests/test_intraday_harness.py -q` Pass: replaying one synthetic session twice yields byte-identical fills; a market buy fills at the ask plus two ticks; a session replay never fills at a price absent from the tape at that instant.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval --engine-ab strategies/breakout --sessions 20 --out .coder-scratch/out/engine-ab.json` Pass: on 20 real sessions from `data/lake`, writes both engines' per-session net P&L and drawdown and a paired-test p-value; if `engine.toml` names only the thin replayer (U17 chose it), the file says so and the breakout's replay through the thin replayer runs in under 60 s per session.
  - Run: `uv run python -c "import nautilus_trader; print(nautilus_trader.__version__)"` Pass: prints a version when `engine.toml` names Nautilus; when it names the thin replayer, this item is recorded `skipped` with the engine choice quoted from the Findings Artifact.

### U27. strategy-from-forecast — the rule layer mapping a forecast to positions

- **Summary**: `strategies/forecast_rule/candidate.py` turns the swing champion's quantile forecasts into positions with a threshold entry, confidence-scaled size within a hard cap, a stop and a time exit, declaring its model input so the strategy contract applies the forecast-skill guard.
- **Goal**: `params.toml` holds the entry threshold (forecast median over cost in ticks), the size map (median / inter-quantile width, capped at one unit), the stop and the time exit; `candidate.py` declares `model = "models/swing"` so `b3quant eval` runs the model's `fit_predict` per fold first and passes forecasts to `target_position`; `tests/test_forecast_rule.py` proves a forecast below the cost threshold yields no position.
- **Recipe**: —
- **Files**: `strategies/forecast_rule/__init__.py` *(new, small)*, `strategies/forecast_rule/candidate.py` *(new, medium)*, `strategies/forecast_rule/params.toml` *(new, small)*, `tests/test_forecast_rule.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U8, U23
- **Slice**: —
- **Implements / Consumes**: implements `candidate-forecast-rule` / `champion-swing`
- **Verification**:
  - Run: `uv run pytest tests/test_forecast_rule.py -q` Pass: the below-threshold forecast yields zero position; the size never exceeds the cap.
  - Run: `mkdir -p .coder-scratch/out && uv run b3quant eval strategies/forecast_rule --contract strategy --folds daily --out .coder-scratch/out/rule.json` Pass: on the real lake and the merged swing champion, the JSON carries `net_sharpe`, `forecast_skill` (the guard) and `trade_count`.

### U28. optimize-strategy-rule — twenty evaluations of net Sharpe on the rule layer

- **Summary**: An `optimize` loop over `strategies/forecast_rule/**` scores every candidate commit with `b3quant eval --contract strategy --folds daily` and keeps or reverts on `net_sharpe` with max-drawdown and forecast-skill guards, writing the strategy track's Attempt Ledger.
- **Goal**: Twenty rounds under 20 minutes each; the champion merges; a candidate that improves Sharpe by degrading the forecast (the guard) is discarded.
- **Recipe**: optimize
  - commands:
    - cmd: uv run b3quant eval strategies/forecast_rule --contract strategy --folds daily --out .coder-scratch/measurements.json
      wall_clock_min: 20.0
  - measurements: .coder-scratch/measurements.json
  - kpi:
    - key: net_sharpe
    - direction: max
    - min_effect: 0.1
    - guards:
      - key: max_drawdown
        direction: min
        max_regression: 0.05
      - key: forecast_skill
        direction: max
        max_regression: 0.0
    - harness_paths: [eval/\*\*, src/b3quant/cli.py]
    - smoke: uv run b3quant eval --print-harness-hash
  - evaluations: 20
  - allow_write: []
- **Files**: `strategies/forecast_rule/candidate.py`, `strategies/forecast_rule/params.toml` *(both created by U27; the loop's mutable region)*
- **Symbols**: —
- **Depends-on**: U27
- **Slice**: —
- **Implements / Consumes**: implements `champion-forecast-rule` / `candidate-forecast-rule`
- **Verification**:
  - Run: `uv run b3quant eval --print-harness-hash` Pass: prints a 64-hex hash.
  - Run (driver): `cat .orchestrator/runs/<run>/groups/<g>/ledger.json` Pass: no kept row has `fold_set` other than `daily` and the holdout year is absent from every fold set used (R33).

### U29. rl-env — a gym-style environment over the harness, with no agent

- **Summary**: `rl/env.py` builds one environment per walk-forward window from `eval/folds/daily.json`, with state from the swing champion's forecasts and the current position, actions as sizing and entry/exit, reward as net P&L under `eval/costs.py` with a drawdown term, normalisation statistics from the training window, and ships a random policy, a fixed-rule policy and a leakage test instead of a trained agent.
- **Goal**: `make_env(fold_set, fold_index, split) -> gymnasium.Env` per `U15`'s refinement; `rl/policies.py` holds `RandomPolicy` and `FixedRulePolicy` (the same rule as `strategies/forecast_rule`); `tests/test_rl_env.py` proves the test-window environment cannot read a training-window row, that the fixed-rule policy's episode reward equals the rule layer's net P&L on the same window within rounding, and that the gymnasium API checker passes. Training is out of scope: `rl/README.md` states the gate (a forecast champion beating the classical baseline under the strategy contract).
- **Recipe**: —
- **Files**: `rl/__init__.py` *(new, small)*, `rl/env.py` *(new, medium)*, `rl/policies.py` *(new, small)*, `rl/README.md` *(new, small)*, `tests/test_rl_env.py` *(new, medium)*
- **Symbols**: —
- **Depends-on**: U7, U27, U15
- **Slice**: —
- **Implements / Consumes**: implements `rl-env` / `candidate-forecast-rule`
- **Verification**:
  - Run: `uv run pytest tests/test_rl_env.py -q` Pass: the leakage test, the fixed-rule equivalence test and the API check pass.
  - Run: `uv run python -c "import gymnasium; from gymnasium.utils.env_checker import check_env; print(gymnasium.__version__)"` Pass: the real gymnasium is installed and its checker is what the test calls.

### U30. track-verdicts — the closing go/no-go per track, with the driver's held-out evaluations

- **Summary**: `docs/verdicts/loop-1.md` records, per track (swing, intraday, pairs, breakout, chart patterns, RL, news, engine, data), the champion and its KPI, the ledger digest, the held-out evaluation the driver ran, the data gaps, which inbox claims failed verification, and the recommended loop-2 scope; `scripts/holdout_eval.py` is the driver's one command for the held-out year.
- **Goal**: `scripts/holdout_eval.py` runs `b3quant eval` with `B3QUANT_HOLDOUT=1` for each champion and writes `data/eval/holdout-<track>.json`; the verdict document is generated from the ledgers, the baseline records and the holdout files by `scripts/write_verdicts.py` and then edited by the coder for the prose; every research unit's Findings Artifact is linked; the engine kill-test result decides the engine line.
- **Recipe**: —
- **Files**: `docs/verdicts/loop-1.md` *(new, medium)*, `scripts/holdout_eval.py` *(new, small)*, `scripts/write_verdicts.py` *(new, medium)*, `tests/test_write_verdicts.py` *(new, small)*
- **Symbols**: —
- **Depends-on**: U9, U12, U16, U19, U21, U23, U25, U26, U28, U29
- **Slice**: —
- **Implements / Consumes**: — / —
- **Verification**:
  - Run: `uv run pytest tests/test_write_verdicts.py -q` Pass: a synthetic ledger and baseline record render a track section with champion, KPI, ledger counts and a `go` / `no-go` placeholder the coder must replace.
  - Run (driver): `uv run python scripts/holdout_eval.py --tracks swing,intraday,rule --out data/eval` Pass: with `B3QUANT_HOLDOUT=1` in the driver's shell, three `holdout-*.json` files appear and the verdict document's held-out lines quote their `net_sharpe` or `forecast_skill`.
  - Run: `mkdir -p .coder-scratch/out && uv run python scripts/write_verdicts.py --eval data/eval --out .coder-scratch/out/verdict.md` Pass: the generated document quotes `net_sharpe` from the real `breakout-baseline.json` and `pairs-stocks-baseline.json` in the data dir (written by U19 and U21 before this unit).
  - Run: `grep -c "## Track:" docs/verdicts/loop-1.md` Pass: 9 (one section per track), and `grep -c "placeholder" docs/verdicts/loop-1.md` prints 0.

## Task Map

```yaml
# orchestrator-task-map v2
tasks:
  - task_id: u1-repo-skeleton
    description: A uv-managed package with the b3quant console script so Preflight can run pytest and ruff
    slice: null
    files:
      - pyproject.toml
      - uv.lock
      - src/b3quant/__init__.py
      - src/b3quant/cli.py
      - tests/__init__.py
      - tests/test_skeleton.py
      - README.md
    size_hints:
      pyproject.toml: small
      uv.lock: large
      src/b3quant/__init__.py: small
      src/b3quant/cli.py: small
      tests/__init__.py: small
      tests/test_skeleton.py: small
    symbols: []
    depends_on: []
    implements: ["b3quant-cli"]
    consumes: []
  - task_id: u2-mt5-export
    description: The Windows-side MT5 export script with a self-test mode and its runbook
    slice: null
    files:
      - scripts/mt5_export.py
      - docs/runbooks/mt5-export.md
      - tests/test_mt5_export_selftest.py
    size_hints:
      scripts/mt5_export.py: medium
      docs/runbooks/mt5-export.md: small
      tests/test_mt5_export_selftest.py: small
    symbols: []
    depends_on: [u1-repo-skeleton]
    implements: ["mt5-raw-schema"]
    consumes: ["b3quant-cli"]
  - task_id: u3-data-lake
    description: The content-hashed parquet lake and its Dataset Manifest
    slice: null
    files:
      - src/b3quant/lake/__init__.py
      - src/b3quant/lake/manifest.py
      - src/b3quant/lake/paths.py
      - src/b3quant/cli.py
      - tests/test_lake.py
    size_hints:
      src/b3quant/lake/__init__.py: small
      src/b3quant/lake/manifest.py: medium
      src/b3quant/lake/paths.py: small
      tests/test_lake.py: medium
    symbols: []
    depends_on: [u1-repo-skeleton]
    implements: ["dataset-manifest"]
    consumes: ["b3quant-cli"]
  - task_id: u4-free-ingest
    description: MT5 ticks and M1, COTAHIST daily and continuous contracts into the lake
    slice: null
    files:
      - src/b3quant/ingest/__init__.py
      - src/b3quant/ingest/mt5.py
      - src/b3quant/ingest/cotahist.py
      - src/b3quant/ingest/continuous.py
      - src/b3quant/ingest/adjustments.toml
      - src/b3quant/ingest/fetch.py
      - src/b3quant/cli.py
      - tests/fixtures/cotahist_sample.txt
      - tests/test_ingest_mt5.py
      - tests/test_ingest_cotahist.py
      - tests/test_continuous.py
    size_hints:
      src/b3quant/ingest/__init__.py: small
      src/b3quant/ingest/mt5.py: medium
      src/b3quant/ingest/cotahist.py: medium
      src/b3quant/ingest/continuous.py: medium
      src/b3quant/ingest/adjustments.toml: small
      src/b3quant/ingest/fetch.py: small
      tests/fixtures/cotahist_sample.txt: small
      tests/test_ingest_mt5.py: medium
      tests/test_ingest_cotahist.py: medium
      tests/test_continuous.py: small
    symbols: []
    depends_on: [u2-mt5-export, u3-data-lake]
    implements: ["lake-datasets"]
    consumes: ["mt5-raw-schema", "dataset-manifest"]
  - task_id: u5-exogenous-features
    description: A point-in-time feature store for covariates and the macro calendar
    slice: null
    files:
      - src/b3quant/features/__init__.py
      - src/b3quant/features/store.py
      - src/b3quant/features/calendar.py
      - src/b3quant/features/covariates.py
      - src/b3quant/cli.py
      - tests/test_features_pit.py
    size_hints:
      src/b3quant/features/__init__.py: small
      src/b3quant/features/store.py: medium
      src/b3quant/features/calendar.py: medium
      src/b3quant/features/covariates.py: medium
      tests/test_features_pit.py: medium
    symbols: []
    depends_on: [u3-data-lake, u4-free-ingest, u16-research-news-macro]
    implements: ["feature-store"]
    consumes: ["dataset-manifest"]
  - task_id: u6-cost-model
    description: B3 fees from a versioned table plus two ticks per side, in BRL per leg
    slice: null
    files:
      - eval/__init__.py
      - eval/fees.toml
      - eval/instruments.toml
      - eval/costs.py
      - tests/test_costs.py
    size_hints:
      eval/__init__.py: small
      eval/fees.toml: small
      eval/instruments.toml: small
      eval/costs.py: medium
      tests/test_costs.py: medium
    symbols: []
    depends_on: [u1-repo-skeleton]
    implements: ["cost-model"]
    consumes: []
  - task_id: u7-harness-core
    description: Folds, leakage probes, the harness hash and the b3quant eval command
    slice: null
    files:
      - eval/folds.py
      - eval/folds/daily.json
      - eval/folds/intraday-5m.json
      - eval/folds/intraday-1m.json
      - eval/folds/holdout.json
      - eval/holdout.toml
      - eval/probes.py
      - eval/hash.py
      - eval/cli.py
      - src/b3quant/cli.py
      - tests/test_folds.py
      - tests/test_probes.py
      - tests/test_eval_cli.py
      - tests/fixtures/candidates/constant/candidate.py
    size_hints:
      eval/folds.py: medium
      eval/folds/daily.json: small
      eval/folds/intraday-5m.json: small
      eval/folds/intraday-1m.json: small
      eval/folds/holdout.json: small
      eval/holdout.toml: small
      eval/probes.py: medium
      eval/hash.py: small
      eval/cli.py: medium
      tests/test_folds.py: medium
      tests/test_probes.py: medium
      tests/test_eval_cli.py: medium
      tests/fixtures/candidates/constant/candidate.py: small
    symbols: []
    depends_on: [u3-data-lake, u4-free-ingest, u6-cost-model]
    implements: ["eval-cli", "fold-sets"]
    consumes: ["dataset-manifest", "cost-model"]
  - task_id: u8-kpi-contracts
    description: Forecast skill against naive and net walk-forward Sharpe as KPI Contracts
    slice: null
    files:
      - eval/contracts/__init__.py
      - eval/contracts/forecast.py
      - eval/contracts/strategy.py
      - eval/naive.py
      - tests/test_contracts.py
      - tests/fixtures/candidates/naive_copy/candidate.py
      - tests/fixtures/candidates/flat/candidate.py
    size_hints:
      eval/contracts/__init__.py: small
      eval/contracts/forecast.py: medium
      eval/contracts/strategy.py: medium
      eval/naive.py: small
      tests/test_contracts.py: medium
      tests/fixtures/candidates/naive_copy/candidate.py: small
      tests/fixtures/candidates/flat/candidate.py: small
    symbols: []
    depends_on: [u7-harness-core]
    implements: ["forecast-kpi", "strategy-kpi"]
    consumes: ["eval-cli", "cost-model"]
  - task_id: u9-book-data-research
    description: Price historical B3 order-book data and find the free broker-tagged tape
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B1-data-sources-book-purchase.md against primary sources (B3's own pages first, then vendor pages) and answer, with sources: (1) Are B3's public daily trade files (NEGOCIOSAVISTA and any derivatives equivalent, with buyer/seller participant codes) still published, where, in what format, and how many sessions back? (2) Which B3 daily file carries WIN and WDO daily bars and settlement? (3) What does UP2DATA ON DEMAND deliver under \"intraday data\" for WIN and WDO, in what format, and what does a 6- and a 12-month window cost? (4) The same from at least two licensed distributors covering futures (dxFeed, Cedro, Enfoque or Tryd), for event-level order-book data and for one-second L2 snapshots, with licence terms for personal research. (5) Does any purchasable tape carry broker codes? End with a purchase recommendation against a R$ 2,000 target and the fallback order (shorter window, snapshots, platform export)."
      output: docs/research/r4-book-data-findings.md
      focus_paths: [docs/research/inbox/B1-data-sources-book-purchase.md, docs/brainstorms/2026-10-02-b3quant-trading-research-pipeline-requirements.md]
      size: large
    files:
      - docs/research/r4-book-data-findings.md
    size_hints:
      docs/research/r4-book-data-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u10-research-swing-forecast
    description: What beats naive at one to five days net of costs, and the swing loop's second candidate
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B4-swing-forecasting-foundation-models.md against the primary papers and answer, with sources: which 1–5-day return forecasting method on index or FX futures has the strongest out-of-sample, net-of-cost evidence; whether any time-series foundation model (Chronos-2, TimesFM-2.5, Moirai-2, TiRex) reports net-of-cost results on daily returns; whether any WIN, WDO or Ibovespa daily study uses a walk-forward protocol; and which second candidate (a fine-tuned foundation model or an LSTM-class sequence model) a LightGBM-first loop with one consumer GPU and 20 evaluations should carry, with the feature list the LightGBM candidate should start from."
      output: docs/research/r14-swing-forecast-findings.md
      focus_paths: [docs/research/inbox/B4-swing-forecasting-foundation-models.md]
      size: medium
    files:
      - docs/research/r14-swing-forecast-findings.md
    size_hints:
      docs/research/r14-swing-forecast-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u11-research-intraday-forecast
    description: Label design and horizon for a trades-only intraday model under four ticks of cost
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B2-intraday-forecasting-lob.md against the primary papers and answer, with sources: which label design (k-step mid-price change with a tick threshold, spread-relative trend, or transaction-centric) and which horizon in the 1–5 minute range survive a round-trip cost of four ticks on a liquid index future; which order-flow features (aggressor-signed volume, OFI from quotes, microprice from top-of-book, volume clocks) are computable from a tick stream carrying bid, ask, last and volume but no order-book depth; and what a LightGBM candidate over those features should start from."
      output: docs/research/r15-intraday-forecast-findings.md
      focus_paths: [docs/research/inbox/B2-intraday-forecasting-lob.md]
      size: medium
    files:
      - docs/research/r15-intraday-forecast-findings.md
    size_hints:
      docs/research/r15-intraday-forecast-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u12-research-book-microstructure
    description: Which order-flow signals are real and what iceberg detection needs, for plan 2
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B3-book-microstructure-icebergs.md against the primary papers and B3's own documentation and answer, with sources: the evidence and reported half-lives for microprice, order-flow imbalance and queue-imbalance signals on futures at 1–5 minute horizons; whether VPIN keeps incremental predictive power after controlling for volume and volatility; whether B3 exposes iceberg semantics or stable order ids in any historical data product; any Brazilian study of participant-level (broker code) flow persistence; and what Nelogica's Motion Tracker and \"TR – Acúmulo de Agressão\" compute. Rank the signals by expected forecast skill at a two-tick cost and name the minimum data each needs."
      output: docs/research/r16-book-microstructure-findings.md
      focus_paths: [docs/research/inbox/B3-book-microstructure-icebergs.md]
      size: medium
    files:
      - docs/research/r16-book-microstructure-findings.md
    size_hints:
      docs/research/r16-book-microstructure-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u13-research-pairs-spread
    description: Which pairs survive eight ticks per round trip on B3, and the screens the baseline uses
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B5-pairs-spread-b3.md against the primary studies and answer, with sources: which Brazilian pairs-trading studies report out-of-sample results with costs, on what universe and period; whether WIN versus the cash index and WDO versus DOL are arbitraged away at a retail cost of two ticks per side and whether WIN–WDO remains tradable; the break-even spread volatility and half-life for a two-leg pair paying eight ticks per round trip, computed in BRL per leg with WIN at 1.00 BRL per tick and WDO at 5.00 BRL per tick per contract and stocks at 0.01 BRL per share; and which screens (cointegration test, half-life bound, volatility floor) and hedge-ratio method a daily same-sector stock-pairs baseline and a 5-minute WIN–WDO spread baseline should use."
      output: docs/research/r17-pairs-spread-findings.md
      focus_paths: [docs/research/inbox/B5-pairs-spread-b3.md]
      size: medium
    files:
      - docs/research/r17-pairs-spread-findings.md
    size_hints:
      docs/research/r17-pairs-spread-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u14-research-chart-patterns
    description: Primary evidence for breakout levels and the chart-pattern detector go/no-go
    recipe: research
    recipe_args:
      question: "The inbox answer docs/research/inbox/B7-chart-patterns-support-resistance.md cites mostly practitioner material. Search the primary literature and answer, with sources: is there peer-reviewed post-2010 evidence of net-of-cost predictive content for head-and-shoulders-class chart patterns (Lo, Mamaysky and Wang successors) on index or FX futures; which support/resistance definitions (prior-session high/low, opening range, pivots, volume nodes, round numbers) have out-of-sample evidence on futures at 5-minute to daily horizons, with costs; which intraday breakout studies use walk-forward evaluation with costs and what they find. Give a go/no-go on coding a chart-pattern detector in a later loop, and the level definitions, time-of-day filter and volatility-targeted sizing rule a WIN/WDO 5-minute breakout baseline should start from."
      output: docs/research/r18-chart-patterns-findings.md
      focus_paths: [docs/research/inbox/B7-chart-patterns-support-resistance.md]
      size: medium
    files:
      - docs/research/r18-chart-patterns-findings.md
    size_hints:
      docs/research/r18-chart-patterns-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u15-research-rl
    description: The environment design and the evidence behind the RL gate
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B6-reinforcement-learning.md against the primary papers and answer, with sources: is there evidence that an RL sizing/entry/exit policy fed by forecast signals beats a tuned rule layer at a budget of tens of training runs on one consumer GPU; which reward-hacking patterns are documented in trading RL and which mitigations have evidence; and how practitioners build a gym-style environment over a walk-forward backtester without leaking test windows (environment per window, past-only features, normalisation from training statistics). Specify the state vector, action space, reward formula with a risk term, episode boundaries and the leakage test a gated environment should ship with."
      output: docs/research/r19-rl-findings.md
      focus_paths: [docs/research/inbox/B6-reinforcement-learning.md]
      size: medium
    files:
      - docs/research/r19-rl-findings.md
    size_hints:
      docs/research/r19-rl-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u16-research-news-macro
    description: Free calendar sources with publication instants, and what loop 2 would pay for news
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B9-news-macro-features.md against the sources themselves and answer, with URLs: the exact endpoint, fields, timestamp precision and licence of IBGE's release calendar (IPCA, IPCA-15, GDP, PNAD), Bacen's Copom calendar and the SGS series for PTAX USD/BRL and the Selic target, B3's holiday and special-session table, and a free source carrying publication instants for FOMC decisions, US payrolls and Treasury auctions (or the cheapest paid one if none is free); plus a free daily source for ES, NQ, DXY and Brent closes. Then price Broadcast, Valor, Reuters Brasil and Bloomberg Línea for personal research use and document InfoMoney, CVM and Bacen document access for a later loop."
      output: docs/research/r20-news-macro-findings.md
      focus_paths: [docs/research/inbox/B9-news-macro-features.md]
      size: medium
    files:
      - docs/research/r20-news-macro-findings.md
    size_hints:
      docs/research/r20-news-macro-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u17-research-backtest-engine
    description: Can NautilusTrader replay our schema, and at what adapter cost
    recipe: research
    recipe_args:
      question: "Verify docs/research/inbox/B8-backtest-engine.md against NautilusTrader's current documentation and source and answer, with URLs: how custom trade-tick and order-book data (L2 snapshots now; order-level events with order ids later) are loaded into its data catalog and book types; how a B3 futures instrument like WIN or WDO (tick size, tick value, multiplier, session hours, expiry and roll) is defined; which fill model yields partial fills and deterministic results under a fixed seed; typical replay speed for one session; and the adapter's size in files and days. Decide for a plan whose only intraday data is an MT5 tick stream (bid, ask, last, volume) whether an intraday strategy harness should use NautilusTrader or a thin trades-plus-quotes replayer with fixed two-tick slippage, and name the single fact that would reverse it."
      output: docs/research/r21-backtest-engine-findings.md
      focus_paths: [docs/research/inbox/B8-backtest-engine.md]
      size: medium
    files:
      - docs/research/r21-backtest-engine-findings.md
    size_hints:
      docs/research/r21-backtest-engine-findings.md: medium
    symbols: []
    depends_on: []
    implements: []
    consumes: []
  - task_id: u18-baseline-breakout
    description: The support/resistance breakout strategy on WIN and WDO at five-minute bars
    slice: null
    files:
      - strategies/__init__.py
      - strategies/breakout/__init__.py
      - strategies/breakout/candidate.py
      - strategies/breakout/params.toml
      - tests/test_breakout.py
    size_hints:
      strategies/__init__.py: small
      strategies/breakout/__init__.py: small
      strategies/breakout/candidate.py: medium
      strategies/breakout/params.toml: small
      tests/test_breakout.py: medium
    symbols: []
    depends_on: [u4-free-ingest, u8-kpi-contracts, u14-research-chart-patterns]
    implements: ["candidate-breakout"]
    consumes: ["strategy-kpi", "lake-datasets"]
  - task_id: u19-eval-breakout
    description: Score the breakout baseline under the strategy contract and record the bar
    recipe: evaluate
    recipe_args:
      commands:
        - cmd: uv run b3quant eval strategies/breakout --contract strategy --folds intraday-5m --out .coder-scratch/measurements.json --copy-to data/eval/breakout-baseline.json
          wall_clock_min: 30.0
      measurements: .coder-scratch/measurements.json
      kpi:
        key: net_sharpe
        direction: max
        min_effect: 0.1
        harness_paths: ["eval/**", "src/b3quant/cli.py"]
        smoke: uv run b3quant eval --print-harness-hash
      commit_paths: []
    files: []
    symbols: []
    depends_on: [u18-baseline-breakout]
    implements: []
    consumes: ["candidate-breakout"]
  - task_id: u20-baseline-pairs
    description: Daily same-sector cointegration pairs and the WIN–WDO five-minute spread
    slice: null
    files:
      - strategies/pairs/__init__.py
      - strategies/pairs/candidate.py
      - strategies/pairs/screen.py
      - strategies/pairs/params.toml
      - tests/test_pairs.py
    size_hints:
      strategies/pairs/__init__.py: small
      strategies/pairs/candidate.py: medium
      strategies/pairs/screen.py: medium
      strategies/pairs/params.toml: small
      tests/test_pairs.py: medium
    symbols: []
    depends_on: [u4-free-ingest, u8-kpi-contracts, u13-research-pairs-spread]
    implements: ["candidate-pairs"]
    consumes: ["strategy-kpi", "lake-datasets"]
  - task_id: u21-eval-pairs
    description: Score the pairs baselines under the strategy contract and record the bar
    recipe: evaluate
    recipe_args:
      commands:
        - cmd: uv run b3quant eval strategies/pairs --contract strategy --folds daily --out .coder-scratch/measurements.json --copy-to data/eval/pairs-stocks-baseline.json
          wall_clock_min: 20.0
        - cmd: uv run b3quant eval strategies/pairs --contract strategy --folds intraday-5m --param mode=win-wdo-5m --out .coder-scratch/winwdo.json --copy-to data/eval/pairs-winwdo-baseline.json
          wall_clock_min: 30.0
      measurements: .coder-scratch/measurements.json
      kpi:
        key: net_sharpe
        direction: max
        min_effect: 0.1
        harness_paths: ["eval/**", "src/b3quant/cli.py"]
        smoke: uv run b3quant eval --print-harness-hash
      commit_paths: []
    files: []
    symbols: []
    depends_on: [u20-baseline-pairs]
    implements: []
    consumes: ["candidate-pairs"]
  - task_id: u22-model-swing
    description: The LightGBM swing candidate and the second candidate the research unit named
    slice: null
    files:
      - models/__init__.py
      - models/swing/__init__.py
      - models/swing/candidate.py
      - models/swing/features.py
      - models/swing/second.py
      - models/swing/params.toml
      - tests/test_model_swing.py
    size_hints:
      models/__init__.py: small
      models/swing/__init__.py: small
      models/swing/candidate.py: medium
      models/swing/features.py: medium
      models/swing/second.py: medium
      models/swing/params.toml: small
      tests/test_model_swing.py: medium
    symbols: []
    depends_on: [u4-free-ingest, u5-exogenous-features, u8-kpi-contracts, u10-research-swing-forecast]
    implements: ["candidate-swing"]
    consumes: ["forecast-kpi", "feature-store", "lake-datasets"]
  - task_id: u23-optimize-swing
    description: Twenty evaluations of forecast skill on the swing candidates
    recipe: optimize
    recipe_args:
      commands:
        - cmd: uv run b3quant eval models/swing --contract forecast --folds daily --out .coder-scratch/measurements.json
          wall_clock_min: 20.0
      measurements: .coder-scratch/measurements.json
      kpi:
        key: forecast_skill
        direction: max
        min_effect: 0.01
        guards:
          - key: rule_pnl_vs_naive
            direction: max
            max_regression: 0.0
        harness_paths: ["eval/**", "src/b3quant/cli.py"]
        smoke: uv run b3quant eval --print-harness-hash
      evaluations: 20
      allow_write: []
    files:
      - models/swing/candidate.py
      - models/swing/features.py
      - models/swing/second.py
      - models/swing/params.toml
    symbols: []
    depends_on: [u22-model-swing]
    implements: ["champion-swing"]
    consumes: ["candidate-swing"]
  - task_id: u24-model-intraday-tape
    description: The LightGBM intraday candidate over trade-tape features from MT5 ticks
    slice: null
    files:
      - models/intraday/__init__.py
      - models/intraday/candidate.py
      - models/intraday/features.py
      - models/intraday/params.toml
      - tests/test_model_intraday.py
    size_hints:
      models/intraday/__init__.py: small
      models/intraday/candidate.py: medium
      models/intraday/features.py: large
      models/intraday/params.toml: small
      tests/test_model_intraday.py: medium
    symbols: []
    depends_on: [u4-free-ingest, u8-kpi-contracts, u11-research-intraday-forecast]
    implements: ["candidate-intraday"]
    consumes: ["forecast-kpi", "lake-datasets"]
  - task_id: u25-optimize-intraday
    description: Twenty evaluations of forecast skill on the intraday candidate
    recipe: optimize
    recipe_args:
      commands:
        - cmd: uv run b3quant eval models/intraday --contract forecast --folds intraday-1m --out .coder-scratch/measurements.json
          wall_clock_min: 45.0
      measurements: .coder-scratch/measurements.json
      kpi:
        key: forecast_skill
        direction: max
        min_effect: 0.01
        guards:
          - key: rule_pnl_vs_naive
            direction: max
            max_regression: 0.0
        harness_paths: ["eval/**", "src/b3quant/cli.py"]
        smoke: uv run b3quant eval --print-harness-hash
      evaluations: 20
      allow_write: []
    files:
      - models/intraday/candidate.py
      - models/intraday/features.py
      - models/intraday/params.toml
    symbols: []
    depends_on: [u24-model-intraday-tape]
    implements: ["champion-intraday"]
    consumes: ["candidate-intraday"]
  - task_id: u26-intraday-strategy-harness
    description: The session replayer behind the strategy contract, with the engine kill test
    slice: null
    files:
      - eval/intraday/__init__.py
      - eval/intraday/replayer.py
      - eval/intraday/nautilus_adapter.py
      - eval/intraday/instruments.py
      - eval/intraday/engine.toml
      - eval/cli.py
      - tests/test_intraday_harness.py
    size_hints:
      eval/intraday/__init__.py: small
      eval/intraday/replayer.py: large
      eval/intraday/nautilus_adapter.py: medium
      eval/intraday/instruments.py: small
      eval/intraday/engine.toml: small
      tests/test_intraday_harness.py: medium
    symbols: []
    depends_on: [u4-free-ingest, u7-harness-core, u18-baseline-breakout, u17-research-backtest-engine]
    implements: ["intraday-engine"]
    consumes: ["eval-cli", "cost-model", "candidate-breakout"]
  - task_id: u27-strategy-from-forecast
    description: The rule layer mapping the swing champion's forecast to positions
    slice: null
    files:
      - strategies/forecast_rule/__init__.py
      - strategies/forecast_rule/candidate.py
      - strategies/forecast_rule/params.toml
      - tests/test_forecast_rule.py
    size_hints:
      strategies/forecast_rule/__init__.py: small
      strategies/forecast_rule/candidate.py: medium
      strategies/forecast_rule/params.toml: small
      tests/test_forecast_rule.py: medium
    symbols: []
    depends_on: [u8-kpi-contracts, u23-optimize-swing]
    implements: ["candidate-forecast-rule"]
    consumes: ["champion-swing"]
  - task_id: u28-optimize-strategy-rule
    description: Twenty evaluations of net Sharpe on the rule layer
    recipe: optimize
    recipe_args:
      commands:
        - cmd: uv run b3quant eval strategies/forecast_rule --contract strategy --folds daily --out .coder-scratch/measurements.json
          wall_clock_min: 20.0
      measurements: .coder-scratch/measurements.json
      kpi:
        key: net_sharpe
        direction: max
        min_effect: 0.1
        guards:
          - key: max_drawdown
            direction: min
            max_regression: 0.05
          - key: forecast_skill
            direction: max
            max_regression: 0.0
        harness_paths: ["eval/**", "src/b3quant/cli.py"]
        smoke: uv run b3quant eval --print-harness-hash
      evaluations: 20
      allow_write: []
    files:
      - strategies/forecast_rule/candidate.py
      - strategies/forecast_rule/params.toml
    symbols: []
    depends_on: [u27-strategy-from-forecast]
    implements: ["champion-forecast-rule"]
    consumes: ["candidate-forecast-rule"]
  - task_id: u29-rl-env
    description: A gym-style environment over the harness with a random and a fixed-rule policy and no agent
    slice: null
    files:
      - rl/__init__.py
      - rl/env.py
      - rl/policies.py
      - rl/README.md
      - tests/test_rl_env.py
    size_hints:
      rl/__init__.py: small
      rl/env.py: medium
      rl/policies.py: small
      rl/README.md: small
      tests/test_rl_env.py: medium
    symbols: []
    depends_on: [u7-harness-core, u27-strategy-from-forecast, u15-research-rl]
    implements: ["rl-env"]
    consumes: ["candidate-forecast-rule"]
  - task_id: u30-track-verdicts
    description: The closing go/no-go per track with the driver's held-out evaluations
    slice: null
    files:
      - docs/verdicts/loop-1.md
      - scripts/holdout_eval.py
      - scripts/write_verdicts.py
      - tests/test_write_verdicts.py
    size_hints:
      docs/verdicts/loop-1.md: medium
      scripts/holdout_eval.py: small
      scripts/write_verdicts.py: medium
      tests/test_write_verdicts.py: small
    symbols: []
    depends_on: [u9-book-data-research, u12-research-book-microstructure, u16-research-news-macro, u19-eval-breakout, u21-eval-pairs, u23-optimize-swing, u25-optimize-intraday, u26-intraday-strategy-harness, u28-optimize-strategy-rule, u29-rl-env]
    implements: []
    consumes: []
```
