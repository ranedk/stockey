# Pre-registration — industry rotation (relative strength leaders, Stage 2 inside them)

Written 2026-10-01, before any backtest of these books has been computed. Spec:
`docs/SECTOR_ROTATION_PRD.md`. The rotation VIEW (cmd/rotation snapshot) has been run and
its current-week output looked at; no historical return of any book below has been seen.

## 1. Hypothesis

Industry momentum (Moskowitz & Grinblatt 1999): industries that led over the past six months
keep leading for months. The stock-level Weinstein family is spent on these years (rows
37-40: Stage 2 = the momentum premium, nothing more). This tests ONE new thing: whether
restricting Stage 2 stocks to the industries leading on relative strength improves on plain
Stage 2 chosen the same way.

## 2. Data and definitions (internal/rotation, as committed with this file)

- NSE adjusted EQ (the slice bar cache), Rs 10 cr floor = 60-bar median traded value, judged
  at the end of the previous week. Weekly = last close of the ISO week.
- Market index = equal-weight weekly return of eligible names (each weekly return capped at
  +-50%). Industry index = the same over an industry's eligible members; an industry is
  ranked only in weeks it has >= 5 eligible members. 58 Sharpely/NSE industries;
  membership is TODAY's classification applied to all history (named caveat).
- RS26 = 26-week return minus the market's. Stages: internal/stage (row 10), unchanged.
- Leading industry = RS26 rank in the top fifth (ceil(0.2 x ranked)) AND its index in Stage 2.

## 3. The books (one family, trials = 2)

Common: 20 names, equal weight, at most 4 per industry; decisions on the last completed week;
a REBALANCE every 4 weeks; between rebalances a holding is sold on a weekly close below its
30-week MA and its slot stays in cash until the next rebalance.

- **R1 industry rotation.** At a rebalance: keep a holding if it is still eligible, not
  below its MA, its industry ranks in the top two fifths (the 2x buffer of rows 49-50), and
  its RS26 ranks in the top 40 among eligible Stage 2 names in top-two-fifths industries.
  Then fill empty slots from the entry list — eligible Stage 2 names in LEADING industries,
  by RS26, highest first — respecting the per-industry cap.
- **R2 = R1 with a market filter.** While the market index is in Stage 4, hold nothing; the
  week it leaves Stage 4 the book is rebuilt at once (not at the next scheduled rebalance).

Reference (not a trial): **P plain Stage 2** — identical machinery with every industry
condition removed: entry = eligible Stage 2 names by RS26; keep = eligible, not below the MA,
RS26 rank in the top 40 among eligible Stage 2 names; same cap.

## 4. Setup (as rows 37-40)

`slice stage -part rotation`. Decisions 2013-07-01 -> 2021-12-31 (2022+ untouched), daily
sizing units from the last completed week, decide at a close, fill at the next open, sleeve
rebalance clock 5 trading days, 50 bps round trip (100 bps sensitivity). Controls in the same
run: the equal-weight universe and the same-size stable-shuffle book; the momentum lookback
blend run alongside. Monthly paired differences, block bootstrap (5-month blocks, 10,000
reps, seed 1), FDR 10% within the family.

## 5. Deciding

A book PASSES only if all three hold:
1. it survives as rows 38/40 did — matched-risk edge over the tougher of the two controls,
   an FDR-10% discovery within {R1, R2}, its 100-bps edge not negative against either;
2. it beats P (plain Stage 2) on monthly net returns, paired, bootstrap p < 0.10 — otherwise
   the industry layer is not what did the work;
3. its matched-risk monthly mean is not below the momentum lookback blend's run here (point
   estimate, reported with its p; not a significance test).

A pass goes to a forward paper track with the stockey fundamental filter as a paired variant
(the filter has no history before 2026-09-29 and cannot be backtested). A fail is recorded and
the Rotation page stays a reporting tool. Reported, not deciding: turnover, mean names held,
weeks invested (R2), correlation with momentum, and the result without the 50% return cap.

## 6. Ledger

LEDGER row 53, trials = 2, whatever the outcome. Report:
`research/reports/2026-10-01_industry_rotation.txt`.
