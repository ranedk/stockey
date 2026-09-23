# Pre-registration — combination policies beyond the handcrafted one

Written 2026-09-23, before any policy's book has been computed. Closes the
README item "Combination policies beyond the handcrafted one (Hedge | ML),
judged vs the same matched-control baseline", open since LEDGER row 34.

## 1. The question

The live `momentum-lowvol-combination` track (row 34) holds the momentum
lookback blend and the low-volatility blend at **48/52**, weights set from
Law 6 inputs only — the branches' cost of trading (a fact) and their
correlation (estimable), never their backtested returns. That is one policy
for setting the split. This asks whether any policy that *moves* the split
over time — on realised performance, on realised risk, or on the market
regime — beats it.

Nothing here can promote anything. A policy that wins on these years wins on
the years that produced both parents and the combination itself; the only
route to capital remains a pre-registered forward paper track.

## 2. What is tested — one family, trials = 8

The two branches are the frozen tracks' own books at their own frozen weights
(`paper.MomentumLookbackBlendSpec` 33/33/17/17, `paper.LowVolBlendSpec`
11/11/11/33/34), exactly as row 34 built them. Every policy sets one number
each rebalance: `w_mom`, the momentum branch's share; the low-risk branch
takes `1 - w_mom`. All weights are set from data available **before** the
decision date.

**Arm A — Hedge (performance-adaptive), 4 cells.** Multiplicative weights
(Freund–Schapire): a branch's share is proportional to `exp(m_i / s)`, where
`m_i` is the exponentially weighted mean of its realised net period returns
and `s` is a declared scale. The one knob is the **half-life of that memory:
6, 12, 24, 48 months**. `s = 1%/month` (`policy.HedgeScale`), fixed here
rather than fitted: a branch running one percent a month ahead of the other
gets `exp(1)` times the weight — a 73/27 split — and one percent a month is
the size of the edges this ledger actually measures (rows 30-34 report
0.4-0.9%/month).

**Arm B — risk parity (volatility-adaptive, no returns), 3 cells.** `w_mom ∝
1/σ_mom`, with σ each branch's realised daily-return volatility over a
trailing **3, 6 or 12 months**. Uses risk, which Law 6 calls estimable; uses
no return.

**Arm C — conditional (ML), 1 cell.** Ridge regression (shrinkage λ = 1.0 on
standardised features, declared) predicting the next month's difference of
branch returns from four features known at the decision date: the market's
12-month trailing return; the market's 60-day realised volatility; breadth
(share of the eligible universe above its 200-day average); and the branches'
own 12-month trailing return difference. Market and breadth as the existing
code defines them (`traits.MarketReturns`, `paper.breadthOf`, Rs 10 cr
universe). Fit on an **expanding window, minimum 3 years, stepped 1 year,
purged by 31 calendar days** — the 21-trading-day decision horizon, converted
as `research.PurgedWalkForward`'s own doc comment sets out; a fold's weights
are never fit on data the fold scores. Prediction mapped to a
weight by `w_mom = clip(0.48 + 0.20·ẑ, 0.20, 0.80)`, where
`ẑ = (prediction − mean label) / sd(label)`, both measured on the fit window:
a model that forecasts a branch difference of the size that difference
actually has gets the full twenty points, and a model that explains nothing
predicts near the mean and barely moves the split. Standardising instead by
the spread of the PREDICTIONS was the first version and is wrong — it
rescales any fit to full size, so a model with no explanatory power would
still tilt to its bounds; the synthetic null caught it before any market data
was read (`TestRidgeOnNoiseKeepsNoSystematicTilt`). The map is centred on the
incumbent's 48.

Arms A, B and C are one family — they answer one question, and judging them
apart would be the searched-family fiction the protocol exists to stop.
**Trials = 8.**

## 3. Controls

- **The handcrafted combination book** (48/52, from the spec, so it cannot
  drift from what paper trades) — the decisive comparison.
- **Equal-weight universe** and **stable-shuffle** books, summed at each
  policy's own time-varying branch weights, so the numbers stay comparable to
  rows 33 and 34.
- **Arm C only: its own time-shifted twin** — the same fit and map run on
  features lagged a further 12 months. A conditional policy that cannot beat
  its own stale-feature copy is reading noise (the device row 16 used).

## 4. Setup (fixed)

- NSE adjusted EQ, decisions **2013-07-01 → 2021-12-31**, Rs 10 crore floor,
  monthly (21 trading days), next-open fills; the combination's first date is
  the first on which all nine member books exist. 2022+ stays unread.
- Member books from `internal/sleeve`, summed at the policy's branch weights.
- **Branch-weight turnover is charged.** Moving the split trades: each
  rebalance adds one-way turnover `½·Σ|Δw|` at the branch level, at the same
  cost as the member books. A policy that pays for its own adaptation is the
  only fair version of this test; the incumbent pays nothing because its
  weights never move.
- **50 bps round trip, 100 bps as the sensitivity.**
- **Deciding statistic: the edge at MATCHED risk** (row 24's reading device —
  each policy's book scaled, with hindsight, to the volatility of the control
  it is compared with), as for both parents and row 34. The raw edge is
  reported beside it.
- Monthly paired differences; one-sided stationary block bootstrap, mean block
  5 months, 10,000 resamples, **seed 1**, declared and never drawn.
- BH-FDR at q = 0.10 over the 8; deflated Sharpe against trials = 8.
- Command: `slice combine`; report
  `research/reports/2026-09-23_combination_policies.txt`.

## 5. What each arm predicts, before the run

- **Hedge: expected to fail.** It is selection dressed as adaptation, and Law
  5's gold experiment says selection destroys value. Worse here: the branches'
  excess returns correlate −0.12, so weighting toward the recent winner should
  systematically buy the branch about to lag. It is run because the README
  names it and because "we expected it to fail" is only a claim if it is
  written down first.
- **Risk parity: the arm with a story.** The momentum blend ran ~25%
  volatility on these years and the low-volatility blend ~15%, so 48/52 of
  CAPITAL is roughly 60/40 of RISK. Law 10 says risk is the dial; equalising
  it between branches is that law applied inside the combination. If anything
  here beats the incumbent, this is what should.
- **Conditional: a story and evidence against it.** For: this workspace's own
  history is that conditioning worked (rows 3, 5) where selection did not, and
  row 29 found momentum weakest exactly among the lowest-volatility names.
  Against: rows 20–21 killed every regime overlay on exposure, and row 38's
  market-stage filter rescued nothing.

## 6. Decision rule

A cell is a **candidate** only if all of:

- **P1:** matched-risk paired edge over the handcrafted combination with
  bootstrap p < 0.10, surviving BH-FDR at q = 0.10 within the 8.
- **P2:** positive mean monthly difference over the handcrafted combination at
  100 bps.
- **P3 (arm C only):** also a positive mean over its time-shifted twin.

Consequences, fixed now:

- **No cell survives → the handcrafted policy stands.** The README item closes
  as answered, and the live track is unchanged.
- **Cells survive → nothing changes today.** Law 5 forbids taking the best
  cell: if more than one cell of an arm survives, that arm enters as the
  handcrafted blend of its surviving cells or not at all. A survivor is a
  candidate for a NEW pre-registered forward track beside the existing five,
  replacing none of them, and judged forward at matched risk.
- The live `momentum-lowvol-combination` track keeps running either way. Its
  frozen weights are not touched by this result (Law 19).

## 7. Ledger

One row, **trials = 8**, whatever the outcome.
