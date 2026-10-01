# Strategy spec — Stage 2 relative-strength leaders, fundamentally clean (paired variant)

**Status: FROZEN 2026-10-01.** The forward record starts 2026-10-01, the same day as
stage2-rs-leaders. Pre-registered in `research/LEDGER.md` row 55. Operator request.

## 1. What is traded

stage2-rs-leaders (`docs/strategies/2026-10-01_stage2_rs_leaders.md`) in every respect --
universe, Stage 2 filter, 6-month ranking, 20 names, max 4 per industry, 20-session clock, 2x
buffer, 30-week-average exit, costs, controls and random seed -- with ONE addition:

- **Fundamental filter (stockey):** a name may be bought, or kept at a rebalance, only if on the
  decision day it has a stockey story score, NO flaw, and a score at or above that day's median
  of all scored companies (`paper.Spec.FundamentalFilter`). Flaws: cash burn with losses, two
  loss years (waived for stockey's scaling-growth / turnaround admits), leverage in its group's
  top tenth with interest cover below 2, an auditor change or material related-party deal in the
  last year, promoter pledge >= 50% or rising in 6 months. A name stockey does not score fails.
- The filter is NOT an exit: a held name that starts failing it is sold only at the next
  rebalance (not kept), never between rebalances.

Data: stockey's `fundamentals_story_filter_daily` (synced nightly, point in time by date, the
newest score version on each date). A score older than 7 days is carried forward and the run
prints a warning.

## 2. Why

The operator's strategy uses fundamentals to AVOID bad companies, not to pick winners -- the
factor research found no fundamental selection edge but did not test avoidance on a price
book. Story scores exist only from 2026-09-29, so this cannot be backtested: a paired forward
record against the identical price-only book is the only honest test. On day one the filter
changed 13 of 20 names.

## 3. Pre-registered evaluation (binding)

- Both tracks' own terms apply (12 months AND 12 rebalances, first possible 2027-10-01).
- **The question this track exists for:** the paired monthly difference, clean minus
  price-only, after costs, with its bootstrap p and the two books' drawdowns beside it. The
  filter is adopted for this family only if the clean book is ahead after costs AND its worst
  drawdown is no deeper; otherwise the price-only rule stands.
- Frozen for the whole window; changing the story score's definition (a new score_version in
  stockey) is named in the evaluation, since it changes this track's input.

## 4. How it is tracked

`cmd/paper run -strategy stage2-rs-leaders-clean` in `scripts/paper_daily.sh`; listed on the
screener's Paper page next to stage2-rs-leaders.
