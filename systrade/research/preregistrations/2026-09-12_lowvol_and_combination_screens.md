# Pre-registration — kill screens for the low-volatility blend and the combination

Written 2026-09-12, after both construction measurements (costs and
correlations only: `research/reports/2026-09-12_lowvol_blend_construction.txt`,
`research/reports/2026-09-12_combination_construction.txt`) and before any
return of either blended book has been computed. Both configurations are
frozen in code (`paper.LowVolBlendSpec`, `paper.MomentumLowVolCombinationSpec`)
and the screens read their weights from there.

## 1. What is tested

Two configurations, **trials=1 each** (LEDGER rows 33 and 34):

- the low-volatility blend: 11/11/11/33/34 over the lowest-risk quintiles by
  3/6/12-month volatility, 1-year beta and 1-year residual volatility;
- the combination: 16/16/8/8 momentum and 6/6/6/17/17 low risk, nine books.

## 2. What they can and cannot do

Every member was measured in rows 30 and 32 on the same years, so these
screens are close to a formality: they can kill a blend (if combining the
books loses what each had) and cannot confirm one. The confirmation is the
forward paper record.

## 3. Setup (fixed)

- NSE adjusted EQ, decisions 2013-07-01 → 2021-12-31, Rs 10 crore floor; each
  blend from the first date every member has a book.
- Member books from `internal/sleeve` at a 21-trading-day rebalance, summed at
  the frozen weights; the equal-weight and stable-shuffle controls summed at
  the same weights (reproducible, LEDGER row 31).
- **Deciding statistic: the edge at MATCHED risk** — the blended book scaled,
  with hindsight, to each control's realised volatility (row 24's reading
  device), because both books hold the low-risk strategy whose claim is
  risk-adjusted. The raw edge is reported beside it.
- 50 bps round trip, 100 bps as the sensitivity; monthly paired differences;
  one-sided stationary block bootstrap, mean block 5 months, 10,000 resamples,
  seed 1.
- Commands: `slice lookbacks -family lowvol -screen`,
  `slice lookbacks -family combo -screen`.

## 4. Kill criteria — any one kills that track

- **K1:** at 50 bps, bootstrap p ≥ 0.10 at matched risk against EITHER control.
- **K2:** at 100 bps, a negative mean monthly difference at matched risk
  against either control.

## 5. Ledger

LEDGER rows 33 (low-volatility blend) and 34 (combination), trials=1 each,
whatever the outcome.
