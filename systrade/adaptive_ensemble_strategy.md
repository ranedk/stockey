# Adaptive Indicator-Ensemble Trading Strategy — Development & Backtest Spec

**Version:** 0.1 (draft)
**Scope:** Cash equities only (NSE). No derivatives, no leverage, no shorting (long/flat only).
**Purpose:** Define the strategy precisely enough that a backtest can be implemented once, run against a train/verification split, and accepted or rejected against explicit criteria — with no parameter tuning on the verification set.

---

## 1. Objective

Build a long/flat systematic strategy that:

1. Computes signals from N technical indicators drawn from orthogonal families.
2. Maintains per-indicator performance weights, updated online from realized outcomes.
3. Trades only when the top-ranked indicators agree on direction above a consensus threshold.
4. Beats the benchmark (NIFTY 50 total-return buy-and-hold) on **risk-adjusted** terms after costs, or is rejected.

Non-goals: intraday trading, stock-picking alpha, prediction of price levels. The system predicts direction/exposure only.

---

## 2. Universe & Data

| Item | Specification |
|---|---|
| Instruments | NIFTY 50 index proxy (NIFTYBEES ETF) as primary; optionally top-10 liquid NSE large-caps as a robustness set |
| Data | Daily OHLCV, adjusted for splits/dividends |
| History required | ≥ 12 years (e.g., 2012-01-01 to 2024-12-31) to cover multiple regimes: 2013 taper tantrum, 2016 demonetization, 2018 correction, 2020 crash, 2021 bull, 2022 sideways |
| Source | NSE bhavcopy / yfinance / broker API — must be point-in-time (no survivorship-biased universe if using stocks) |
| Frequency of decisions | Weekly (decision every Friday close, execution Monday open). Daily variant may be tested in train set only. |

**Data hygiene rules**

- All indicator values at decision time T use data up to and including close of T. Execution price is next session's open. Never use close of T as the fill price for a decision made at close of T unless modeling MOC orders explicitly.
- Corporate-action adjusted series throughout; verify no lookahead in the adjustment.

---

## 3. Data Split (Train / Verification)

Two-part split, hard firewall:

| Segment | Period | Purpose |
|---|---|---|
| **Part A — Train/Development** | 2012-01-01 → 2019-12-31 | All exploration, parameter tuning, indicator selection, walk-forward tuning happens here and only here |
| **Part B — Verification (holdout)** | 2020-01-01 → 2024-12-31 | Run **once**, with parameters frozen. No iteration. |

**Rules:**

1. Part B is touched exactly one time, after development is declared complete and parameters are committed (write them to a frozen config file with a git tag/date before running).
2. If Part B fails acceptance criteria, the strategy is rejected. You may start a new research cycle, but the old Part B is now contaminated — a genuinely new verification would need out-of-sample time (paper trading / newer data).
3. Within Part A, use **walk-forward validation** for tuning: e.g., tune on 2012–2015, validate 2016; roll forward yearly. This limits overfitting even inside the train set.
4. A "burn-in" of 250 trading days at the start of any segment is excluded from performance measurement (indicators and weights need warm-up).

---

## 4. Indicator Set

Choose **6 indicators, one per family**, to minimize redundancy. Initial set (all standard definitions):

| # | Family | Indicator | Params (initial) | Signal definition s_i ∈ [-1, +1] |
|---|---|---|---|---|
| 1 | Trend | MACD (12, 26, 9) | fixed | tanh(k₁ × MACD_hist / ATR₂₀) |
| 2 | Trend strength | ADX(14) + DI | fixed | sign(+DI − −DI) × min(ADX/50, 1), 0 if ADX < 15 |
| 3 | Mean reversion | RSI(14) | fixed | clamp((50 − RSI)/30, −1, +1) — contrarian |
| 4 | Volatility position | Bollinger %B (20, 2σ) | fixed | clamp(2 × (0.5 − %B), −1, +1) — contrarian |
| 5 | Volume | OBV slope (20-day linear regression, normalized) | fixed | tanh(k₂ × OBV_slope) |
| 6 | Momentum | 12-week rate of change | fixed | tanh(k₃ × ROC₁₂w / σ₁₂w) |

Notes:

- Each signal is **continuous**, not binary. Sign = direction, magnitude = conviction.
- Normalization constants k₁..k₃ are calibrated once on the first train window so that signal magnitudes have comparable distributions (e.g., interquartile range ≈ [−0.5, 0.5]); then frozen.
- **Redundancy check (mandatory):** compute pairwise Pearson correlation of signal series on the train set. If any pair has |ρ| > 0.7, replace one of the pair. Document final correlation matrix in the results report.
- Maximum 6 indicators. Do not add more without removing one. Every added indicator is added overfitting surface.

---

## 5. Weight Update Rule (Online Learning)

Multiplicative-weights / Hedge-style, updated at each decision date T:

```
For each indicator i:
    r_i(T)   = s_i(T−1) × market_return(T)        # signal's hypothetical PnL this period
    score_i  = EWMA of r_i with half-life H        # H = 8 weeks (initial)
    w_i(T)   = exp(η × score_i / σ_scores)         # η = learning rate, initial 1.0
    w_i(T)   = w_i(T) / Σ w_j(T)                   # normalize to sum 1
```

- Performance is measured by **signal PnL** (r_i), not directional hit-rate. A signal right on big moves and wrong on small ones must score higher than the reverse.
- Half-life H and learning rate η are the **only two tunable parameters** of the weighting engine. Tune on Part A walk-forward only. Grid: H ∈ {4, 8, 13, 26} weeks; η ∈ {0.5, 1.0, 2.0}.
- Weight floor: w_i ≥ 0.02 (no indicator is ever fully eliminated; regimes return).

