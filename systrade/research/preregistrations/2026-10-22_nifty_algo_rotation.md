# Pre-registration — rotating among a library of Nifty algos: run what is working, drop it when it breaks

Written 2026-10-22, before any rotation result has been computed. LEDGER row 76, trials = 3,
one family. TODO G-goal; operator's theory, 2026-10-22: algos work for some time, then stop;
traders keep finding the ones working now.

## Already seen (so it is not mistaken for evidence)

- Rows 64-74: nearly every single Nifty option/futures rule tested here earns about zero before
  costs over 2021-10..2024-10, and loses after them. Several library members below ARE those
  rules (their in-sample daily P&L has been looked at in aggregate, not month by month and not
  for rotation).
- Row 75 (Dhan marketplace, 2024-11..2026-10): a strategy's last 2-3 months predicted its next
  month (selling, live: top third − bottom third +6.7% of margin), but it looked like a fixed
  ranking, not a cycle. That covers this row's check period: **the check is softly
  contaminated** (we know persistence existed among some Nifty algos then, not ours).

## Story (Law 1)

Short-term index algos earn their money in regimes: sellers in quiet, range-bound weeks, buyers
and breakouts in trending ones, trend rules in long moves. If regimes last longer than it takes
to see them (2-3 months), running the algos that worked recently, and dropping one when its
results leave their usual band, should beat holding all of them, and beat picking at random.
If regimes do not last, or the library has nothing to find, it will not.

## 1. The library (36 members, fixed now; Nifty only)

Every member is a daily net P&L series, after row 73's costs (options: Rs 20/order, STT by
period, exchange/SEBI/GST, stamp, slippage 0.5 pt per leg per transaction; index trades:
3 bps of notional per round trip, all-in). Option prices from Dhan's rolling WEEK1 1-minute
bars, overnight exits from the bhavcopy, as rows 73-74 (same missing-leg rules).

- **S, short ATM straddle, intraday**, out at 15:15: entry 09:20 / 10:00 / 11:00 x per-leg stop
  at +25% / +50% / none. (9) [S-0920-25 is row 73 A.]
- **F, iron fly, intraday**, out at 15:15: entry 09:20 / 10:00 / 11:00 x wings 200 / 400 x
  managed (each side alone: take profit at value <= 0.5 x credit, stop at >= 1.5 x credit) /
  unmanaged. (12) [F-1000-400-managed is row 74 D1; unmanaged is D2.]
- **O, overnight**: iron fly 400 sold 10:00, out at the next session's bhavcopy close (row 73 B);
  the same sold 15:15; row 74 D3 (managed, out next 15:15). No entry on an expiry day. (3)
- **L, long ATM straddle, intraday** (the buyer's side), out at 15:15: entry 09:20 / 10:00 x
  no exit / take profit at +50% of the pair's premium with a stop at −30%. (4)
- **T, Nifty futures trend, daily**: Carver EWMAC 8/32, 16/64, 32/128, 64/256 on Nifty 50, long
  and short, futures carry as row 62. (4)
- **I, index intraday** (Nifty minute spot from the option data, traded as futures):
  opening-range breakout on the first 15 / 30 minutes (enter on the first close beyond the
  range, stop at the other side, out 15:15); first-half-hour momentum (sign of 09:15 → 09:45,
  held 15:00 → 15:30, row 63's); gap fade (an open more than 0.3% from the previous close is
  faded at 09:20, out 15:15). (4)

**Common scale.** Each member is scaled to 10% annualised volatility by its own trailing
60-session volatility, lagged one day, capped at 3x (a member with < 60 sessions of history is
not eligible). A day a member does not trade is 0.

## 2. Rotation policies (the trials)

Decisions use data up to the previous close; positions follow from the next session.

- **R1 (the operator's cycle), 3-month window.**
  1. Formation: each member's Sharpe ratio over the last 63 sessions.
  2. Hold the top 3 with a positive formation Sharpe, equal weight (fewer if fewer qualify;
     the empty slots are cash).
  3. Keep each held member until it BREAKS: its cumulative return since selection falls below
     −2 x (its formation daily sd x sqrt(sessions since selection)).
  4. A broken member is replaced, next session, by the best non-held member on the then-current
     63-session Sharpe (positive only). No other switching.
- **R2, as R1 with a 2-month window** (42 sessions).
- **R3, monthly re-ranking, no break rule**: on the first session of each month hold the top 3
  by 63-session Sharpe (positive only). Tells apart "the break rule helps" from plain selection.

Switching cost: only T members carry positions across days; entering or leaving one charges
3 bps of its notional. Everything else is flat each night or closes on its own clock.

## 3. Controls (not trials)

- **EW**: all eligible members, equal weight, every day.
- **Random**: 1,000 draws (seed 1) of the same policy with the selection replaced by random
  picks among eligible members, on the same switching days and slot counts. A policy's
  "random p" = the share of draws with a higher Sharpe.

## 4. Test

- Unit: the policy's daily account return. Sharpe and mean from the first session with 63
  sessions of history (about 2022-01-20) to 2024-10-31.
- p vs zero: one-sided bootstrap of daily returns in blocks of 20 sessions (10,000 reps, seed
  1); Benjamini-Hochberg across R1-R3.
- **In-sample PASS** for a policy needs all four:
  1. mean > 0 with BH q < 0.10;
  2. random p < 0.10 (it beats picking at random);
  3. higher Sharpe than EW;
  4. positive in at least 2 of the 3 calendar years 2022, 2023, 2024 (to October); max drawdown
     within 35% at this 10%-vol scale.
- Reported, not gated: each member's own result; how long held members last before breaking;
  which families get picked when; R1 vs R3 paired.

## 5. Years

- **In-sample:** sessions 2021-10-21 to 2024-10-31 (formation from the start; trading from ~2022-01).
- **Check (once, unchanged rules), only for policies that pass:** 2024-11-01 to 2026-09-30, the
  library rebuilt on that period with the same definitions. Pass = mean > 0 and beats EW there.
  Softly contaminated (row 75). Burn name: `nifty-algo-rotation-row76-check-2024-11-to-2026-09`.
- A pass on both goes to a forward paper track.

## 6. Ledger

LEDGER row 76, trials = 3. The 36 library members are inputs, not trials: no member is judged
on its own here, and none may be added or dropped after the first result. Other windows,
slot counts or break levels are a new family and a new row.
