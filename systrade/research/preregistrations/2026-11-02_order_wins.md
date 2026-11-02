# Pre-registration — large order wins ("a big order, and the stock keeps going")

Written 2026-11-02, before any order announcement for it was fetched or read. Operator's question
after Solar Industries (a large defence order, a strong rise) was missed by the theme watchlist:
can our data find such companies? Our feeds do not collect order announcements today.

**Prior evidence, stated now:** LEDGER row 56 tagged "order win" among triggers around momentum
entries (2016-2022, 403 stocks). Nothing survived FDR, and in capital goods order wins came
BEFORE failures more often than before continuations (18% vs 39%). That study did not use the
order's size or measure returns from the announcement. This test does both; the in-sample years
overlap row 56's for momentum names only, which is disclosed, not hidden.

**Story (Law 1):** an order is booked as revenue over years, so a large one moves future revenue
far more than the trailing numbers investors anchor on; under-reaction to material, slowly
recognised news is the same behavioural premium as post-earnings drift. A small order is noise;
a large one, relative to the company's revenue, is news.

## 1. Data

- Universe: NSE EQ stocks outside financial services and diversified (as row 58), with 60-session
  median traded value >= Rs 5 cr at s0 (§2). Symbols: every NSE EQ symbol that met the liquidity
  floor on any day in 2018-2025, delisted ones included.
- Announcements: NSE corporate announcements (`/api/corporate-announcements`, one request per
  company, 2018-01-01 to 2025-12-31), the announcement text NSE serves with each filing
  (`attchmntText`), stored in a new stockey table, NOT `fundamentals_trigger_filings` (row 56's
  check years there stay untouched).
- Classification (outcome-blind, before any return is computed): a regex prefilter (order,
  contract, letter of award / LoA, work order, purchase order, bagged, secured, awarded), then
  Claude (CLI, the tagger's model) on each candidate's text: is it the company itself winning a
  NEW order or contract (not a repeat mention, not a subsidiary's order unless consolidated, not
  an order placed BY the company, not a tender it merely bid in)? With the stated value and its
  unit copied verbatim with the sentence holding it. Code checks the sentence is in the text and
  converts the value to Rs crore (crore / lakh / million / billion; foreign currency at the RBI
  reference rate of that date, `rbi_currency_rates`). An order with no stated value is counted
  and excluded.
- Revenue: trailing four quarters' sales published BEFORE the announcement (NSE XBRL results,
  `trigger_study/nse_results.py`, exact publish times; consolidated where filed), refetched
  through the 2025-09 quarter before the check runs.
- Prices and industries: as row 58 (`advisory_adjusted_ohlcv_daily`, total-return closes;
  Sharpely industry, equal-weight, >= 5 members else sector else market, days capped at +-20%).

## 2. Event and timing

- **Large order:** stated value >= **10%** of trailing four quarters' revenue. One event per
  company per 60 sessions (the first large order; later ones inside the window are ignored).
- s0 = the last session whose close is before the announcement time. **Entry:** close of s0+1.
  **Outcome:** stock minus its control over **60 sessions** from entry (primary); 20 and 40
  reported.

## 3. Members — one family, trials = 2

- **A. Large order:** every event. Control = the stock's industry index (as row 58).
- **B. Large order in a rising stock:** events where the stock is in Stage 2 at s0 -- weekly
  close above its 30-week average AND the average up more than 1% over 4 weeks (systrader's
  `internal/stage` Stage 2 condition), on the last completed week. Control = equal-weight
  return of the same industry's eligible stocks that are ALSO in Stage 2 at s0 (>= 3, else
  sector, else market) -- so B is not credited with plain momentum (Law 3).

## 4. Test

- Statistic: mean outcome (stock minus control) **after charging 50 bps** (a round trip).
- p: one-sided, cluster bootstrap over calendar months of entry (10,000 reps, seed 1).
- Benjamini-Hochberg over A and B.
- **In-sample PASS** = net mean >= **+2 pp** over 60 sessions, BH q < 0.10, and positive in at
  least 3 of the 5 years 2018 (H2) - 2022.
- Reported but not judged: 20/40 sessions, the median and share beating the control, the event
  count by year and sector, and the 5% and 25% size cut-offs (descriptive only; the rule is 10%).

## 5. Years

- **In-sample:** announcements 2018-07-01 to 2022-12-31 (the first year of results gives the
  trailing revenue).
- **Check (once, unchanged rule, only for a member that passes):** announcements 2023-01-01 to
  2025-12-31. PASS = net mean > 0 with p < 0.10 and positive in at least 2 of the 3 years. Order
  announcements in these years have not been read by any study (row 56 left 2023+ untagged).
  A pass leads to a forward paper track; a fail closes the idea for this definition.

## 6. What may change before the in-sample run, and what may not

May: fixing a bug in fetching or parsing, with the change logged here. May not: the 10% cut-off,
the 60-session horizon, the controls, the cost, the pass bars, the years -- any of those after
seeing a return sends this back to exploration as a new row.

## 7. Ledger

LEDGER row 84, trials = 2.

## Change log (before any return was computed)

- 2026-11-02, parsing: the prefilter also drops the exchange's own volume/price-movement query
  text (it always contains "in order to"), the phrase "in order to", and orders passed AGAINST
  the company (court, tax, assessment, tribunal, NCLT, SEBI, demand, penalty orders). Pilot on 3
  companies: 60 candidates -> 27, every order win kept. A company's reply to a price-movement
  query that cites an order is still a candidate.
