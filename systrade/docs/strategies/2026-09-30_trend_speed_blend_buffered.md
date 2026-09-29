# Strategy spec — trend speed blend, rank-buffered (NSE cash equity)

**Status: FROZEN 2026-09-29.** The forward record starts 2026-09-30. Pre-registered in
`research/LEDGER.md` row 50; the operator chose this single track over a quarterly
trend-quintile variant and over running both (TODO A3).

This is trend-speed-blend (`docs/strategies/2026-09-10_trend_speed_blend.md`) with ONE
change: a **rank buffer**. It is a separate, parallel forward record — **not a
replacement**. trend-speed-blend's record, clock and judgement are untouched, and so are
the other four tracks.

## 1. What is traded

Identical to trend-speed-blend in every respect but selection:

- **Universe, signal, weights, rebalance (every 20 trading days), execution (decide at
  the close, fill at the next open), costs (50 bps round trip), controls (same seed):**
  exactly trend-speed-blend's §1.
- **Selection with the buffer:** at each rebalance the book holds the top quintile by the
  speed-blend forecast, EXCEPT that a name already held is kept while it stays inside the
  top **two** quintiles (`paper.Spec.KeepMultiple = 2`); the slots left are filled with
  the best-ranked names not held. The book always holds exactly one quintile's count.

## 2. Why a buffer

A cross-sectional book rebalanced monthly sells a name the moment its rank dips just
below the cut, and buys it back when it recovers — turnover that pays costs and turns
long-term gains into short-term ones. LEDGER row 49 (in-sample, 2013-07 → 2021-12):
turnover 903 → 504%/yr, churn per rebalance 36 → 20%, long-term share of gains 3 → 23%,
net 26.0 → 27.5%/yr, after tax (A1 model, upper bound) 20.9 → 22.6%/yr. The buffer
also raised the GROSS return (28.9 → 29.1%), i.e. the plain rule sells trend winners
early. The monthly-vs-quarterly comparison (same report family, 2026-09-29) was
inconsistent across the two trend tracks, so the clock is NOT changed here.

## 3. Where every number came from

- `KeepMultiple = 2`: the smallest buffer measured; 3 was no better on any track (row 49).
  One free choice, declared here, not searched further.
- Everything else: trend-speed-blend's §3, unchanged.

## 4. What this track is not

- Not a replacement for trend-speed-blend on a short record.
- Not evidence yet: row 49 is in-sample and K was chosen after seeing it.

## 5. Pre-registered evaluation (binding)

- **trend-speed-blend's §5 terms apply verbatim** (against both controls from day one; no
  judgement before 12 months AND 12 rebalances; success = positive mean monthly difference
  against BOTH controls; kill = negative mean monthly difference against equal-weight over
  18 months, or realised round-trip costs above 100 bps; frozen for the whole window).
- **Plus the question it exists for:** at the first date both it and trend-speed-blend
  have 12 months and 12 rebalances (from 2027-09-30), a paired monthly comparison of the
  two AFTER costs, with realised turnover and the realised long-term share of gains
  reported beside it. The buffer is adopted for the trend family only if this track is
  ahead after costs AND its realised turnover is at least a third lower; otherwise the
  unbuffered rule stands.

## 6. How it is tracked

- `cmd/paper run -strategy trend-speed-blend-buffered` in `scripts/paper_daily.sh`,
  writing the same `systrader_paper_*` tables under its own name; `cmd/dhan orders
  -strategy all` picks it up (dry runs); screener/'s Paper page lists it.
