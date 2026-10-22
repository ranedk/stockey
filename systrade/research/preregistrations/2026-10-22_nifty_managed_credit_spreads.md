# Pre-registration — Nifty credit spreads with Dhan-style exits, and plain intraday selling

Written 2026-10-22, before any profit or loss of these trades has been computed. LEDGER row 74,
trials = 3, one family. Follows rows 72 and 73.

## Where the rules come from (stated so the test is read correctly)

The exits are read off the vendor's own trades (row 72's archive, 2025-05..2026-10, 4,859
credit-spread exits; `research/scripts/2026-10-22_dhan_credit_spread_exits.py`):

- shape: sell one option about at the money, buy one 400 points further out (300 in 12%);
  credit about 26% of the width; entries mostly 10:00-11:00;
- "Target 1" closes HALF the position; in the Expiry / Exit-Early families it fires at about
  50% of the credit kept (median 0.49-0.51);
- losing exits during the day cluster at a loss of 25-70% of the credit, median ~30-50% --
  a stop, probably set on Nifty's level, not on the premium;
- the rest closes ~15:xx the same day or the next; their direction is a coin toss (row 72).

**Consequence for the years.** These rules were learned on 2025-26 trades. 2021-10..2024-10 has
never been looked at with them, so it is a genuine out-of-sample test of the vendor's method.
The 2024-11..2026-09 period is where the rules came from: **no check period is run there**; a
pass goes straight to a forward paper track, the only further evidence that counts.

What was already seen in 2021-10..2024-10 with OTHER rules: row 73 (stopped straddle ~0 net; one-day
iron fly −1.9%/trade, the daytime part +9.2 pts gross but survivorship-prone), row 70, row 64 C.

## Story (Law 1)

The vendor's book makes money with no forecasting skill, so either 2025-26 was simply a good
year for selling, or the exits do work: taking half the premium early banks the decay before a
move can take it back, and the stop cuts the trending day while the wing caps the gap. Selling
both sides, each managed on its own, keeps the exits and removes the (no-skill) direction.

## 1. Data

As row 73: Dhan rolling-option 1-minute bars, WEEK1, offsets ±10 (a bar stamped t is the
price at t+1 min) for entry and every check; the bhavcopy close only where stated. Lots,
holiday expiries and WEEK1's contract as row 73's script.

## 2. Trades (one lot of each spread; every session in the window)

Common to all three: at 10:00 IST, K = the ATM strike (offset 0). Two spreads:

- call spread: sell the K call, buy the K+400 call;
- put spread: sell the K put, buy the K−400 put.

Each spread's credit c = short − long at entry; its value v(t) = short − long on each later
minute's closes. Capital at risk for the pair = 400 − (c_call + c_put), as row 73 B.

- **D1. Intraday, managed.** Each spread on its own: close it at the first minute where
  v ≤ 0.5 c (half the credit kept) or v ≥ 1.5 c (loss of half the credit); otherwise close at
  15:15. Every session, expiry days included.
- **D2. Intraday, unmanaged** (row 73's "daytime part", measured cleanly): both spreads
  10:00 → 15:15, no other exit. Every session, expiry days included.
- **D3. Overnight, managed.** As D1, but the position stays open past the close; checks resume
  at the next session's first minute (an overnight gap fills at that minute's closes), and
  whatever is still open closes at 15:15 next session. No entry on an expiry day.

Mechanics, stated now:
- Whole spread closes at a trigger (the vendor closes half at Target 1; one exit keeps one rule).
- Triggers read minute CLOSES of both legs together (the two highs do not happen together);
  the fill is that minute's closes plus slippage.
- A leg with no bar for a minute carries its last close forward. A LONG leg missing at its
  exit is valued at 0 (conservative: our wing worth nothing). Missing legs are counted and
  reported. For D3, if any leg is missing at the final 15:15, all four legs close at that
  session's bhavcopy close instead.

## 3. Costs

Row 73's, per leg per transaction: brokerage Rs 20/order, STT by period on the sell side,
exchange/SEBI/GST, stamp on buys, **slippage 0.5 point**. Sensitivity at 0.25 pt, 1 pt and the
old max(0.25 pt, 1% of premium) reported, not gating.

## 4. Test

- Unit: one session's pair. Return = net points / capital at risk.
- p: one-sided bootstrap over sessions (10,000 reps, seed 1); Benjamini-Hochberg across D1-D3.
- **PASS** needs all three:
  1. mean net return > 0 with BH q < 0.10;
  2. positive in at least 2 of the 3 full years 2022, 2023, 2024;
  3. account max drawdown (not compounded) within 35% at 2% of the account at risk per trade.
- Reported, not gated: hit rate, worst day, each exit's share (take-profit / stop / time), by
  weekday and expiry day vs not, and every slippage level.

## 5. Years

Entries 2021-10-21 to 2024-10-31 only. No check period (see above). A pass goes to a forward
paper track under the protocol; a fail closes these three versions.

## 6. Ledger

LEDGER row 74, trials = 3. Different trigger levels, wing widths, entry times or holds are a new
family and a new row.
