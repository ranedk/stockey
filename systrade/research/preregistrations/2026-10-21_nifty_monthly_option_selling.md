# Pre-registration — selling Nifty MONTHLY options at the roll, defined risk, managed exits

Written 2026-10-21, before any profit or loss of these structures has been computed.
Follows LEDGER rows 64-67 (TODO G-goal step 2):

- row 64: weekly defined-risk selling held to expiry FAILED;
- row 65: weekly straddles are priced about at what Nifty delivers;
- row 66: no "rich vs recent movement" sort;
- row 67 (exploration): an unhedged MONTHLY ATM straddle sold the session after the
  previous monthly expiry paid out 0.89 of its premium (66% of months won, gross), while the
  same contract sold 10 or 5 sessions before expiry paid out 1.09 / 1.28. The premium looks
  front-loaded.

**What has already been seen.** Row 67 looked at 2021-10..2024-10 for unhedged straddles,
held to expiry, at three entry points. Nothing with wings, exits, stops or 2016-2021 has been
computed. The once-only check period below has not been read by anything.

**Story (Law 1).** Option buyers pay for insurance over a month; the seller is paid for
carrying jump risk, and most of the time-value premium of a month's option is earned while
the expected move is still uncertain. Close to expiry the option is a cheap bet on one or two
days' moves that the market prices about fairly (row 65), so the seller should take the
early part and hand the last part back. Wings cap the loss so that a March 2020 cannot end
the account.

## 1. Data

- Option prices: `nseindia_fo_bhavcopy` (NSE F&O bhavcopy, every listed strike), NIFTY OPTIDX.
  Price = NSE's daily CLOSE for that contract (the last-half-hour weighted price when it
  traded). Dhan's rolling-option data is NOT used: it spans only +-10 strikes (+-500 points)
  around the money, too narrow for monthly wings and for marking a position after a move.
- Monthly expiry = the last NIFTY option expiry of its calendar month (holiday-moved, as the
  bhavcopy records it).
- Index close and settlement: Nifty 50's official close (`nseindia_indices`, the junk weekend
  rows of TODO C11 excluded).
- Lot size: the bhavcopy's where present (2024-07-08+); before that 75 for expiries to
  2021-06-30, then 50.

## 2. Trades — one family, four versions (trials = 4)

Every month, one position, all four versions on the same entry:

- **Entry:** at the close of the first session after the previous monthly expiry, into the
  next monthly expiry. ATM = the listed multiple-of-100 strike nearest the Nifty close.
- **Structure (all versions):** an iron fly.
  - Sell the ATM call and put. Premium P = their two closes.
  - Buy the call at ATM + W and the put at ATM - W, where W = 2 x P rounded to the nearest
    100 points: wings about two expected moves out.
  - Each leg must have traded on the entry day (contracts > 0). A wing that did not trade
    moves INWARD to the nearest multiple-of-100 strike that did. The month is skipped (and
    counted) if any leg cannot be found.
- **Skip rule (all versions):** no entry in a month whose holding window contains a
  general-election results day (row 65: the one event far outside what options price).

The four versions differ only in the exit:

- **M1. Hold to expiry.** All legs settle at intrinsic value against the official close. This
  is the row-64-style baseline, kept as a version in its own right.
- **M2. Exit early.** Close all four legs at the close of the 10th session before expiry
  (row 67's boundary).
- **M3. Exit early + take profit.** As M2, but close everything at the first daily close where
  the position's value is at most 50% of the entry credit.
- **M4. Exit early + stop.** As M2, but close everything at the first daily close where the
  loss on the position reaches 1x the entry credit.

Marks for M3 and M4 use each leg's daily close, checked from the session after entry
through the exit day. If a leg has no bhavcopy row on some day, that day is not checked.

## 3. Costs (per leg, per transaction; settlement at expiry is not a transaction)

- Brokerage Rs 20 per order, in points = 20 / lot size.
- Exchange + SEBI + GST: 0.06% of premium. Stamp duty 0.003% of premium on buys.
- STT: 0.0625% of premium on sells until 2024-09-30, 0.1% from 2024-10-01 (entry: short legs;
  early exit: selling back the wings). A long leg that expires in the money pays 0.125% of its
  intrinsic value.
- Slippage: max(0.25 points, 1% of premium) against us on every leg, on every transaction
  (entry and any early exit).

## 4. Test

- Unit: one trade per month. Return = net P&L / capital at risk, where capital at risk is
  the iron fly's maximum loss (W - net credit).
- p: one-sided against zero, bootstrap over months (10,000 reps, seed 1). Benjamini-Hochberg
  across M1-M4.
- **In-sample PASS** for a version needs all three of:
  1. mean net monthly return > 0 with BH q < 0.10;
  2. positive in at least 6 of the 9 in-sample calendar years (2016-2024);
  3. account max drawdown (non-compounded) no deeper than 35%, sized so that each month puts
     10% of the account at risk.
- Reported for every version, not gated: hit rate, worst month, the M2-vs-M1 paired
  difference (does exiting early help?), and per-year results.

## 5. Years

- **In-sample:** monthly expiries 2016-02 to 2024-10 (about 104 months).
- **Check (once, unchanged rules):** monthly expiries 2024-11 to 2026-09. That is about 23
  months, so it is a check of sign and survival, not a precise estimate. A version passes it
  if:
  - its mean net return there is > 0;
  - its account drawdown there stays within 35% at the same sizing.

  Only versions that passed in-sample are run. Regime note, stated now: lots got larger and
  the Nifty expiry moved to Tuesday (September 2025). A pass on in-sample that fails here
  fails.
- A pass on both goes to a forward paper track, per the protocol.

## 6. Ledger

LEDGER row 68, trials = 4. A fail closes these four versions. Different wings, exit day, or
take-profit/stop levels are a new family and a new row.
