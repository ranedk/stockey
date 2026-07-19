# Strategy Test Backlog — evidence-grounded, knob-by-knob

**Status:** research planning doc (2026-07-19). Report/research-only; nothing here has portfolio or broker
authority. Every candidate here must graduate through the existing discipline
(`advisory/factor_ic_sweep.py` → `advisory/factor_graduation.py`): walk-forward + regime split + FDR +
matured labels, bounded/reversible, proposal-only until an operator opts in.

## Why this list looks the way it does

Two independent bodies of evidence agree:

1. **This system's own research (2026-07):** no robust cross-regime *selection* alpha across four data
   families (see `todo.md` Current State + memory `factor-research-no-selection-alpha`). The measured edge
   is **not-losing** — RS-top-quintile + a trend/breadth deployment floor cut drawdown ~2-3× with return
   preserved (`deployment-timing`, `adaptive-ensemble-backtest`). More features/fitting *hurt* on every axis.
2. **The external literature:** only a handful of factors survive out-of-sample and after costs —
   **momentum, low-volatility, weakly value**; quality/QMJ, small-cap, and most "factor zoo" entries do not,
   and *timing/vol-managing individual factors is unprofitable after costs except at the market level*
   ([factor-zoo review](https://link.springer.com/article/10.1007/s11573-021-01035-y)). The biggest
   documented gains come not from better selection but from **managing momentum's own volatility**
   (Barroso–Santa-Clara constant-vol scaling ~doubled Sharpe and killed crashes) and **cutting turnover**.
   In India, momentum survives costs **only at longer horizons**: high-turnover momentum returned
   **8.5% CAGR (below Nifty) vs 19.4% for low-turnover**
   ([backtestindia](https://backtestindia.com/blog/momentum-factor-india-liquidity-premium-scaled-turnover),
   [repec](https://ideas.repec.org/a/eme/jamrpp/jamr-11-2015-0081.html)).

Net: the durable levers are **low-turnover momentum + volatility management (not-losing) + low-vol
selection**. That is our thesis, externally validated.

## The knob principle (what can actually work here)

Our hard constraint is ~13 independent 20-day blocks → we **cannot fit high-degree-of-freedom knobs**
(fine entry thresholds, per-regime weights) without overfitting. So the only knobs that can work are
**low-DoF, structurally-motivated ones with strong external priors** — rebalance frequency, target-vol
level, lookback horizon, residualization choice, low-vol universe. **Adopt those from the literature; use
our data only to VALIDATE (walk-forward + FDR), never to optimize.**

## Tier 1 — highest evidence, directly fixes what we're doing wrong

| Test | Evidence | Knobs | Can the knob work here? |
|---|---|---|---|
| **Cut rebalance frequency / longer holding** | India momentum net-positive only >6mo; low-turnover 19.4% vs high-turnover 8.5% CAGR | rebalance {monthly, quarterly, **semi-annual**}; min-holding | **YES — highest value, 1 low-DoF knob.** Our ~20-day book is the high-turnover trap. Adopt semi-annual, validate. |
| **Constant-volatility-scaled book** (Barroso–Santa-Clara) | scale by inverse 6-mo realized vol, target ~12% → Sharpe 0.53→0.97, crashes eliminated ([alphaarchitect](https://alphaarchitect.com/risk-of-momentum-crashes/)) | target vol {10,12,15%}; vol lookback {3,6mo} | **YES.** We already have vol-target sizing + crash floor; this formalizes them. 2 low-DoF knobs. |
| **Vol-adjusted momentum score** (Nifty200-Momentum30 method) | rank by (6mo+12mo return)/vol; live investable Indian index ([niftyindices](https://www.niftyindices.com/Factsheet/Factsheet_Nifty200_Momentum30.pdf)) | lookback blend (6+12mo); vol-normalize on/off | **YES — adopt, don't fit.** Replaces raw RS with the proven scored version. |

## Tier 2 — strong evidence, more build

| Test | Evidence | Knobs | Can the knob work here? |
|---|---|---|---|
| **Residual / idiosyncratic momentum** (Blitz et al.) | rank by *residual* return (strip market/sector beta) → half the vol, shallower drawdowns ([quantpedia](https://quantpedia.com/strategies/residual-momentum-factor), [Robeco](https://www.robeco.com/en-int/insights/2023/02/quant-chart-taming-momentum-crashes)) | residual model {market, market+sector}; lookback | **Likely.** The *proper* version of the sector-neutral RS we tested crudely and which failed as naive demeaning. Structural choice. Medium build (PIT regressions). |
| **Low-volatility selection** | top-100 low-vol NSE 12.4% vs Nifty 10.4% CAGR, lower vol, 18yr ([backtestindia](https://backtestindia.com/blog/low-volatility-anomaly-india-nse-backtest)) | vol lookback {1yr}; universe {top-100/200}; rebalance | **YES — untested by us, orthogonal to momentum.** Low-DoF, long track record. |
| **Absolute-strength / trend overlay on momentum** | "relative + absolute strength" beats price momentum in India; TSMOM return comes from vol-scaling not prediction ([Stern TSMOM](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)) | filter: index>200DMA / name>200DMA / 12mo TSMOM sign | **YES — we already have the breadth/trend floor.** Our deployment edge, lit-confirmed. |

## Tier 3 / Skip (evidence says don't bother)

- **Quality / QMJ:** *negative* in India post-COVID (mean −0.002) ([sciencedirect](https://www.sciencedirect.com/science/article/pii/S3050700625000416)) → skip.
- **Short-horizon (<6mo) momentum, high-frequency rebalancing:** dies after costs → skip.
- **Factor timing / vol-managing individual factors:** unprofitable after costs except the market → don't build.
- **Small/micro-cap-dependent factors, seasonality:** illiquid / too few independent observations on ~5yr.

## Tier-1 ablation — RESULT (2026-07, `advisory/momentum_lab.py`)

Ran the combined test on dhan `EQUITY` (split-adjusted; ~494 liquid names/day), holdout 2022-06..2026-07 —
which turned out to be **one strong momentum-bull regime with no momentum crash**. After costs:

| config | CAGR | Sharpe | maxDD |
|---|---|---|---|
| EW universe buy&hold (market proxy) | 34.4% | **1.38** | −23.5% |
| A — current: raw RS, monthly | 40.1% | 1.20 | −32.8% |
| B — **+ vol-adjusted score** | 37.9% | **1.23** | −29.6% |
| C — + semi-annual rebalance | 29.6% | 0.98 | −30.8% |
| D — + constant-12%-vol scale (full) | 17.1% | 0.83 | **−15.8%** |

Rebalance × cost sensitivity (vol-adj score): **weekly won at every cost level** (45/90/150bps → 53.7/46.1/
36.5% CAGR) vs semi-annual (29.6/28.4/26.8%).

**What we learned about the knobs:**
- **Vol-adjusted score — KEEP.** The one robust, cost-free win (Sharpe 1.23 vs 1.20, shallower maxDD). Route
  it through `factor_graduation`.
- **Rebalance frequency — UNSETTLED, prior OVERTURNED here.** The India "low-turnover wins" prior did NOT
  hold: on a *liquid* universe in a *momentum bull*, weekly beat semi-annual even at 150bps. But this is one
  bull regime with no crash (the scenario that punishes high turnover never occurred) — do not commit it.
- **Constant-vol scaling — crash insurance only.** Cut maxDD −33%→−16% but gave up more than half the return
  in this bull. Deploy via the existing breadth/deployment floor, not as an always-on scaler.
- **The market did the work:** nothing beat the EW-universe buy&hold on Sharpe (1.38) — consistent with "no
  easy selection alpha."

This is the meta-principle in action: we *adopted* the literature priors and *validated* — and validation
**contradicted** the low-turnover and vol-scaling priors, because our regime/universe differs from the
papers' (liquid names, single bull, no crash). Adopt the score; keep the rest monitored, not committed.

## Sources
- Factor zoo / what survives after costs: https://link.springer.com/article/10.1007/s11573-021-01035-y
- India momentum after costs: https://ideas.repec.org/a/eme/jamrpp/jamr-11-2015-0081.html ; https://www.sciencedirect.com/science/article/abs/pii/S0927538X23002640
- India momentum turnover: https://backtestindia.com/blog/momentum-factor-india-liquidity-premium-scaled-turnover
- Nifty momentum methodology: https://www.niftyindices.com/Factsheet/Factsheet_Nifty200_Momentum30.pdf
- Time-series momentum / vol-scaling: https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf
- Barroso–Santa-Clara / Daniel–Moskowitz momentum crashes: https://alphaarchitect.com/risk-of-momentum-crashes/
- Residual / idiosyncratic momentum: https://quantpedia.com/strategies/residual-momentum-factor ; https://www.robeco.com/en-int/insights/2023/02/quant-chart-taming-momentum-crashes
- Low-vol anomaly India: https://backtestindia.com/blog/low-volatility-anomaly-india-nse-backtest
- Quality/QMJ India (negative): https://www.sciencedirect.com/science/article/pii/S3050700625000416
