# Pre-registration — post-results drift ("excellent results keep running")

Written 2026-10-15, before the data for it was fetched. Operator's question after LEDGER row 57
failed its check: if we look at companies that declared EXCELLENT results — not momentum
stocks that merely improved — is there a trend? This is post-earnings-announcement drift, a
documented anomaly; it has never been tested in this workspace (factor research covered
growth/quality/ownership levels, not results surprises; row 48 is a forward record only).

## 1. Data

- Universe: NSE EQ stocks, sectors other than financial services and diversified (their XBRL
  layout differs), with 60-session median traded value >= Rs 5 cr at the result date.
- Results: NSE XBRL quarterly results (`trigger_study/nse_results.py`), quarters 2019-07 to
  2024-12, one basis per company, the exact publish time (`broadCastDate`).
- Prices: `advisory_adjusted_ohlcv_daily` (systrader DB), daily adjusted closes.
- Industry index: equal-weight daily return of eligible members of the stock's Sharpely
  industry (>= 5 members, else its sector, else the market), each day capped at +-20%.

## 2. Event timing

- s0 = the last session whose close is BEFORE the publish time (a result published after
  15:30 IST belongs to the next session).
- **Announcement return (AR):** stock vs industry from the close of s0 to the close of s0+2.
- **Entry:** close of s0+2 — after the announcement window, so the signal never overlaps the
  measured return. **Outcome:** stock vs industry from entry over **60 sessions** (primary);
  20 and 40 reported.

## 3. "Excellent" — three definitions, one family (trials = 3)

Ranked within the quarter's cross-section (all results for the same period end that pass §1):
- **A. Numbers:** mean percentile of YoY net-profit growth and YoY sales growth (year-ago
  quarter on the same basis; profit growth needs a year-ago profit >= max(Rs 5 cr, 5% of
  sales), a loss-to-profit swing ranks at the top). Excellent = top tenth.
- **B. Market verdict:** AR (§2). Excellent = top tenth.
- **C. Both:** in the top fifth on A AND the top fifth on B.

## 4. Test

- Statistic: mean 60-session outcome of the excellent group minus the rest of the cross-section,
  **after charging the excellent group 50 bps** (a round trip; the rest are not traded).
- p: one-sided, cluster bootstrap over calendar months of entry (10,000 reps, seed 1).
- Family: A, B, C; Benjamini-Hochberg within the family.
- **In-sample PASS** for a definition = net difference >= +2 pp over 60 sessions, BH q < 0.10,
  and the difference positive in at least 2 of the 3 years 2020-2022.

## 5. Years

- **In-sample:** results with year-on-year comparisons, published 2020-10-01 to 2022-12-31.
- **Check (once, unchanged rule):** results published 2023-01-01 to 2025-03-31 (quarters to
  Dec 2024). Caveat stated now: these years were already looked at for MOMENTUM entries in row
  57; that sample overlaps this one only in part (momentum names around results), so the check
  is not fully virgin. A clean confirmation needs results from 2025 on (NSE's newer integrated
  filing, not yet read) — a passing check leads there, and then to a paper track.

## 6. Ledger

LEDGER row 58, trials = 3. A fail is recorded and closes the idea for these definitions.
