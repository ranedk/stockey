# Strategy spec — Stage 2 leaders with the market floor, and its matched control (paired)

**Status: FROZEN 2026-10-21.** Both forward records start 2026-10-22. LEDGER row 71
(follows row 69). Operator request (TODO G-goal step 4).

## 1. What is traded

Two tracks, each identical to stage2-rs-leaders (`docs/strategies/2026-10-01_stage2_rs_leaders.md`)
in universe, Stage 2 filter, 6-month ranking, 20 names, max 4 per industry, 20-session clock,
2x buffer, 30-week-average exit, costs and random seed. They differ only in how much is owned:

- **stage2-rs-leaders-floor:** row 20's regime floor -- fully out whenever the universe's
  equal-weight index closes below its 200-day average, back in when it closes above; checked
  every day (`Overlay = regime`, `ExposureDaily = true`).
- **stage2-rs-leaders-const67:** always 67% invested (`Overlay = constant`, 0.67) -- the
  floor's own average exposure over row 69's history. The question is whether the floor
  picks WHEN to hold cash better than holding the same cash all the time.

Two code changes made for this pair, both off for every earlier spec (row 69 reproduces exactly):

- `Spec.MarketWarmup`: the floor's market index is first walked through ~280 sessions before
  the start, so the floor can act from day one instead of sitting fully invested for its
  first 200 sessions.
- The order sheet is scaled to the overlay's next-day exposure (`Track.NextExposure`,
  `paper.PendingAt`); before, it always showed a fully invested book. On a hold day where the
  floor turns to cash, the sheet lists the whole book as exits.

## 2. Why

Row 69 (2014-02..2026-10, IN-SAMPLE, already seen): the floor lost to its matched constant
on four books, and won on this one only: 14.19%/yr, maxDD -37.5% vs 13.42%, -43.8%. One win
in five, on numbers already looked at, is a lead, not evidence. Only these records decide.

## 3. Pre-registered evaluation (binding)

- No judgement before 12 months AND 12 rebalances (first possible: 2027-10-22).
- **The floor is adopted for this book only if** it is ahead of const67 after costs AND its
  worst drawdown is no deeper. Otherwise the plain book (or a constant fraction) stands.
- Reported beside it: the paired monthly difference with its bootstrap p, both books against
  the unprotected stage2-rs-leaders over the same months, and the number of floor switches.
- Caveat stated now: a year may contain no market fall below the 200-day average, in which
  case the floor is fully invested throughout and the pair only measures the cost of the
  constant's idle 33%. That outcome is reported as "untested", and the window extends to the
  first 12 months containing at least one floor exit.
- Frozen for the whole window: any change is a new spec with its own clock.

## 4. How it is tracked

`cmd/paper run -strategy stage2-rs-leaders-floor` and `-strategy stage2-rs-leaders-const67` in
`scripts/paper_daily.sh`; `cmd/dhan orders -strategy all` builds their sheets as a dry run;
screener/'s Paper page lists every registered spec.
