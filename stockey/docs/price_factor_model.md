# Multi-Factor Confidence Model (price + fundamental)

A point-in-time, validated, multi-factor "confidence" model that complements the event-driven
hypothesis path (constraint #1). The operator's idea -- more factors agreeing -> higher confidence --
is right, but only across **independent** factors. This document defines both phases; Phase 1 (factor
signals + validation) is built in `advisory/price_factors.py`.

## The trap this design avoids

A naive "count the agreeing factors" score double-counts correlated factors and inflates confidence.
Measured on real data: Momentum/Trend Spearman ~0.83 (one signal, not two); within the validatable
trend family, trend_following / 52w-distance / rel-strength / breakout cluster at 0.4-0.7 -- they
collapse to ~one independent vote. Some factors even anti-correlate. So confidence must come from
**independent components**, each of which has **earned forward-return edge**, not from a raw count.

## Factor taxonomy (oriented so higher = more bullish)

Grouped by independent component (`advisory/price_factors.py::FACTORS`):

- **trend** (collapses to ~1-2 votes): momentum, momentum_acceleration, trend_following,
  trend_quality, relative_strength, distance_from_52w_high, breakout, multi_timeframe_alignment,
  price_efficiency.
- **reversion** (counter-trend; regime-dependent, NOT additive with trend): mean_reversion,
  drawdown_recovery.
- **volatility** (compression vs expansion are inverses): volatility, volatility_compression,
  volatility_expansion.
- **volume** (confirmation overlay): volume_confirmation, relative_volume, up_down_volume_ratio.
- **liquidity** (tradability gate / size proxy): liquidity.
- **fundamental** (independent of technical -- the genuinely additive axis): earnings_growth,
  revenue_growth, leverage (low debt), cash_generation.
- **deferred**: beta / market sensitivity (needs a return-vs-benchmark regression, not a single row).

Sources: `advisory_technical_daily` (technical, point-in-time) + `advisory_fundamentals_daily`
(growth / leverage / cash, point-in-time as-of join).

## Phase 1 -- factor signals + validation (BUILT)

`advisory/price_factors.py`:
- `compute_factors(technical_row, fundamental_row)` -> oriented factor values for one (symbol, date).
- `factor_ic_report(frame, forward_col)` -> per-factor cross-sectional Spearman IC + top-minus-bottom
  quintile spread (the "does it pay" test).
- `factor_correlation(frame)` -> the independence map.

Validation is **cross-sectional** (does the factor rank predict a stock's forward return *relative to
the universe that day*) -- the standard factor-IC method, and it needs no benchmark index (the stored
NIFTY history is too shallow). A factor must earn IC before it is trusted, exactly as an event
hypothesis must earn a promotion-audit.

### Phase 1 findings (forward 20d cross-sectional excess, ~22k obs, 2018-2024)

| factor | component | IC | top-minus-bottom |
|---|---|---|---|
| earnings_growth | fundamental | +0.089 | +2.7% |
| leverage (low debt) | fundamental | +0.074 | +2.0% |
| revenue_growth | fundamental | +0.060 | +2.0% |
| mean_reversion | reversion | +0.053 | +1.4% |
| relative_strength | trend | +0.030 | +1.4% |
| distance_from_52w_high | trend | +0.025 | +0.8% |
| breakout | trend | -0.048 | -0.9% |
| liquidity | liquidity | -0.081 | -3.0% |

- **Fundamentals lead** (growth + low leverage are the strongest validated predictors).
- **Liquidity is negative** -- the small-cap/size premium (less-liquid names outperform).
- **Chasing extended breakouts hurts** at 20d (extension mean-reverts).
- **Trend factors are 0.4-0.7 correlated** -- they collapse to ~one vote, confirming the no-naive-count rule.
### Data-coverage gap CLOSED (compute factors from raw OHLCV)

The richer technical features (momentum returns, persistence, ATR, volume metrics) are NULL in deep
history -- the feature builder added them recently. But adj_close/high/low/volume are 100% populated.
So `compute_price_series_factors(closes, highs, lows, volumes)` computes the technical factors
point-in-time directly from the raw trailing OHLCV series (the deep-history path), instead of reading
the null pre-computed columns. A factor model should compute from price anyway.

Re-validated with deep-history series-computed technicals (forward 20d cross-sectional excess):
earnings_growth still leads (IC +0.110); momentum is now validatable and positive (+0.054); the
trend family (relative_strength / 52w-distance / multi_timeframe / trend_following) is modestly
positive AND **0.7-0.87 correlated -- one signal, not five**.

**Caveat that shapes Phase 2:** the deep-history universe is thin (~20-29 symbols), so cross-sectional
ICs are noisy and SOME SIGNS FLIP across samples (leverage was +0.074 on the older-feature 22k-obs
run, -0.063 on the deep-technical 1.2k-obs run). Therefore Phase 2 must use **robust, sign-validated
component weights with shrinkage toward equal-weight / literature priors -- NOT precise weights fit
to a 20-symbol sample.** Re-fit as more symbols accumulate deep history.

## Phase 2 -- the confidence score (DESIGN)

Combine **validated, de-correlated components** into one confidence score:

1. **Collapse correlated factors to one component signal.** The trend family -> a single trend score
   (e.g. the first principal component, or the best-validated representative). Don't sum six copies.
2. **Weight components by validated edge**, not equally. On current evidence fundamentals dominate,
   liquidity (small-cap) and mean-reversion contribute, trend is a modest single vote, breakout-chasing
   is penalised. Re-fit weights as more technical history matures.
3. **Confidence = agreement across INDEPENDENT components** -- the real boost is technical AND
   fundamental AND volume pointing the same way (independent), not six trend factors agreeing.
4. **Regime-switch reversion vs trend.** Mean-reversion and momentum are opposites; select by regime
   (trend in trending tapes, reversion in ranges) -- they are not additive.
5. **Validate the COMBINATION.** Does higher confluence actually predict higher forward excess than a
   single factor, after cost? Test it; do not assume confluence adds edge (it usually adds some, not 6x).
6. **Wire it in** as a price-pattern thesis that grounds alpha through the same decision contract, and
   feeds the LLM packet's `technical_confirmation` (timing) and a new `fundamental` dimension.

Like hypotheses, a factor / the combined score only grounds a live decision after it has earned edge
on real, point-in-time data -- reputation never substitutes for the audit.
