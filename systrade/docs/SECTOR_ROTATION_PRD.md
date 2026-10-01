# Sector rotation, relative strength and early Stage 2 -- PRD

Operator request 2026-10-01: "Find sector rotation, RS and Stage 2 winners early", replacing
the plain stage listing on the screener's /stages page, and a strategy built on it. This is a
price/TA strategy, so it lives in systrader; stockey's fundamentals enter only as a filter.

## 1. What we already know (do not re-test)

- Rows 37-40 (2013-07 -> 2021-12): holding Stage 2 names earns the momentum premium (17.1%/yr
  vs 13.6% equal-weight) and nothing the momentum tracks do not hold more cheaply. Breakouts
  (classifier-based and faithful), "early Stage 2" (dropping extended names) and a market-stage
  filter on breakouts all FAILED. Within Stage 2 the top fifth by Mansfield RS lagged its slice.
  The exploration years are spent on stock-level stage rules.
- Factor research (stockey, row 4 + memory): fundamentals carry no cross-regime stock-selection
  edge; they are useful to AVOID bad companies, not to pick winners.
- Rows 49-50: trading weekly eats the edge; a monthly clock with a 2x rank buffer is what helped
  after tax.
- NOT tested: the INDUSTRY layer -- ranking industries by relative strength and buying inside
  the leaders. Industry momentum is its own documented anomaly (Moskowitz-Grinblatt 1999:
  industries that led over 6 months keep leading for months), not a Weinstein variant. That is
  the one new hypothesis here.

## 2. The strategy, top-down

1. **Market** -- the equal-weight market index (Rs 10 cr universe) and its Weinstein stage;
   breadth = share of stocks in Stage 2.
2. **Industry** -- 58 Sharpely/NSE industries (`master_sharpely_equity.industry_code`). Each
   industry's equal-weight index, its 26-week return relative to the market (RS26), its rank,
   and its own stage. LEADING = top fifth by RS26 AND the industry index in Stage 2.
3. **Stock** -- Stage 2 names in leading industries, ranked by their own 26-week relative
   return; Rs 10 cr median traded value floor.
4. **Fundamental filter (forward only)** -- no stockey flaw and story score at or above the
   median. There is no point-in-time history of the story score before 2026-09-29, so this layer
   can only be tested forward on paper, never in the backtest.
5. **Book** -- 20 names, equal weight, at most 4 per industry. Monthly rebalance (20 sessions)
   with a 2x rank buffer (keep a holding while it ranks inside the top 40). Between rebalances,
   exit only on a weekly close below the 30-week average.

How long things take (to be MEASURED by the view, not assumed): a stock's Stage 2 run, an
industry's stay in the top fifth, and how often leadership changes. The view reports all three
from history so the monthly clock can be checked against reality.

## 3. Definitions (fixed now; a change is a new version)

| Item | Definition |
|---|---|
| Prices | `advisory_adjusted_ohlcv_daily`, EQ series, weekly = last close of the week |
| Market index | equal-weight daily return of names with median 63-day traded value >= Rs 10 cr |
| Industry index | equal-weight daily return of an industry's members (same floor, >= 3 members) |
| RS26 | 26-week total return of the index minus the market's |
| RS rank | RS26 rank among industries, 1 = strongest; reported now, 4 and 13 weeks ago |
| Stage | `internal/stage` (30-week SMA, 4-week slope, +-1% flat band; row 10) |
| Leading industry | RS26 in the top fifth AND industry stage 2 |
| Stock RS26 | the stock's 26-week return minus the market's |
| Early Stage 2 (view only) | weeks since the stock entered Stage 2; distance above the 30-week MA |

## 4. The view (replaces /stages)

`GET /api/rotation` (systrader cmd/api) and the screener page "Rotation":
- **Market strip** -- market index stage, breadth (% of stocks in Stage 2), as-of date.
- **Industry table** -- 58 rows: RS rank now / 4 weeks / 13 weeks (rotation arrows), RS26,
  industry stage, % of members in Stage 2, member count, sector. Leaders highlighted.
- **Candidates** -- stocks passing the industry and stock layers, with RS26, weeks in Stage 2,
  distance above the 30-week MA, volume ratio, and stockey's story score and flaws (from
  stockey's API) so the fundamental filter is visible.
- **Drill-down** -- one industry: every member with its stage and RS.
- **History stats** -- median weeks in Stage 2, median weeks an industry stays in the top fifth.
`GET /api/stage` is unchanged: stockey's portfolio still reads it.

Reporting only; no ledger trial (exploration, trials=0).

## 5. The test (pre-registered before any run)

Family "industry rotation", trials=2, 2013-07-01 -> 2021-12-31 (new family: the industry
layer has not been tested on these years), weekly decisions on the last completed week,
next-open fills, 50 bps round trip (100 bps sensitivity):
- **R1** -- the book in section 2 (steps 1-3, 5), always invested.
- **R2** -- R1, but in cash while the market index is in Stage 4.

Controls: the same-size plain Stage 2 book (row 38a's rule, 20 names by RS26, same clock and
buffer -- isolates the industry layer), the momentum lookback blend, the equal-weight universe,
and a same-size random Stage 2 book. Paired monthly differences, block bootstrap (5-month blocks,
10,000 reps, seed 1), FDR within the family.

**Pass** = beats the plain Stage 2 book at matched risk with FDR q < 0.10 after 50 bps, and is
not worse than the momentum blend. A pass goes to a forward paper track (with the fundamental
filter as a paired variant) for 6-12 months before any capital. A fail is recorded and the view
stays as a reporting tool.

## 6. Build order

1. Sync `master_sharpely_equity` and `fundamentals_sector_reference` into the local DB (small
   full-copy tables; DATA_CONTRACT updated on both copies).
2. `internal/rotation`: industry membership, weekly indices, RS26 and ranks, stages, candidates,
   history stats. `GET /api/rotation`, `GET /api/rotation/industry/{code}`.
3. stockey API: story scores by symbol, for the candidate table.
4. Screener: replace /stages with the Rotation page.
5. Pre-registration file + LEDGER row, then the backtest (`cmd/rotation`), report, ledger result.
