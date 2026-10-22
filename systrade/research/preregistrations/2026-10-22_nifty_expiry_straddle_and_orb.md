# Pre-registration — stopped short straddles on expiry days only, and the 30-minute opening-range breakout

Written 2026-10-22, before any 2024-11..2026-09 result of either rule has been computed. LEDGER
row 78, trials = 2, one family. Follows rows 76-77 (TODO G-goal step 3, operator's "when is an
algo working" question).

## Where the rules come from (all in-sample, all already seen)

- Row 77's condition map of row 76's 36-member library (2021-10..2024-10) found one condition that
  switches members on and off: the WEEKLY EXPIRY SESSION. Stopped short ATM straddles earned
  there and lost on other days. The six stopped versions (09:20 / 10:00 / 11:00 entry x stop at
  +25% / +50% per leg) pooled equally: **+2.86 bps of notional per expiry day (n 159, t 3.3),
  −0.98 bps on other days**. By year: 2021 (Q4) +8.8, 2022 +5.8, 2023 +0.1, 2024 +1.1 — the
  effect FADED over the in-sample years; stated so a fail in the check is not a surprise.
- The 30-minute opening-range breakout (I-orb30) was the only member clearly positive over all
  days: +2.33 bps per session (t 1.25), by year +3.1 / +2.6 / −0.7 / +5.4; no condition sorted it.
- Picking the basket rather than the best single version (S-0920-50, +4.5 bps) is deliberate:
  the version was chosen by the data, the basket is not.

Because both came from searching 313 member x condition cells and 36 members, **the in-sample
figures are not evidence**. The only test is the unseen period below.

## Story (Law 1)

- E1: an option loses its last time value fastest on its expiry day; a seller who is short only
  that day collects it, and the per-leg stop caps the trending expiry day that otherwise takes
  it all back. On other days the decay is slower than the moves it is exposed to.
- E2: when Nifty leaves its first-half-hour range, order flow that waited for direction keeps it
  going for the rest of the day; the stop at the other side of the range cuts the false breaks.

## 1. Rules (exactly row 76's library definitions, `research/scripts/2026-10-22_nifty_algo_library.py`)

- **E1. Expiry-day stopped straddle basket.** On each WEEK1 expiry session (the session the
  contract actually expires, holiday moves as row 73), run the six members S-0920-25, S-0920-50,
  S-1000-25, S-1000-50, S-1100-25, S-1100-50 (short the ATM WEEK1 call and put at the entry time,
  each leg stopped on its bar high at +25% / +50% of its entry price, the rest bought at 15:15)
  and take their equal-weight average. No trade on other sessions. Unit: one expiry session's
  basket net return per unit of notional.
- **E2. I-orb30.** Every session: range = Dhan's minute spot from 09:15 to 09:45; enter on the
  first minute close beyond the range in its direction, stop on a minute close beyond the other
  side, else out at 15:15; 3 bps of notional per round trip. A day with no breakout is 0. Unit:
  one session's net return per unit of notional.

Costs as the library: options per leg per transaction Rs 20/order, STT by period (0.1% of the
sell side from 2024-10-01), exchange 0.03503% from 2024-10-01, SEBI, 18% GST, stamp on buys,
slippage 0.5 point. Lot sizes from the F&O bhavcopy. Missing-leg rules as rows 73-74.

Things that changed in the check period and are taken as they come, not adjusted for:
- Nifty's weekly expiry moved from Thursday to Tuesday (2025-09) — the calendar comes from the
  bhavcopy, as always.
- Lot sizes changed (25 -> 75 -> 65, read from the bhavcopy); STT on options went up 2024-10-01.
- SEBI's late-2024 F&O measures (one weekly expiry per exchange, no calendar-spread margin
  benefit on expiry day, an extra 2% ELM on short options on expiry day) — the extra margin does
  not change P&L per unit of notional.

## 2. Test

- p: one-sided bootstrap of the unit returns (10,000 reps, seed 1; for E2, blocks of 20
  sessions); Benjamini-Hochberg across E1 and E2.
- **PASS** for a rule needs all three:
  1. mean > 0 with BH q < 0.10;
  2. positive in both halves of the check: 2024-11-01..2025-09-30 and 2025-10-01..2026-09-30;
  3. max drawdown within 35% on the compounded account scaled to 10% annualised vol (scale
     fixed from the IN-SAMPLE vol of the unit: E1 over expiry days, E2 over all sessions).
- Reported, not gated: E1 by entry time and stop (the six members) and on non-expiry days (the
  in-sample contrast); E2 by weekday and expiry vs not; hit rates; missing legs; slippage at
  0.25 / 1 pt for E1.

## 3. Years

- **Check, once, rules unchanged:** sessions 2024-11-01 to 2026-09-30. Burn name
  `nifty-expiry-straddle-orb30-row78-check-2024-11-to-2026-09`.
- Before the burn, a mechanics pass may count sessions, expiry days, missing bars and skips (no
  P&L). A data gap found there is fixed or stated before the burn, never after.
- Contamination: row 75 (Dhan marketplace, 2024-11..2026-10) looked at other people's
  algos in this period, not these rules; row 72's vendor exit rules were learned there but are
  not used here. Treated as clean.
- A pass goes to a forward paper track; a fail closes both and the row 76 library line of work.

## 4. Ledger

LEDGER row 78, trials = 2. Other expiry-day structures, entry times, stop levels or range windows
are a new family and a new row.