---

## 6. Consensus & Trading Rule

At each decision date T:

1. Rank indicators by w_i. Take the **top K = 4** of 6.
2. Compute ensemble signal: S(T) = Σ (over top-K) w_i × s_i(T), renormalized by Σ w_i of top-K.
3. **Agreement filter:** count how many of the top-K signals share the sign of S(T). Require ≥ 3 of 4.
4. **Conviction filter:** require |S(T)| ≥ θ. Initial θ = 0.25. Grid on train set: {0.15, 0.25, 0.35}.
5. **Volatility filter:** if ATR₂₀/price is in the top decile of its trailing 2-year distribution, force flat (no new positions; exit existing).

**Position rule (long/flat only):**

| Condition | Position |
|---|---|
| S(T) ≥ θ, agreement passed, vol filter passed | Long, size = min(1.0, |S(T)| / 0.6) of capital |
| S(T) ≤ −θ (bearish consensus) | Flat (cash / liquid fund) |
| No consensus / filters fail | Flat |

- Rebalance only if target position differs from current by > 15 percentage points of capital (reduces churn).
- Idle cash earns the risk-free rate (model at 6% p.a. or actual T-bill series).

---

## 7. Cost Model (mandatory in every backtest run)

Per round trip on NSE cash delivery, model conservatively:

| Component | Assumption |
|---|---|
| STT (delivery) | 0.1% on both buy and sell |
| Brokerage | 0 (discount broker delivery) — but include ₹ per-order fees if applicable |
| Exchange + SEBI + stamp | ~0.005% |
| GST on charges | 18% on applicable charges |
| Slippage | 0.05% per side (ETF/large-cap); 0.10% per side for stress test |

Every reported metric must be **after costs**. Also report the zero-cost variant only as a diagnostic (large gap between the two = strategy trades too much).

---

## 8. Backtest Engine Requirements

1. Event-driven or vectorized — either is fine, but must enforce: decision at close(T) → execution at open(T+1).
2. Deterministic and seeded (any randomness, e.g., tie-breaks, must be reproducible).
3. Config-driven: all parameters in one YAML/JSON file. The frozen config used for Part B must be committed before Part B is run.
4. Output per run: equity curve, position series, per-indicator weight history, trade log, metrics table (Section 9).
5. Unit tests: (a) lookahead test — shift all prices by one day and assert results change; (b) cost test — assert net < gross; (c) a synthetic-data test where a known perfect indicator should dominate weights within ~3 half-lives.

---

## 9. Metrics & Acceptance Criteria

Compute for strategy and benchmark (NIFTY 50 TR buy-and-hold), after costs, excluding burn-in:

**Metrics:** CAGR, annualized volatility, Sharpe (rf = 6%), max drawdown, Calmar, % time in market, number of trades/year, turnover, hit rate of taken trades, average gain/loss ratio, monthly return table, per-indicator average weight over time.

**Acceptance criteria on Part B (all must hold; decide these before running Part B):**

1. Sharpe(strategy) ≥ Sharpe(benchmark) + 0.2, OR CAGR within 2 pts of benchmark with max drawdown ≤ 0.6 × benchmark drawdown.
2. Max drawdown ≤ 30%.
3. Strategy is profitable after costs in absolute terms.
4. Turnover ≤ 15× per year (sanity bound; higher means costs will kill it live).
5. **Robustness (run on Part A only, before freezing):** performance does not collapse (Sharpe drop > 50%) when each parameter is perturbed one step on its grid, or when tested on the secondary instrument set.

If Part B passes → proceed to 6-month paper trading (shadow mode) against live data before any capital. If shadow mode is consistent with backtest (returns within ~1 std error, costs as modeled) → deploy small.

---

## 10. Parameter Register (complete list — nothing else may be tuned)

| Parameter | Initial | Tuning grid (Part A only) |
|---|---|---|
| Decision frequency | Weekly | {daily, weekly} — diagnostic only, expect weekly to win after costs |
| EWMA half-life H | 8 wk | {4, 8, 13, 26} |
| Learning rate η | 1.0 | {0.5, 1.0, 2.0} |
| Top-K | 4 | fixed |
| Agreement count | 3 of 4 | fixed |
| Conviction θ | 0.25 | {0.15, 0.25, 0.35} |
| Vol filter decile | top 10% | fixed |
| Rebalance band | 15 pts | fixed |
| Indicator params | standard | **fixed — never tuned** |

Total tunable combinations: 2 × 4 × 3 × 3 = 72. Small enough that the walk-forward winner is meaningful; do not expand this grid.

---

## 11. Deliverables & Milestones

1. **M1 — Data layer:** adjusted OHLCV loader + integrity checks (gaps, splits), burn-in handling.
2. **M2 — Indicator library:** 6 signals + correlation matrix report on Part A.
3. **M3 — Engine:** weighting, consensus, costs, execution model; unit tests passing.
4. **M4 — Train-set study:** walk-forward tuning on Part A, robustness perturbation report, frozen config committed.
5. **M5 — Verification:** single run on Part B, metrics vs. acceptance criteria, go/no-go memo.
6. **M6 (if go):** shadow-mode paper trading harness, weekly automated report.

---

## 12. Known Risks / Honest Caveats

- Daily/weekly price-only technicals in liquid indices carry weak edge; the realistic win is drawdown control, not large alpha. Benchmark honestly.
- One holdout run is a single sample of one 5-year regime; passing Part B is necessary, not sufficient. Shadow mode is the real second verification.
- If Part B fails and you iterate, you no longer have clean out-of-sample data — plan research cycles accordingly.
- This document is an engineering spec, not investment advice.
