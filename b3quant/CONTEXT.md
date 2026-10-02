# b3quant

Domain glossary for the B3 trading research pipeline. Opinionated,
project-specific terms only. Copy this file to the `b3quant` repo root as
`CONTEXT.md`; the orchestrator's own vocabulary (Unit Recipe, KPI Contract,
Evaluation Harness, Champion, Attempt Ledger, Keep-or-Revert) is defined in
smart_mcps and is not repeated here.

## Language

**Instrument Universe**:
The fixed list of what the harness may trade — WIN and WDO continuous
contracts plus the named liquid IBOV stocks — as opposed to what it may only
read as a feature. Declared once in the data lake manifest; a strategy that
trades outside it fails validation.
_Avoid_: watchlist (implies a trader's discretion), symbols (any dataset has
symbols; only these are tradable)

**Continuous Contract**:
The stitched WIN or WDO series across expiries under a recorded roll rule and
a back-adjustment flag. Research runs on it; the per-contract raw series stay
in the lake because fills happen on a specific expiry.
_Avoid_: front month (one expiry, not the series), rolled series (does not say
whether it was back-adjusted)

**Dataset Manifest**:
The single JSON index of every dataset in the lake — source, licence,
instrument, granularity, date range, row count, sha256. A dataset not listed
there does not exist for the harness; its hash is what a KPI Contract freezes.
_Avoid_: catalog (implies browsing), data registry (collides with the
recipe registry)

**Order Book**:
The B3 limit order book at a declared depth — the queue of resting buy and
sell offers at each price — reconstructed from order events (add, modify,
cancel, trade). "Order-book data" means event-level or snapshot data that
allows that reconstruction; a trade tape alone is not order-book data.
_Avoid_: book alone (reads as a published book — it confused the human once),
level 2 (a depth, not the thing), DOM (a platform's widget name)

**Tape**:
The sequence of executed trades with timestamp, price, quantity, aggressor
side and, when present, buyer and seller broker codes.
_Avoid_: times and trades (platform jargon for the same thing), ticks (MT5's
word covers bid/ask updates too)

**Aggressor**:
The side that consumed liquidity in a trade — a buy that lifted the ask or a
sell that hit the bid — classified from the tape against the quotes (or
carried as a flag when the source provides it). Aggressor-signed volume over a
window is the order-flow imbalance the intraday models read.
_Avoid_: agressão (platform jargon for the same thing), initiator (ambiguous
between the order and the participant)

**Participant Code**:
The B3 identifier of the trading participant (brokerage) on each side of a
trade or order, present in some public and purchased data and anonymised in
others. A Big-Player Imprint is only computable where it is present; every
dataset manifest states whether it is.
_Avoid_: broker name (a mapping that changes; the code is the key), player
(a platform's word for a participant it chose to display)

**Big-Player Imprint**:
A measurable, persistent pattern in the Tape or Order Book attributable to one
participant's flow — broker-tagged aggression, repeated refills at one price,
size clustering — used as a feature, never as a narrative.
_Avoid_: smart money (unmeasurable), player tracking (a retail-platform
product name)

**Iceberg Order**:
A limit order whose displayed quantity is a fraction of its total, inferred
from refills at the same price and side after fills; a detector's output is a
probability per price level, scored against the reconstructed Order Book.
_Avoid_: hidden order (B3 also has fully hidden order types; not the same)

**Cost Model**:
The harness's charge per side: B3 fees from the versioned fee table plus two
ticks of slippage, applied to every fill before any P&L is reported. A harness
path; never a candidate's parameter.
_Avoid_: commission (only one component), friction (vague)

**Fold Set**:
A named, hash-keyed set of purged, embargoed walk-forward train/test windows
over one dataset hash. Every score names its Fold Set; an optimize loop never
sees the Held-out Year.
_Avoid_: train/test split (one split, not a sequence), cross-validation
(implies shuffling, which leaks)

**Held-out Year**:
The last twelve months of every dataset, excluded from every Fold Set an
optimize loop may use and scored once per track by the driver at the end of a
run.
_Avoid_: test set (every fold has one), validation period

**Forecast Skill**:
One minus the ratio of a model's out-of-sample loss to the naive baseline's
loss on the same Fold Set; the objective of model loops. Zero means no better
than naive; negative means worse.
_Avoid_: accuracy (directional accuracy is one loss among several, and a
poor one), R² (the regression special case)

**Net Sharpe**:
Annualised Sharpe ratio of the net-of-Cost-Model daily P&L series across a
Fold Set's test windows; the objective of strategy loops.
_Avoid_: Sharpe (ambiguous about costs), return (ignores risk)

**Leakage Probe**:
A harness check that runs before scoring — shuffled labels, future-shifted
features, timestamp monotonicity, point-in-time feature checks — whose
failure is a crash naming the probe, never a score.
_Avoid_: sanity check (anything is one), unit test (these run on data, not
code)

**Track**:
One line of inquiry in the pipeline with its own research unit, baseline,
candidates and verdict: swing forecasting, intraday forecasting, book
microstructure, pairs, chart patterns, reinforcement learning, news.
_Avoid_: sphere (the brainstorm's loose word), strategy (a track may contain
several or none)

**Strategy**:
A deterministic rule that maps data available at time t to a target position
at t, scored only through the harness under the Cost Model. A model is not a
strategy until a rule layer maps its forecast to positions.
_Avoid_: bot, robot (imply live trading), signal (an input to a strategy, not
one)

**Track Verdict**:
The go/no-go per Track at the end of a run: champion and its KPI, ledger
digest, Held-out Year result, data gaps, recommended next scope.
_Avoid_: result (every evaluation has one), conclusion
