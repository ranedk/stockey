# Pre-registration — short-hold Nifty option selling: the intraday stopped straddle, and the one-day iron fly

Written 2026-10-22, before any profit or loss of these two trades has been computed. LEDGER
row 73, trials = 2, one family. Follows rows 64-70 and row 72 (TODO G-goal step 3).

## What has already been seen (stated so it is not mistaken for evidence)

- Row 64 C: an iron fly sold 09:30 on expiry day and held to the close: −0.05% a week on
  risk, in-sample 2021-10..2024-10. Close to zero, failed.
- Row 70 (in-sample 2021-10..2024-10): an ATM WEEK1 straddle sold at 15:15 and bought back at
  the next 15:15 earned +2.2% of premium a day gross, after the survivorship correction. Its
  overnight vs daytime split (+1.2% / +2.8%) was computed BEFORE that correction and is biased.
- Row 72 (Dhan's marketplace, 61 Nifty strategies of one vendor, live 2024-12..2026-10):
  - plain intraday/overnight straddles and strangles earned about 0 after costs (median −5%
    a year on margin; 5 of 19 positive);
  - one-day single-side credit spreads, one near the money short and a wing ~400 points out,
    earned a median +66% a year on margin after costs (17 of 26 positive);
  - their direction calls were a coin toss: Nifty moved their way 46% of the time, with no
    link to the morning, the gap or the last days.
- **The check period below (2024-11..2026-09) is therefore softly contaminated:** row 72
  showed one-day ATM credit spreads made money then, though not with these exact rules. The
  check stays a check of sign and survival, and the forward paper track remains the real gate.

## Story (Law 1)

Option buyers pay for insurance against moves; sellers are paid for carrying them (row 61).
Held to expiry the payment is gone: rows 64-68 find the tails and the wings take it back. Over
one day, the seller holds only one night's and one day's risk, and two things may differ:

- **A, the intraday stopped straddle** (the most common Indian retail/algo setup, and the
  operator's contacts' "exit on stop"): the per-leg stop is meant to cut the trending day,
  while quiet days pay the morning's premium.
- **B, the one-day iron fly**: row 72's winners without their (no-skill) direction. Selling
  both sides is a single-side credit spread with the direction taken out, so if their money
  came from the overnight premium and not from forecasting, B earns it too. The wings keep
  the margin and the worst day small.

## 1. Data

- Entry prices, and every price during the day for A: Dhan rolling-option 1-minute bars,
  WEEK1 (`dhan_option_rolling`), strike offsets ±10 (±500 points). A bar stamped t is the
  price at t+1 min (`r64.chain_at`).
- B's exit next session: the NSE F&O bhavcopy close of the same contract
  (`nseindia_fo_bhavcopy`, every listed strike, so no survivorship: the
  `dhan-rolling-option-survivorship` lesson). A contract expiring that session settles at
  intrinsic value against Nifty 50's official close.
- Expiries and holiday labels as in row 68: the contract on a day is the latest expiry within
  4 days of the label.
- Lot size (it only sets brokerage in points): the bhavcopy's where present (2024-07-08+),
  else 50, as in row 68.

## 2. Trades (one lot; every session in the window)

**A. Intraday short straddle, 25% stop per leg.**
- At 09:20 IST sell the WEEK1 ATM call and put (offset 0's strike). Every session, expiry
  days included.
- Each leg has its own stop at 1.25 x its entry price. It fires on the first minute bar whose
  HIGH reaches the stop; the fill is max(stop, that bar's open). The other leg stays on.
- At 15:15 buy back whatever is still open at that minute's close.
- If a leg's bars stop before 15:15 (its strike left Dhan's ±10 window), that leg is valued
  at its last bar's close. Counted and reported. (A stop at +25% fires long before an ATM leg
  moves 500 points into the money; only the far, cheap leg can leave the window.)

**B. One-day iron fly, 400-point wings.**
- At 10:00 IST sell the WEEK1 ATM call and put, buy the call 400 points above and the put
  400 points below (offsets ±8). No entry on an expiry day (the contract dies that session).
- Close all four legs at the next session's bhavcopy close; if the next session is the
  expiry, settle at intrinsic value.
- Capital at risk = 400 − net credit, points.

## 3. Costs (per lot, per leg, per transaction; expiry settlement is not a transaction)

- Brokerage Rs 20 per order, in points = 20 / lot size.
- STT on the sell side: 0.0625% of premium to 2024-09-30, 0.1% from 2024-10-01; a long leg
  settling in the money pays 0.125% of intrinsic.
- NSE transaction 0.03503% of premium (0.05% before 2024-10-01), SEBI 0.0001%, 18% GST on
  brokerage + exchange + SEBI, stamp 0.003% on buys.
- **Slippage: 0.5 point per leg per transaction.** A change from rows 64/68 (max(0.25 pt, 1%
  of premium)), decided now, before any result, and stated as such. Reason: a weekly ATM Nifty
  option's quoted spread is 0.05-0.2 points; 1% of a 150-point premium (1.5 points) is several
  times the spread and on a one-day trade it alone is ~4% of the premium a day. Reported
  beside the verdict, not gating: slippage 0.25 pt, 1 pt, and the old 1% rule.

## 4. Test

- Unit: one trade per session. A's return = net points / entry premium (and points);
  B's return = net points / capital at risk.
- p: one-sided against zero, bootstrap over sessions (10,000 reps, seed 1); Benjamini-Hochberg
  across A and B.
- **In-sample PASS** for a version needs all three:
  1. mean net return > 0 with BH q < 0.10;
  2. positive in at least 2 of the 3 full in-sample years (2022, 2023, 2024 to October);
  3. account max drawdown (not compounded) no deeper than 35%, sized at 2% of the account at
     risk per trade -- B: the capital at risk; A: 2.5 x entry premium (a double stop-out plus
     the next-largest day's gap, stated now as the unit of risk).
- Reported, not gated: hit rate, worst day, by weekday and sessions to expiry, the share of A's
  legs stopped, the share of B's P&L earned overnight (B's 10:00 -> 15:15 same day vs 15:15 ->
  next close, from the 1-minute data where both strikes are still in range, flagged as
  survivorship-prone), and every slippage level of section 3.

## 5. Years

- **In-sample:** entries 2021-10-21 to 2024-10-31 (~740 sessions).
- **Check (once, unchanged rules), only for versions that pass in-sample:** entries
  2024-11-01 to 2026-09-30. Pass = mean net return > 0 there and drawdown within 35% at the
  same sizing. Softly contaminated (see above); the regime differs (one weekly product from
  November 2024, larger lots, Tuesday expiry from September 2025). Burn name:
  `nifty-short-hold-selling-row73-check-2024-11-to-2026-09`.
- A pass on both goes to a forward paper track, as the protocol requires.

## 6. Ledger

LEDGER row 73, trials = 2. A fail closes both versions as written. Other stop levels, entry
times, wing widths or exit times are a new family and a new row. Bank Nifty is not in this
row (its options are not downloaded; its weekly contract ended November 2024).
