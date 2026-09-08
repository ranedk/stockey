# The Trading Bible

Distilled from Robert Carver's *Systematic Trading* (2015) and our chapter-by-chapter
study of it. **Every design decision, line of code, and backtest in this repo must
comply.** A condensed version is installed as the `trading-bible` skill; this file is
the annotated reference. When code and bible conflict, the code is wrong.

**Amended 2026-09-08 by the operator.** Laws 2 and 4 now defer to
`docs/RESEARCH_PROTOCOL.md` on how the multiple-testing bar is computed and on
how out-of-sample evidence is generated. The original wording is preserved
below with the amendment marked, because a law whose history is invisible is
one nobody can argue with later. Everything else stands unchanged.

---

## Part I — Research discipline (Ch 3–4: Fitting, Portfolio Allocation)

### Law 1: Ideas first
Every trading rule must have an economic story before any performance data is
examined: *who is on the other side of the trade, and why do they keep paying us?*
Acceptable stories: risk premia, skew premia, leverage aversion, forced/hedging
flows, liquidity provision, behavioral biases (under-reaction → trends).
No story → the rule is presumed noise.

### Law 2: Count M — the research ledger
Every rule variation ever tested against a dataset counts toward the
multiple-testing burden M. All experiments are logged in `research/LEDGER.md`
(rule, params, dataset, date range, result, decision). The significance bar is
Bonferroni: required t ≈ Φ⁻¹(1 − 0.025/M).

| M (variations tried) | 1 | 10 | 50 | 100 | 1000 |
|---|---|---|---|---|---|
| required t-stat | 1.96 | 2.81 | 3.29 | 3.48 | 4.06 |

The luckiest of M junk rules shows t ≈ √(2·ln M) by chance alone. A "great
backtest" found by scanning is the *expected output of scanning noise*, not
evidence. Batching runs differently does not reduce M: the best-of-50-batches
of 10 is the best-of-500.

**AMENDED 2026-09-08.** The counting stands; the bar changes. A single
workspace-wide Bonferroni was being applied across unrelated research families
— by 2026-09 M had reached 1114, of which 855 were imported cron *monitoring*
runs, so every new idea faced t = 4.08 because a factor-IC monitor had run
nightly for two months. That is arithmetic punishment, not discipline. From
now on:

- **Correct within the family that was actually searched**, using FDR at 10%
  plus a deflated Sharpe that accounts for the number of configurations tried
  in THAT family. Report the workspace-wide bar alongside, for context.
- **Every experiment still gets a ledger row**, and M is still counted and
  still published. Nothing here reduces what must be written down.
- **Exploration rows declare `trials=0`** and make no significance claim of any
  kind. They are logged precisely so a mined dataset can never later be
  mistaken for a fresh one.

### Law 3: Edge = performance − matched baseline
Never quote raw win rates or raw Sharpe. A pattern that wins 80% while random
entries in the same stocks/period win 90% has NEGATIVE edge. Baselines must match
instrument, period, direction, and any filter (e.g. above-200DMA universe).
The backtester's matched-control harness exists for this.

### Law 4: Out-of-sample or it didn't happen
- Expanding/rolling-window fitting only; no parameter may see the data it is
  scored on.
