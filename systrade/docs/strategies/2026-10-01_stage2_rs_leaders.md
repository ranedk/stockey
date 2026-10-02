# Strategy spec — Stage 2 relative-strength leaders (NSE cash equity)

**Status: FROZEN 2026-10-01.** The forward record starts 2026-10-01. Pre-registered in
`research/LEDGER.md` row 54. Operator request (2026-10-01) after LEDGER row 53.

## 1. What is traded

- **Universe:** NSE adjusted EQ, 60-bar median traded value >= Rs 10 crore (as every track).
- **Filter:** only names whose last COMPLETED week is Weinstein Stage 2 (`internal/stage`:
  30-week average, 4-week slope, +-1% band; row 10, unchanged).
- **Ranking:** 6-month total return (126 sessions) -- the same order as relative strength
  against the market, since subtracting the market's return changes no rank.
- **Book:** the top 20, equal weight, at most 4 per Sharpely/NSE industry
  (`paper.Spec.HoldCount = 20`, `MaxPerGroup = 4`).
- **Clock:** rebalanced every 20 sessions; a held name is kept while it ranks inside the top
  40 (`KeepMultiple = 2`), the free slots go to the best names not held.
- **Exit between rebalances:** a weekly close below the 30-week average sells the name at the
  next open (`Stop = StopSignal`); its slot stays in cash until the next rebalance.
- **Execution and costs:** decide at the close, fill at the next open, 50 bps round trip;
  controls: the equal-weight universe and the same-size random book (same seed as the other
  tracks), from day one.

## 2. Why

LEDGER row 53 tested restricting Stage 2 to RS-leading industries; it failed. Its plain
Stage 2 REFERENCE book -- this rule -- returned 22.1%/yr (23.3% in the 2026-10-02 corrected run; vol 26.6%, max drawdown −47%) against
13.5% for the equal-weight universe, 2013-07 → 2021-12. That number is IN-SAMPLE and was seen
before this spec was written: the rule was not a pre-registered trial, the Weinstein family's
exploration years are spent (rows 37-40), and row 38's (a) — every Stage 2 name, no ranking —
was suggestive but not a discovery. So this track is a hypothesis earned by looking, and only
its forward record can make it evidence.

## 3. Where every number came from

- Stage definition, 30-week average exit: Weinstein via row 10, fixed since 2026-08-21.
- 20 names, max 4 per industry, 4-week clock, 2x buffer: row 53's pre-registration (written
  before that run), carried over unchanged so this track IS the book row 53 measured.
- 6-month return: the RS26 window of row 53 in sessions (26 weeks ~ 126 sessions).
- No parameter was tuned after seeing row 53's numbers.

Differences from row 53's reference book, named: this engine trades daily bars (the reference
traded weekly decisions through the sleeve harness), and when fewer than 20 names qualify at a
rebalance the chosen names share the whole book instead of leaving the empty slots in cash.

## 4. What this track is not

- Not evidence yet. Not a replacement for any other track.
- Not to be judged early on a good or bad start.

## 5. Pre-registered evaluation (binding)

- Against both controls from day one; no judgement before 12 months AND 12 rebalances
  (first possible: 2027-10-01).
- **Success:** a positive mean monthly difference against BOTH controls, after costs.
- **Also reported, against the momentum lookback blend's forward record over the same
  months:** the paired monthly difference; it is not a pass condition, but a track that only
  repeats momentum at higher turnover is not worth running beside it.
- **Kill:** a negative mean monthly difference against equal-weight over 18 months, or
  realised round-trip costs above 100 bps.
- Frozen for the whole window: any change is a new spec with its own clock.

## 6. How it is tracked

`cmd/paper run -strategy stage2-rs-leaders` in `scripts/paper_daily.sh`, writing the
`systrader_paper_*` tables under its own name; `cmd/dhan orders -strategy all` builds its
sheet as a dry run (never sent without -live and LIVE_ORDERS=yes); screener/'s Paper page
lists it. The screener's Rotation page shows the same Stage 2 / relative-strength facts.
