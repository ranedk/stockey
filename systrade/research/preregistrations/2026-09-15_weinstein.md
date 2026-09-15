# Pre-registration — Stan Weinstein's stage analysis on NSE

Written 2026-09-15, before any return of any stage-conditioned book has been
computed. The classifier (`internal/stage`, LEDGER row 10) was built in
August as reporting only; its parameters were fixed then, from the book,
and are not revisited here.

## 1. The question

Does Weinstein's method add anything to what the momentum tracks already
trade? Stage 2 — price above a rising 30-week average — overlaps heavily with
trend and momentum. What is new is the ENTRY (a breakout from a flat base,
confirmed by volume and relative strength) and the EXIT (a weekly close below
the 30-week average), which our monthly quintile books do not have.

Parts of it were tested before in other clothes: the market-stage filter is
close to the 200-day regime floor LEDGER row 20 rejected, and row 21 found no
per-position stop that reduced drawdown. Expectation, stated in advance: it
re-finds the momentum premium; the exit is the part most likely to matter.

## 2. Fixed inputs (Weinstein 1988; none searched)

- Weekly bars: last close and summed volume per ISO week, from the adjusted
  daily cache. A day's stage is its LAST COMPLETED week's, so nothing uses a
  close after the decision.
- Stages (`internal/stage`): 30-week SMA, 4-week slope, ±1% flat band;
  Stage 1/3 disambiguated by the last clear trend.
- Breakout confirmation: breakout-week volume ≥ 2× the 10-week average
  ("at least twice"); Mansfield relative strength > 0 — the stock's price
  relative to the market, against its own 52-week average of that ratio. The
  market is the equal-weight index of the Rs 10 cr universe (the same one the
  beta traits use).
- Exit: a weekly close below the 30-week average.

## 3. Setup (the same as every screen)

NSE adjusted EQ, decisions 2013-07-01 → 2021-12-31 (2022+ stays unread),
Rs 10 crore floor, equal weight across held names, decide at a close and fill
at the next open, rebalanced weekly (every 5 trading days — Weinstein reviews
weekly), 50 bps round trip with 100 bps as the sensitivity. Controls: the
equal-weight universe, and the stable-shuffle book — the same number of
names chosen at random from the same eligible set and held (LEDGER row 31).
Monthly paired differences, one-sided stationary block bootstrap, mean block
5 months, 10,000 resamples, seed 1.

## 4. Step 1 — do the stages sort future returns? (kill screen, trials=4)

Four books: every name in Stage 1, 2, 3, 4. Each against its own same-size
random group. `slice stage -part buckets`.

- **Proceed to step 2 only if** the Stage 2 book beats its same-size random
  group at 50 bps with bootstrap p < 0.10. Otherwise stop: the label does not
  carry information on this market.
- Reported, not deciding: the other stages, the shape (2 above 1/3 above 4).

## 5. Step 2 — three trading versions (one family, trials=3)

- **(a) hold Stage 2:** enter when a name's week is Stage 2; exit on a weekly
  close below the 30-week average.
- **(b) breakouts:** enter only in the week a name turns Stage 1 → Stage 2
  with volume ≥ 2× and Mansfield RS > 0; exit as (a).
- **(c) breakouts in a Stage 2 market:** (b), taking new entries only while
  the market index itself is in Stage 2.

Deciding statistic: the edge at MATCHED risk over the tougher of the two
controls (breakout books can sit partly in cash; the stable-shuffle control
holds the same count, the equal-weight one does not). A version SURVIVES if
it beats both controls at matched risk, is a discovery under FDR 10% within
these three with the deflated Sharpe over three trials reported, and its
edge at 100 bps is not negative against either control.

**Overlap:** the daily gross excess return (book − equal-weight) of every
surviving version is correlated with the momentum lookback blend's, run in
the same harness on the same days. Correlation ≥ 0.8: the same bet — it joins
the momentum family as a variant rather than becoming a track. Below 0.8: a
candidate sixth paper track, frozen in its own spec before its forward record
starts.

## 6. Ledger

Row 37 (step 1, trials=4) and, if step 1 passes, row 38 (step 2, trials=3),
whatever the outcome. M is a record only (Law 2, second amendment).
Reports: `research/reports/2026-09-15_weinstein_buckets.txt`,
`research/reports/2026-09-15_weinstein_strategy.txt`.
