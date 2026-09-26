# Pre-registration — momentum lookback blend, kill screen

Written 2026-09-12, after the construction measurements (costs and
correlations only, `research/reports/2026-09-12_lookback_blend_construction.txt`)
and before any return of the blended book has been computed. The weights are
frozen in code (`paper.MomentumLookbackBlendSpec`) and the screen reads them
from there.

## 1. What is tested

One configuration, **trials=1**: the 33/33/17/17 blend of the 6, 9, 12 and
12-minus-1 month momentum books, each its variant's top quintile, rebalanced
monthly.

## 2. What it can and cannot do

Its four members were each measured in LEDGER row 30 on the same years, so
this screen is close to a formality — it can kill the blend (if combining the
books somehow loses what each had), and cannot confirm it. The confirmation
is the forward paper record.

## 3. Setup (fixed)

- NSE adjusted EQ, decisions 2013-07-01 → 2021-12-31, Rs 10 crore floor; the
  blend from the first date every member has a book (mid-2014).
- The member books from `internal/sleeve` at a 21-trading-day rebalance,
  summed at the frozen weights; the equal-weight and stable-shuffle controls
  summed at the same weights. Summing charges a name held by two variants
  twice, which the real book nets — conservative.
- 50 bps round trip; 100 bps as the sensitivity.
- Monthly paired differences; one-sided stationary block bootstrap, mean
  block 5 months, 10,000 resamples, seed 1.
- Command: `slice lookbacks -screen`.

## 4. Kill criteria — any one kills, and the forward track does not start

- **K1:** at 50 bps, bootstrap p ≥ 0.10 against EITHER control.
- **K2:** at 100 bps, a negative mean monthly difference against either control.

## 5. Ledger

LEDGER row 31, trials=1, whatever the outcome.
