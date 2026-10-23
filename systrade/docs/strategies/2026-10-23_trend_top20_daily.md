# Strategy spec — trend top 20, checked daily, only changes traded (NSE cash equity)

**Status: FROZEN 2026-10-23.** The forward record starts 2026-10-26. From LEDGER rows 81-82
(the operator's idea: a book of few names that trades only when a name enters or leaves).
A separate, parallel forward record: every other track is untouched.

## 1. What is traded

- **Universe, signal, execution, costs, controls:** exactly trend-speed-blend's §1
  (`docs/strategies/2026-09-10_trend_speed_blend.md`): the speed-blend EWMAC forecast,
  decide at the close, fill at the next open, 50 bps round trip, same random seed.
- **Selection, every session:** hold the **20** names with the highest forecast. A name
  already held is kept while it ranks inside the top **60** (`KeepMultiple = 3`); a name
  that falls outside is sold, and its slot goes to the best-ranked name not held.
- **Weights:** a new name gets an equal share of what the sold names were worth; names that
  stay keep the weight they drifted to — nothing is re-evened (`KeepWeights`). So a session
  with no name crossing the top-60 line trades nothing.
- Controls: equal-weight (the whole universe) and a random book of 20 names, held the same way.

## 2. Why

- Row 81: every book on a 20/21-day clock carries timing luck — which days the clock lands
  on moved results 1-8 points a year. A daily check with a wide buffer has no clock.
- Row 82: 10 names were too few (15-23%/yr on small rule changes, drawdowns −45..−55%);
  narrow buffers churned on daily rank noise; re-evening weights cost ~1%/yr. This line —
  20 names, 3x buffer, kept weights — was the best of the 8: **23.6%/yr, maxDD −37%, costs
  2.4%/yr, 2016-01..2026-10**, against 20.1% / −39% for the quintile book from the same start.

## 3. Where every number came from

- 20 names, 3x buffer: the best of row 82's 8 variants on data already seen. **In-sample,
  chosen after seeing it — not evidence.** Only this forward record counts.
- Everything else: trend-speed-blend's §3, unchanged.

## 4. Pre-registered evaluation (binding)

- **trend-speed-blend's §5 terms apply verbatim** (against both controls from day one; no
  judgement before 12 months; success = positive mean monthly difference against BOTH
  controls; kill = negative mean monthly difference against equal-weight over 18 months,
  or realised round-trip costs above 100 bps; frozen for the whole window).
- **Plus the question it exists for:** at 12 months, a paired monthly comparison against
  trend-speed-blend (the same score as a quintile on a 20-day clock) after costs, with
  realised turnover beside it. Ahead after costs -> the daily, few-names form is the better
  way to hold this score; behind -> the quintile book stands.

## 5. How it is tracked

- `cmd/paper run -strategy trend-top20-daily` in `scripts/paper_daily.sh`, writing the same
  `systrader_paper_*` tables under its own name; `cmd/dhan orders -strategy all` picks it up
  (dry runs); screener/'s Paper page lists it.
