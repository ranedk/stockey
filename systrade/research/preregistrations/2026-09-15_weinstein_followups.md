# Pre-registration — two Weinstein follow-ups from the slice map

Written 2026-09-15, after LEDGER rows 37-39 and before either book below has
been computed. Both come from row 39's exploration map, so both are tests of
hypotheses the exploration years already suggested: they can kill, never
confirm — the forward paper record confirms.

## 1. The two hypotheses

- **(d) Early Stage 2.** Row 39: Stage 2 works least where the move is
  extended — the steepest 30-week MA (the only ◆, −5.0) and names furthest
  above it (◆, −4.5). Book: every Stage 2 name EXCEPT, each decision day, the
  top fifth of that day's Stage 2 names by 30-week MA slope OR the top fifth
  by distance above the 30-week MA. Membership is re-read every week; there is
  no hold-through rule.
- **(e) A faithful Weinstein breakout.** Row 39: our Stage 2 needs the MA
  already rising > 1% in 4 weeks, which a flat base has not done in its
  breakout week, so rows 38's "Stage 1 → 2 on 2× volume" fired on 129 days in
  eight years. The book's buy point instead: the week a name closes above the
  highest weekly close of the prior 30 weeks (the top of the base — the MA's
  own window, no new number), having been in a base the week before (the
  prior week's 30-week MA slope within ±1%, the flat band), with the MA not
  falling (this week's slope ≥ −1%), the close above the MA, volume ≥ 2× its
  10-week average, and Mansfield RS > 0. Held until a weekly close below the
  30-week MA (as rows 38's versions).

## 2. Setup (as rows 37-38)

NSE adjusted EQ, **Rs 10 crore floor** (the tradable universe — row 39's
strongest slices sat below it and are out of scope), decisions 2013-07-01 →
2021-12-31, weekly stages from the last completed week, equal weight across
held names, next-open fills, rebalanced every 5 trading days, 50 bps round
trip (100 bps sensitivity). Controls: the equal-weight universe and the
same-size stable-shuffle book. Monthly paired differences, block bootstrap
(5-month blocks, 10,000 reps, seed 1). Momentum lookback blend run alongside
for the overlap.

## 3. Deciding (one family, trials=2)

As row 38: a version SURVIVES if its matched-risk edge beats both controls, it
is an FDR-10% discovery within these two, and its 100-bps edge is not negative
against either. Overlap: correlation of daily gross excess with the momentum
blend's; ≥ 0.8 = the same bet.

Additionally, for (d) only — because its hypothesis is an IMPROVEMENT: its
monthly net return must beat the plain Stage 2 book's (the same universe and
clock) with bootstrap p < 0.10, or the exclusion is not what did the work.

For (e), reported and not deciding: entries a year and mean names held, so a
too-sparse book cannot pass unnoticed.

## 4. Ledger

LEDGER row 40, trials=2, whatever the outcome. Command:
`slice stage -part followups`; report
`research/reports/2026-09-15_weinstein_followups.txt`.
