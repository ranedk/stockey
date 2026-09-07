# Rule Ideas Beyond EWMAC + Carry

Phase 1 ships with EWMAC (3 slow variations) + carry only — Carver: these two
capture ~85% of his full system's backtested performance. Everything below is
Phase 2+, admitted one at a time through the research protocol at the bottom.
Every candidate listed with its story (Law 1) and expected correlation to what
we already run.

## Tier 1 — strong candidates (Carver-adjacent, cheap, proven premia)

1. **Breakout (normalized Donchian).** Forecast = 40 × (price − mid of N-day
   range) / range, smoothed; variations N = 40, 80, 160, 320. Story: same
   under-reaction premium as EWMAC but a *path-independent* trigger — reacts to
   levels, not smoothed velocity. Corr with EWMAC ~0.6–0.7 yet adds value in
   Carver's own later work (it's his favorite second trend family). Positive
   skew, cheap, trivially continuous. ~~**First rule to add.**~~

   **MEASURED 2026-09-07 (LEDGER row 14) — the correlation claim above is wrong
   on NSE equities.** Breakout is EWMAC read at roughly 5x the lookback:
   breakout80 vs ewmac16_64 rho=0.94, breakout160 vs ewmac32_128 0.94,
   breakout320 vs ewmac64_256 0.93, breakout40 vs ewmac16_64 0.84 (pooled over
   2.68M liquid symbol-days; the within-symbol averages agree to +/-0.03). Every
   variation breaches the admission protocol's rho>0.7 duplicate bar against the
   very family it was supposed to diversify. It stays implemented
   (`rules.Breakout`) because it remains the natural proxy if it ever proves
   cheaper to trade, but it is NOT a second trend family and must not be handed
   its own weight.

2. **Acceleration (momentum-of-momentum).** Forecast ∝ EWMAC_N(t) −
   EWMAC_N(t − N). Story: fresh trends outperform stale ones (herding builds
   gradually); catches turns earlier, exits exhausted trends. Corr with EWMAC
   level ~0.3–0.5. Moderate turnover — must pass the speed limit per instrument.

   **MEASURED 2026-09-07 (LEDGER row 14): confirmed, and it is the only rule in
   the library that is genuinely distinct.** accel16 vs the EWMAC family
   ρ = 0.33 / 0.00 / -0.13 for fast 16/32/64, and only 0.42 against accel32 —
   the three spans are not even one family among themselves. Whether that
   independence is worth anything is a returns question, still untested.

3. **Long-term mean reversion / value.** Forecast ∝ −(price − 5yr average) ÷
   vol, very slow. Story: multi-year overshoot reverts (De Bondt–Thaler);
   this is the classic *negatively correlated* complement to trend (corr ≈ −0.2
   to 0). Tiny turnover, works on asset-class indices. Negative skew flavor —
   weight accordingly (Law 15).

   **MEASURED 2026-09-07 (LEDGER row 14) — "complement" is false here.** At a
   2-year window it is not weakly negative but almost exactly minus slow trend:
   meanrev512 vs ewmac64_256 ρ = -0.95. A rule that is the negative of one we
   already run adds no diversification; giving it a weight just nets down trend
   exposure, which the trend weight itself could do more honestly. Only the
   5-year window (meanrev1280, ρ = -0.69 against ewmac64_256) is far enough away
   to be a separate group, and even that is mostly the same bet inverted.

4. **Cross-sectional momentum (equity sleeve).** Rank stocks/sectors by 6–12m
   vol-adjusted return, skip last month; forecast from rank z-score, long-tilt
   only in the cash/ETF sleeve. Story: relative-strength premium is distinct
   from time-series momentum (funded by index-hugging institutions rebalancing
   against winners). Needs point-in-time universe (Law: no survivorship).

## Tier 2 — worth testing, weaker priors

