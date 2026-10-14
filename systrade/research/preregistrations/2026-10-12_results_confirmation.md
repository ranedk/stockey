# Pre-registration — results confirmation after a momentum entry

Written 2026-10-12, before any return measured FROM a result date has been computed. Follows
LEDGER row 56 (trigger study): among momentum entries, the first quarterly result after entry
had much better margin and profit change in the ones that kept running. That comparison put
the result INSIDE the outcome window, so it is partly the outcome itself. This test measures
returns only from the moment the result was public.

## 1. Rule (fixed)

- **Population:** EVERY momentum entry in `fundamentals_trigger_events` (row 56 definitions:
  first week in the top tenth by 26-week return vs the market after 8 weeks outside it; Rs 5 cr
  liquidity; 2 years listed; no +300% year; financials and diversified excluded) — all
  outcomes, including the "middle" ones. Nothing is conditioned on the outcome.
- **The result:** the first quarterly result whose NSE publish time (XBRL `broadCastDate`) is
  after the entry week and within 120 days of it, compared with the same quarter a year
  earlier on the same basis (consolidated where the company files it for most quarters).
  Entries with no such result are excluded and counted.
- **Confirmed** = EBITDA margin above the year-ago quarter's AND net profit above the year-ago
  quarter's. EBITDA = profit before tax + finance costs + depreciation − other income.
  Everything else is **not confirmed**.
- **Start:** the first weekly close after publication — that week's close if published Monday
  to Thursday, else the next week's.
- **Outcome:** stock return relative to its own industry's equal-weight index (row 53/56
  indices) over **13 weeks** (primary) and 26 weeks (secondary) from the start.

## 2. Years

Decisions (start dates) up to 2022-12-31: results data begin with the quarter to 2019-09-30, so
the first year-on-year comparison is the quarter to 2020-09-30. These are the years row 56
already looked at, so this run is IN-SAMPLE (it can kill the idea, it cannot confirm it).
Decisions from 2023-01-01 are untouched and are the check, run once with this rule unchanged.

## 3. Test

- Statistic: mean 13-week excess of confirmed minus not confirmed.
- p: one-sided, cluster bootstrap over calendar months of the start date (10,000 reps,
  seed 1) — entries in the same month share market conditions.
- **Pass (in-sample)** = difference ≥ +3 percentage points over 13 weeks AND p < 0.05.
- Reported, not deciding: medians, share beating the industry, 26-week numbers, by year,
  counts excluded, and the all-entries average.

## 4. Ledger

LEDGER row 57, trials = 1 (primary). A pass leads to the 2023+ check; a fail ends the line.

## 5. Addendum 2026-10-14 — the once-only check (written before any 2023+ entry, result or return was computed)

In-sample result (LEDGER row 57): PASS, +4.5 pp over 13 weeks, p 0.001. The check runs the
rule in sections 1 and 3 UNCHANGED, once:

- **Entries:** momentum entries with entry week 2023-01-01 to 2024-12-31, computed by the same
  code (`events.build_check`, stored as sample 'check'), prices from 2020-06 so the 104-week
  history and the 26-week momentum have data.
- **Results:** NSE XBRL quarters 2022-01 to 2024-12 (NSE's XBRL listing ends with the Dec 2024
  quarter; later results use the newer integrated filing, not read). Entries whose next result
  is a 2025 quarter therefore have none and are excluded under the existing "no result within
  120 days" rule — counted and reported, mostly late-2024 entries.
- **One code change, made and verified before the check:** the year-ago quarter must be on the
  same basis (consolidated/standalone) as the result. It changes nothing in-sample (re-run gave
  identical numbers: 610 entries, +4.5 pp, p 0.0012) but the check range can hold both bases.
- **Pass** = difference >= +3 pp over 13 weeks AND one-sided p < 0.05, as in section 3.
  Reported alongside: 26 weeks, by year, exclusions. No second run, no threshold change.
