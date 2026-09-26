# Strategy spec — trend speed blend (NSE cash equity)

**Status: FROZEN 2026-09-10.** The forward record starts 2026-09-10, on one
condition: the kill screen pre-registered in
`research/preregistrations/2026-09-10_speed_blend_screen.md` must not kill it.
If it does, this track never starts and the ledger says so.

This is trend-quintile (`docs/strategies/2026-09-08_trend_quintile.md`) with
its selection signal built the way Law 5 says it should have been: a blend of
speeds instead of one speed picked. It is a second, parallel forward record —
**not a replacement**. trend-quintile's record, clock and judgement are
untouched by anything here.

## 1. What is traded

Identical to trend-quintile in every respect but the signal:

- **Universe:** NSE cash equities whose 60-bar median traded value is at or
  above Rs 10 crore.
- **Signal:** `rules.SpeedBlend()` — EWMAC 16/64, 32/128 and 64/256, each at
  Carver's published forecast scalar and capped at ±20, combined **40% / 16% /
  44%**, multiplied by an **FDM of 1.10**, re-capped at ±20, and clipped to
  [0, +20] only after combining. Clipping each speed at zero first would rank
  a name with forecasts of +12, −15, −15 level with one at +12, 0, 0.
- **Selection:** the top quintile of the eligible universe by that forecast.
- **Weights:** equal across held names; fully invested, no leverage, no
  shorting, no cash timing, no stop (rows 20-22 apply unchanged).
- **Rebalance:** every 20 trading days; hold in between.
- **Execution:** decide at the close, fill at the next open; 50 bps round trip.
- **Controls:** an equal-weight book of the same universe and a random ranking
  of the same size, with the SAME seed as trend-quintile — so the two tracks
  share identical control books and differ only in what they select.

## 2. Why a blend

Law 5: don't select, blend. trend-quintile picked one speed, 32/128. LEDGER
row 25 found no speed clearly best — ewmac16 the weakest, ewmac32 and ewmac64
close — and Carver's own reference weights give the middle speed the least,
because it largely duplicates its neighbours. The story is EWMAC's, unchanged
(`rules.EWMAC.Story`); so is the known failure mode, momentum crashes.

## 3. Where every number came from — no performance input

| Input | Value | Source |
|---|---|---|
| Grouping | one group: three speeds of one rule | Carver ch. 8 and Table 44 |
| Correlations of the speeds' books in excess of equal-weight | 16-32 0.84, 16-64 0.61, 32-64 0.82 | `slice speeds`, 2013-07 → 2026-06 |
| Table 8 | row 11 → 42 / 16 / 42 | the same row that row 14's forecast correlations (0.87/0.59/0.87) and Carver's Table 57 give |
| Cost of each speed's book | 0.145 / 0.112 / 0.098 SR per year | `slice speeds` |
| Table 12, column A (costs are facts) | ×0.968 / ×1.013 / ×1.046 → 40.3 / 16.1 / 43.6 | rounded to whole percent: 40 / 16 / 44 |
| FDM | 1.104 → 1.10 | 1/√(w'Cw) over the pooled forecast correlations, cap 2.5 |

The report is `research/reports/2026-09-10_speed_blend_construction.txt`. Its
last lines are Carver's bootstrap as a cross-check; that is the only
computation in the construction that sees sample means, and it was run after
these numbers were fixed in code. It cannot change them.

A candid note on the FDM: this book ranks names, so a constant multiplier
changes nothing except through the ±20 cap. It is applied because Law 9 says
so and because it keeps the forecast on the scale the rest of the system
expects.

## 4. What this track is not

- Not chosen because it scored better: nothing in §3 looked at returns.
- Not a candidate to replace trend-quintile on a short record. Comparing the
  two forward records and keeping the winner early would be pick-the-winner
  with extra steps.

## 5. Pre-registered evaluation (binding)

- **trend-quintile's §5 terms apply verbatim:** track against both controls
  from day one; no judgement before 12 months AND 12 rebalances; success is a
  positive mean monthly difference against BOTH controls; kill is a negative
  mean monthly difference against equal-weight over 18 months, or realised
  round-trip costs above 100 bps; frozen for the whole window.
- **The two tracks are compared once**, at the first date both have 12 months
  and 12 rebalances (from 2027-09-10), as a paired monthly difference with a
  block-bootstrap interval (`internal/evidence`). That comparison decides
  nothing by itself: at most it motivates a new spec, with a new clock.

## 6. How it is tracked

- `cmd/paper run -strategy trend-speed-blend`, in `scripts/paper_daily.sh`
  beside trend-quintile, writing the same `systrader_paper_*` tables under its
  own name.
- An in-sample reference, `trend-speed-blend-reference`, from 2022-01-01 —
  a backtest, labelled as one wherever it appears.
- screener/'s Paper page lists it as its own strategy.
