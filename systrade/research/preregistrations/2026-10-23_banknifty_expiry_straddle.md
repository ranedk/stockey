# Pre-registration — E1 (the expiry-day stopped straddle basket) on Bank Nifty

Written 2026-10-23, before any Bank Nifty option P&L has been computed in this workspace (the
Bank Nifty minute bars were first downloaded today). LEDGER row 80, trials = 1. Follows row 78
(E1 passed its Nifty check) and the operator's note that the people they know trade "mostly
NIFTY and a few BANK NIFTY".

## Why

E1's story is not about Nifty: an option loses its last time value fastest on its expiry day,
and the per-leg stop caps the trending day. If that is the reason, it should hold on the other
big index option too. If E1 works only on Nifty, it is more likely a fluke of one market.

## 1. Rules — E1 unchanged

- On each Bank Nifty expiry session, the six members S-0920-25, S-0920-50, S-1000-25, S-1000-50,
  S-1100-25, S-1100-50, exactly `short_straddle()` in
  `research/scripts/2026-10-22_nifty_algo_library.py`: short the ATM call and put (Dhan's
  strike offset 0) at the entry time, each leg stopped on its bar high at +25% / +50% of its
  entry price (filled at the stop or the bar's open if worse), the rest bought at 15:15; the
  equal-weight average of the six. Unit: one expiry session's net return per unit of notional.
- **Expiry sessions:** Bank Nifty option contract labels from the NSE F&O bhavcopy that actually
  traded on the session they expired (holiday moves to the session before, as row 73). This
  drops the phantom labels NSE leaves behind when it moves the expiry weekday (row 78's erratum).
- **Which bars:** the series whose nearest contract expires that session — Dhan WEEK1 while Bank
  Nifty had weekly options (expiries through 2024-11-13), MONTH1 after (monthly only since
  SEBI's one-weekly-per-exchange rule). If the mechanics pass shows a series does not match the
  expiring contract, that is fixed or stated before the burn.
- **Costs:** as E1 (Rs 20/order, STT by period, exchange, SEBI, 18% GST, stamp), with Bank
  Nifty's lot size: the bhavcopy's from 2024-07, else 25 for expiries through 2023-06-30 and 15
  after. **Slippage 1 point per leg per transaction** (E1 used 0.5 on Nifty; Bank Nifty
  premiums are about three times larger in points and its spreads wider). Missing-leg rules as
  rows 73-74.

## 2. Test

- p: one-sided bootstrap of the per-expiry returns (10,000 reps, seed 1). One trial, no BH.
- **PASS** needs all four:
  1. mean > 0 with p < 0.10;
  2. positive in both halves by count (first half of the expiries in date order, second half);
  3. max drawdown within 35% on the compounded account scaled to 10% annualised vol (scale from
     the whole period's vol of the unit — there is no separate in-sample here);
  4. mean > 0 over the monthly-only era (2024-11-14..2026-09-30, about 22 expiries). This is the
     only version that can be traded today, so a pass without it is not worth a paper track.
- Reported, not gated: the six members; weekly vs monthly expiries; by year; non-expiry days
  (the Nifty contrast was about −1 bp); slippage at 0.5 and 2 points; correlation with Nifty E1
  on the same expiry sessions.

## 3. Years

- **One run:** Bank Nifty expiry sessions 2021-10-21..2026-09-30 (the start of the Dhan minute
  history this test uses). Burn name `banknifty-expiry-straddle-row80-2021-10-to-2026-09`.
- Before the burn, a mechanics pass may count sessions, expiry days, missing bars, which series
  carries the expiring contract, and skips (no P&L). A data gap found there is fixed or stated
  before the burn, never after.
- Contamination: no Bank Nifty option bars were read before today. Row 75's Dhan marketplace
  review looked at vendors' algos, some on Bank Nifty, not this rule. Bank Nifty's index level
  has been in daily data for years but no option rule was ever tested on it. Treated as clean.
- A pass adds a Bank Nifty line to E1's forward paper track (monthly expiries, ~12 a year); a
  fail is recorded and E1 continues on Nifty alone.

## 4. Ledger

LEDGER row 80, trials = 1. Other entry times, stops or structures on Bank Nifty are a new row.
