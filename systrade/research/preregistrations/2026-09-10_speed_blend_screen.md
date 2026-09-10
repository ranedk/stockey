# Pre-registration — trend speed blend, kill screen

Written 2026-09-10, after the construction measurements — costs and
correlations only, `research/reports/2026-09-10_speed_blend_construction.txt` —
and before any return, Sharpe ratio or edge of the blended book has been
computed. The configuration is already frozen in code (`rules.SpeedBlend`,
`paper.SpeedBlendSpec`) and in `docs/strategies/2026-09-10_trend_speed_blend.md`.

## 1. What is tested

One configuration, **trials=1**: trend-quintile's book with its selection
signal replaced by the handcrafted blend of EWMAC 16/64, 32/128 and 64/256 at
40/16/44 with FDM 1.10. Nothing was searched: the weights came from Table 8
on correlations and Table 12 on costs, the rest is trend-quintile's frozen
configuration unchanged.

## 2. What this screen can and cannot do

It runs on the 2013-2026 sample that rows 17-18 mined, so — exactly as row 18
said of itself — **it can kill the blend and cannot confirm it**. A good
number here is what a blend of three speeds of an already-mined premium
produces by construction. The only confirmation is the forward paper record.

## 3. Setup (fixed)

- NSE adjusted EQ, decisions 2013-07-01 → 2026-06-30, Rs 10 crore 60-bar
  median turnover floor, top quintile of the blended forecast, equal weight,
  rebalance every 20 decision days, decide at close / fill at next open.
- `internal/sleeve` with its equal-weight (beta) and stable-shuffle
  (turnover-matched selection) controls, shuffle seeds 1-5.
- Costs 50 bps round trip, and 100 bps as the sensitivity.
- Monthly paired differences; one-sided stationary block bootstrap, mean block
  5 months (n^1/3), 10,000 resamples, seed 1; 90% intervals.
- Command: `slice blend` (defaults).

## 4. Kill criteria — any one kills, and the forward track does not start

- **K1:** at 50 bps, bootstrap p ≥ 0.10 against EITHER control.
- **K2:** at 100 bps, a negative mean monthly difference against either
  control.

## 5. Reported, deciding nothing

- The blend at trend-quintile's realised risk against trend-quintile's own
  book (`evidence.ScaleToRisk`, LEDGER row 24's standard reading). It decides
  nothing: choosing between the two on this sample is Law 5's
  pick-the-winner, and the two tracks' forward records are compared only at
  the joint evaluation the spec fixes.
- The probabilistic Sharpe ratio of the excess over the tougher control
  (one trial).
- The workspace-wide Bonferroni bar, for context.

## 6. Ledger

LEDGER row 26, trials=1, whatever the outcome.