5. **Carry variants**: stock-futures basis (rich in India), ETF
   premium/discount to iNAV, calendar-spread slope on MCX metals. Same story
   as core carry; test as variations, not new rules (they'll correlate highly).
6. **Skew premium.** Prefer assets whose recent return distribution is
   negatively skewed (you get paid to hold what others find uncomfortable).
   Carver's later research finds a modest premium. Slow, low corr with trend.
7. **Short-term index mean reversion** (2–5 day). Real pre-cost edge in equity
   indices, but fast ⇒ probably fails the 0.13 SR speed limit outside NIFTY
   futures. Test honestly, expect the ledger to kill it.
8. **Seasonality** (gold festive demand, agri harvest cycles). Weak prior,
   heavy M-risk (12 months × K assets = many implicit tests). Only with a
   pre-registered hypothesis.

## Regime classifiers (NOT `rules.Rule` — reporting only)

**Weinstein 4-stage weekly classifier** (`internal/stage`, `cmd/stage`,
`research/LEDGER.md` row 10). Discrete Basing/Advancing/Topping/Declining
label from a 30-week SMA + its slope — deliberately does NOT implement
`rules.Rule` (no continuous forecast, nothing here may touch sizing per
corollary 1) and ships as regime-tagged reporting per innovation #7 below
(Law 16: visible to a human, never wired into position switching). All
parameters are Weinstein's own published ones, fixed ahead of any NSE test —
see the ledger row and `internal/stage`'s doc comment for the M-accounting
rationale. If this is ever promoted to influence positions, that means
recasting it as a continuous vol-standardized forecast and running it
through the normal admission protocol below — not wiring the discrete label
in directly.

## TimesFM as a rule (the ML experiment)

Google's TimesFM is a pretrained time-series foundation model. Integration
design that keeps it a *legal citizen* (Law 8 / corollary 1):

- **Architecture**: Python sidecar (FastAPI) serving `forecast(prices[], horizon)`
  → predicted k-day return distribution; Go calls it like any other rule and
  converts: raw = E[k-day return] ÷ (vol × √k), then empirical scalar → avg 10,
  cap ±20. It plugs into the combiner with a handcrafted weight like everyone
  else — it gets NO special authority.
- **Honesty hazards, all must be handled**:
  - *Pretraining leakage*: the public checkpoint has seen history that overlaps
    our backtest. Walk-forward evaluation is only clean on dates AFTER the
    model's training cutoff; earlier "backtests" are upper bounds, label them so.
  - *M accounting*: every prompt/config/horizon variant is a ledger entry.
  - *Explainability*: its story is weak ("a big model sees patterns") — by Law 1
    it therefore needs a HIGHER evidence bar and a small initial weight (≤10%),
    admitted only if live paper-trading confirms.
- Expected value: honest prior is modest — daily-bar alpha from a generic
  pretrained model is unproven. We run it because the harness makes the test
  cheap and the correlation question ("does it see something EWMAC doesn't?")
  is answerable with the matched-control machinery.

## Innovations over the book (this project's additions)

1. **The research ledger with automatic M-deflation** (`research/LEDGER.md` +
   `stats.BonferroniBar`): every backtest report prints its t-stat against the
   bar implied by the ledger's current M. The book prescribes the discipline;
   we make the tooling enforce it.
2. **Matched-control baselines built into the engine**: any rule can be scored
   as edge-over-random-entries (same instrument, period, filters) with
   bootstrap confidence bands — Law 3 as code, catching beta-masquerading-as-alpha
   automatically.
3. **No-look-ahead property tests**: mutate future data, assert past positions
   unchanged. Most silent backtest frauds die here.
4. **Cost-doubling sensitivity** in every report: a strategy that survives
   2× costs is robust; one that doesn't is a broker donation scheme.
5. **Two-sleeve capital design** (futures dynamic + ETF long-only) — adapts the
   book's three trader archetypes into one portfolio suited to Indian lot sizes.
6. **Bootstrap everything**: weight estimation (App C) and drawdown
   distributions via resampling daily P&L — sets realistic expectations for the
   nerve question (open question A4).
7. **Regime-tagged reporting (never regime-switched trading)**: performance
   broken out by vol regime for diagnostics only — acting on it would be
   meddling (Law 16).
8. **Later, real-fill calibration**: once live, measured slippage feeds back
   into the cost model, replacing estimates with facts (the one kind of number
   Carver lets us trust).

## Admission protocol for any new rule

1. Write the story (who pays, why it persists) in the ledger BEFORE coding.
2. Implement against the `rules.Rule` interface; verify continuous/scaled/capped.
3. Pooled, out-of-sample backtest across all instruments; costs on.
4. Score: t-stat of edge vs matched baseline, against the current Bonferroni
   bar; correlation with every incumbent rule (reject if ρ > 0.7 with the
   family it duplicates unless cheaper); skew; cost drag; cost-doubled SR.
5. If admitted: handcrafted weight by correlation grouping (never by SR),
   3-month paper-trade before real weight.
6. Log outcome in the ledger either way. Rejections are data.
