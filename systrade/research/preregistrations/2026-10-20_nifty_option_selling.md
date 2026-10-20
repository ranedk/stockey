# Pre-registration — selling Nifty weekly options, defined risk

Written 2026-10-20, while the data for it is still downloading and before any option
price has been looked at for returns. Follows LEDGER row 61 (EXPLORATION): India VIX sat
above the volatility Nifty then delivered in 79% of 21-session windows (+2.5 pts on
average), but a naked monthly straddle only broke even because a few jumps took everything.
This asks whether a weekly, capped-loss structure collects the premium after real costs.

**Story (Law 1).** Option buyers pay for insurance and for lottery tickets: implied
volatility is set above expected realised volatility, and the gap is a payment to whoever
carries the jump risk. The seller is paid for bearing skew. Buying wings caps the loss so
that one 2020-style week cannot end the account; the test is whether the premium survives
paying for them.

## 1. Data

- Option prices: `dhan_option_rolling` (stockey, via `postgres_fdw`), Nifty, `expiry_flag =
  WEEK`, `expiry_code = 1` (nearest weekly expiry), 1-minute bars, strike offsets -10..+10
  around the money, CE and PE. Each bar names the strike it refers to.
- Expiry dates: `nseindia_fo_bhavcopy` (NIFTY OPTIDX, the actual expiry, holiday-moved).
- Settlement: Nifty 50's official close on the expiry date (`nseindia_indices`, with the junk
  weekend rows of TODO C11 excluded), which is the index options' final settlement price.
- Lot size: `nseindia_fo_bhavcopy.lot_size` where present (2024-07-08+), else 50.

## 2. Trades — three structures, one family (trials = 3)

Every week with a Nifty weekly expiry. Prices are the CLOSE of the last 1-minute bar at or
before the entry time, for each leg's strike; a week is skipped (and counted) if any leg has
no bar in the 30 minutes before entry. All legs are held to expiry and settled at intrinsic
value against the official close. ATM = the strike Dhan labels offset 0 at entry.

- **A. Iron fly, two sessions out.** Entry 15:15 IST on the second session before expiry.
  Sell the ATM call and put; buy the call 10 strikes above and the put 10 strikes below
  (Nifty strikes are 50 points apart, so wings at +-500 points).
- **B. Iron condor, two sessions out.** Same entry. Sell the call 4 strikes above and the
  put 4 strikes below ATM (+-200 points); buy +-10 strikes as wings.
- **C. Iron fly, expiry day.** Entry 09:30 IST on the expiry day itself; legs as A.

Size: one lot per week. Capital at risk per trade = the structure's maximum loss (wing
width minus net credit), in index points; the weekly return is P&L / capital at risk.

## 3. Costs (per leg, per transaction; settlement at expiry is not a transaction)

- Brokerage Rs 20 per order, in points = 20 / lot size.
- Exchange + SEBI + GST: 0.06% of premium. Stamp duty 0.003% of premium on buys.
- STT: 0.0625% of premium on sells until 2024-09-30, 0.1% from 2024-10-01; on a long leg
  that expires in the money, 0.125% of its intrinsic value.
- Slippage: max(0.25 points, 1% of premium) against us on every leg at entry.

## 4. Test

- Statistic: mean weekly return on capital at risk, after costs. Also reported: hit rate,
  worst week, maximum drawdown of the cumulative weekly return, annualised return.
- p: one-sided against zero, bootstrap over weeks (10,000 reps, seed 1). Benjamini-Hochberg
  across A, B, C.
- **In-sample PASS** for a structure: mean net weekly return > 0 with BH q < 0.10, positive in
  at least 2 of the 3 in-sample years, and maximum drawdown no deeper than 50% of a year's
  average capital at risk.

## 5. Years

- **In-sample:** expiries 2021-10-21 to 2024-10-31 — the old regime (several weekly index
  products, small lots, Thursday Nifty expiry).
- **Check (once, unchanged rules):** expiries 2024-11-01 to 2026-10-13 — the new regime (SEBI's
  November 2024 changes: one weekly index product per exchange, larger lots; Nifty expiry
  moved to Tuesday from September 2025). Stated now: a structure that passed in-sample and
  fails here fails, whatever the reason; the regime change is the most likely reason and a
  real one, because it is the market we would trade.
- A pass on both goes to a forward paper track before any capital, per the protocol.

## 6. Ledger

LEDGER row 64, trials = 3. A fail closes these three structures; a different structure is a
new family and a new row.
