# Strategy spec — cross-sectional trend quintile (NSE cash equity)

**Status: TODO — not live, not confirmed, not promoted.** This document exists
so the configuration is frozen in writing BEFORE any forward data is collected.
Once paper trading starts, changing anything below restarts the clock.

Evidence so far: LEDGER rows 17 (exploration) and 18 (cost screen). Both ran on
the 2013-2026 sample, and row 17 mined it. **Nothing here is confirmed**, and
the forward record this spec describes is the only evidence that will be.

## 1. What it trades

- **Universe:** NSE cash equity, adjusted. Eligible on a decision day if its
  60-bar median traded value is at or above **Rs 10 crore**. The Rs 1 crore
  version scores better in backtest; the Rs 10 crore version is the one that
  can actually absorb capital, and it is deliberately the weaker of the two.
- **Signal:** `rules.EWMAC{Fast: 32}` — the 32/128 crossover, vol-normalized,
  long-only clipped to [0, +20]. Carver's published scalar, unchanged.
- **Selection:** the top quintile of that day's eligible universe by forecast.
- **Weights:** equal across the held names. Gross exposure 1.0 — fully
  invested, no leverage, no shorting, no cash timing.
- **Rebalance:** every 20 trading days. Between rebalances, hold.
- **Execution:** decide at the close, fill at the next open.

Nothing is conditioned on market regime. Row 17's washout inversion looked
striking and row 18 showed it moves an annual number by nothing, because
washouts are 3% of days; it is deliberately NOT in this spec.

## 2. Why it should work

Trend followers are paid by disposition-effect sellers exiting winners early,
by mandate-driven rebalancers selling strength, and by anchored traders fading
moves on stale information. Cross-sectionally, that under-reaction shows up as
recent relative winners continuing to outperform. This is the documented
momentum premium; we are almost certainly re-finding it rather than
discovering anything, which is the good case — it comes with a literature, a
mechanism, and a known failure mode.

**Known failure mode:** momentum crashes. Sharp market rebounds off a bottom
punish a book holding the prior winners. The backtest's −41% drawdown is that,
and a live one will feel worse than a backtested one.

## 3. What the backtest said, and what it is worth

At the Rs 10 crore floor, 50bps round trip, 2013-2026: 20.11%/yr, SR 0.93,
against an equal-weight book of the same universe at 12.28% and SR 0.64
(+0.55%/month, t=2.89), and against a turnover-matched random ranking at
12.32% (+0.51%/month, t=2.65). At doubled costs, +0.40%/month, t=2.13. Both
halves positive. Nine rule-speed x rebalance combinations land between 18.2%
and 22.2% a year.

Read those t-statistics as descriptions, not evidence: the hypothesis was mined
from the same sample. The honest summary is "this is what the premium looks
like in-sample, and it is not fragile to the arbitrary choices".

## 4. What paper trading must answer

1. **Does the edge survive out of sample at all?** Everything above could be
   the residue of mining.
2. **What do fills actually cost?** The modelled 50bps is an estimate. The
   backtest edge is thinnest exactly where it is most tradable, so a 30bps
   error in slippage is the difference between a strategy and a donation.
3. **What is the capacity?** Record the traded value against each name's
   median turnover, so the size at which impact starts eating the edge is
   measured rather than assumed.

## 5. Pre-registered evaluation (binding)

- **Track from day one:** the strategy book, an equal-weight book of the same
  eligible universe, and a fixed-seed random-ranking book of the same
  turnover. All three on the same days, the same costs, the same fills.
- **Minimum before any judgement: 12 months and 12 rebalances.** No reading
  the tape at month three and concluding anything.
- **Success:** positive mean monthly difference against BOTH benchmarks over
  the evaluation window.
- **Kill:** a negative mean monthly difference against the equal-weight book
  over 18 months, or realized round-trip costs above 100bps, stops the
  strategy. Either outcome is a ledger row.
- **Frozen:** no parameter, universe or weighting change during the window. A
  change means a new spec, a new clock, and a note saying why.

## 6. What is needed to run it

- The daily order sheet: eligible universe, forecasts, target quintile,
  the trades implied by the current book, on a 20-day clock.
- A record of intended versus achieved fills, per name, per rebalance.
- Nothing else. No live broker connection is required to start — the value is
  in the honest forward record, and paper fills at the next open are exactly
  what the backtest assumed.
