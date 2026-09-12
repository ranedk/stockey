# Strategy spec — momentum lookback blend (NSE cash equity)

**Status: FROZEN 2026-09-12.** The forward record starts with the first
trading day on or after 2026-09-12, on one condition: the kill screen
pre-registered in `research/preregistrations/2026-09-12_lookback_blend_screen.md`
must not kill it. It is a third, parallel forward record beside
trend-quintile and trend-speed-blend, and replaces neither.

## 1. What is traded

- **Universe:** NSE cash equities with a 60-bar median traded value of at
  least Rs 10 crore (the same as the other two tracks).
- **Signals:** the literature's momentum — trailing total return on adjusted
  closes, not EWMAC — at four lookbacks: 6, 9 and 12 months, and 12 months
  skipping the latest one (a month is 21 trading days).
- **Book:** a blend of BOOKS (`paper.ModeBookBlend`): each lookback's own top
  quintile of the eligible universe, equal weight within it, held at
  **33 / 33 / 17 / 17**. A stock in several variants' top quintiles is held
  once at the summed weight. A variant with no book yet hands its share to
  the others.
- **Rebalance:** every 21 trading days (monthly); decide at the close, fill
  at the next open; 50 bps round trip; fully invested, no leverage, no stop.
- **Controls:** equal-weight universe and a random ranking, same seed as the
  other tracks. The random book is one quintile in size, while the blend
  holds the union of four overlapping quintiles — a more diversified book
  than its random control, which the comparison should be read with.

## 2. Why these four, at these weights — no performance input

LEDGER row 30 found momentum's edge positive in every window tried and
concentrated at 6-12 month lookbacks held 1-3 months. The members are the
lookbacks whose ONE-MONTH-hold cells survived there (6, 9, 12, 12-minus-1);
12-to-7 and 3 months did not. Their weights come from the handcrafting tree
on how their books co-move and what they cost to trade
(`research/reports/2026-09-12_lookback_blend_construction.txt`):

| Input | Value |
|---|---|
| Excess-return correlations | 6-9 0.87, 6-12 0.82, 6-12m1 0.76, 9-12 0.89, 9-12m1 0.84, 12-12m1 0.94 |
| Grouping | {12, 12-minus-1} at ρ ≥ 0.9; top level {6}, {9}, {12 group} |
| Table 8 | top level row 3 (all rounded to 0.9): thirds; the 12 group row 2: halves |
| Table 12 column A (costs 0.099 / 0.086 / 0.078 / 0.082 SR/yr) | 32.8 / 33.4 / 17.0 / 16.8 → 33 / 33 / 17 / 17 |

The bootstrap cross-check (19.8 / 29.8 / 31.5 / 18.9), read after the weights
were fixed, sits in the same region and cannot move them. There is no FDM: a
blend of books has no combined forecast to rescale.

## 3. What this track is and is not

It is Law 5 applied to row 30: blend the windows that work, never pick the
best cell. It is not a replacement for either existing track, and not
chosen because it scored better — nothing in §2 looked at returns.

It has **no in-sample reference curve**, unlike the other two tracks: drawing
one from 2022 would read the years this raw-return family has left unread.

## 4. Pre-registered evaluation (binding)

trend-quintile's §5 terms apply verbatim: tracked against both controls from
day one; no judgement before 12 months AND 12 rebalances; success is a
positive mean monthly difference against BOTH controls; kill is a negative
mean monthly difference against equal-weight over 18 months, or realised
round-trip costs above 100 bps; frozen for the whole window. The three tracks
are compared with each other once, at the first date all three have 12 months
and 12 rebalances, and that comparison can at most motivate a new spec.

## 5. How it is tracked

`cmd/paper run -strategy momentum-lookback-blend` in `scripts/paper_daily.sh`.
Every run also records which stocks qualify for which of its four variants —
and for the other tracks' signals — for the screener's cross-strategy view.