- Pool data across instruments; one generic (vol-standardized) rule for all,
  never per-instrument tweaks ("there is never enough evidence that gold is
  special").
- A holdout set is burned the moment it influences a decision twice.
- Cheapest honest data: paper-trade the frozen rule forward.

**AMENDED 2026-09-08.** Three changes, all in `docs/RESEARCH_PROTOCOL.md`:

- **Slicing on a NAMED attribute is allowed** — liquidity tier, sector, size,
  volatility state, market regime — and is not what "per-instrument tweaks"
  forbids. Carver's rule comes from a 40-instrument futures book where fitting
  gold separately is obviously overfitting; it was being applied to a
  3,000-stock cross-section as though it were the same situation. What stays
  forbidden is choosing the slice *because it scored well* and then quoting
  that slice's statistic as evidence. Explore freely, confirm elsewhere.
- **Purged walk-forward replaces the one-shot holdout as the default.** Fit on
  an expanding window, score the next block, purge the forward-return overlap
  so no observation sits on both sides. Thirteen years burned once per family
  is about five decisions before the evidence runs out; we were rationing
  evidence rather than generating it. A true one-shot holdout remains available
  and remains burned on use.
- **Forward paper trading is the gate before any capital moves**, which this
  law's own last line already said. It is the only evidence budget that
  regenerates, and the only one that cannot be mined.

### Law 5: Don't select — blend
Selecting the best variation by backtest Sharpe destroys value (Carver's gold
experiment: pick-the-winner SR 0.07 < random 0.20 < equal-blend 0.33). Choose
variations by **behavior** — speed, cost, mutual correlation (drop any pair with
ρ > 0.95) — and keep several with fixed weights.

### Law 6: Handcrafted weights
Portfolio weights (forecast weights and instrument weights) come from the
handcrafting method: group by correlation, equal weight within groups, multiply
down the tree; Table-8 values for uneven triplets. Correlations are estimable
from realistic data; Sharpe differences are not.
Sharpe adjustments to weights only per Table 12: aggressive for **costs** (they
are facts), tiny for ≥10 years of evidence, **zero** for less.

### Law 7: Discount every backtest
Multiply backtested SR by ≤ 0.75 (honest out-of-sample bootstrap) before using
it anywhere (esp. Kelly). Hard ceilings regardless of backtest: SR ≈ 1.0 for a
diversified multi-asset system, ≈ 0.4 per instrument, ≈ 0.3 per single rule.
Anything above: assume (in order) bug → look-ahead → hidden negative skew →
over-fitting.

---

## Part II — The framework (Ch 5–12)

### Law 8: Forecasts
- Continuous, never binary. The forecast **is** the entry and the exit; no
  separate stop-loss for the systems trader.
- Proportional to expected return ÷ volatility (risk-adjusted, comparable
  across instruments and time).
- Scaled so long-run average |forecast| = 10 (fixed scalars, fitted without
  performance data: EWMAC 2/8→10.6, 4/16→7.5, 8/32→5.3, 16/64→3.75,
  32/128→2.65, 64/256→1.87; carry→30).
- Hard cap ±20 (evidence is thin in the tails; extremes revert; low-vol
  denominators lie). Long-only instruments: clip to 0…+20.

### Law 9: Combining forecasts
Combined forecast = Σ(wᵢ·fᵢ) × FDM, then re-cap at ±20. Forecast weights
handcrafted (reference set: EWMAC16/64 21%, EWMAC32/128 8%, EWMAC64/256 21%,
carry 50%; FDM 1.31). Floor negative correlations at 0 in any multiplier
computation. **FDM ≤ 2.5 always.**

### Law 10: One risk dial (volatility targeting)
- Percentage volatility target is where capital and courage enter — nowhere else.
- Kelly optimum = expected SR; we use **half-Kelly on the deflated SR**, halved
  again for negative skew. Absolute max 50%; this project starts ≤ 20%.
- Cash vol target = pct × **current** capital, recomputed daily (losses de-risk,
  profits compound).
- Percentage target changes at most once, only downward, only if pain was
  misjudged. Expected losses at 20%/₹50L: worst day/month ≈ ₹1L; worst
  week/year ≈ ₹2.2L; 10-yr P(lose half) < 1%. Know these numbers before go-live.

### Law 11: Position sizing
subsystem position = (daily cash vol target ÷ instrument value volatility) ×
combined forecast ÷ 10.
Instrument value vol = price-unit vol (≈ 36-day EWMA std of daily price changes)
× point value × FX. Never trade instruments with suspiciously low volatility
(pegs, near-expiry STIR): small denominators order giant positions that die on
regime breaks (CHF 2015).

### Law 12: Portfolio assembly
final position = subsystem position × instrument weight × IDM. Subsystem
correlations ≈ 0.7 × instrument correlations. **IDM ≤ 2.5.** Round to whole
blocks only at the very end; then **position inertia**: no trade unless
|current − target| > 10% of target.

### Law 13: Costs and the speed limit
- Standardized cost (SR units/round trip) = 2·C ÷ (16·ICV). Cost of a system on
  an instrument = turnover × standardized cost.
- **Spend at most ⅓ of realistic pre-cost SR on costs → 0.13 SR/yr cap per
  instrument** (0.08 for static/discretionary sleeves).
- Costs are facts, pre-cost SRs are guesses: never prefer a fast expensive rule
  over a slow cheap one on backtest evidence.
- Day trading is arithmetically excluded. Low-vol instruments are expensive in
  SR terms by construction — third reason to shun them.

### Law 14: Instruments — the four-block rule
Max position = 2 × vol scalar × instrument weight × IDM must be **≥ 4 blocks**,
else: modestly raise the weight (like Carver's Euro Stoxx 20% floor), shrink the
portfolio, or drop the instrument. Prefer one instrument per asset class before
doubling up within a class. Diversification across low-correlation instruments
is the only free lunch (SR ~ √N for uncorrelated bets); it beats adding rules,
always beats trading faster.

### Law 15: Respect skew
Negative-skew strategies (carry, vol selling, mean reversion) look like free
money until the rare disaster; their measured vol understates true risk. Halve
their risk budget, cap their portfolio share (Carver: 10% for V2TX), and pair
them with positive-skew trend rules. If live results show steady gains with
almost no losses, assume hidden negative skew — not genius.

---

## Part III — Conduct (Ch 1, 9, Epilogue)

### Law 16: Diligent in design, lazy in operation
After go-live: no meddling, no discretionary overrides, no mid-drawdown
"improvements." The 2008 lesson: the humans wanted to switch the system off
while it made $1bn following its rules.

### Law 17: The seven virtues
Humble (assume it will go badly), skeptical (trust nobody, including the
author), pessimistic (future < backtest), thoughtful (know why you earn),
thrifty (don't enrich the broker), nervous (only money you can lose entirely),
and aware that luck dominates short horizons.

---

## Part IV — Engineering corollaries (this project)

1. **Uniform rule interface.** Anything emitting a continuous, vol-standardized,
   avg-10, cap-±20 forecast is a legal rule — EWMAC, carry, breakout, or an ML
   model (TimesFM). Nothing else may influence positions.
2. **Point-in-time everything.** Universe membership, lot sizes, corporate
   actions, delistings — as known on date t. Survivorship is a silent Sharpe
   inflator.
3. **No look-ahead, provably.** Tests must verify that changing data at t+1
   cannot change the position at t.
4. **Every backtest report prints:** net SR, deflated verdict given ledger M,
   t-stat, skew, max drawdown, annual turnover, cost drag in SR units, and the
   cost-doubled SR (sensitivity).
5. **The ledger is append-only** (`research/LEDGER.md`). Deleting failed
   experiments is self-deception with version control.
