# Pre-registration — EWMAC and carry on the two-sleeve Indian universe

Written 2026-09-07, before any P&L has been computed on either sleeve. The
futures series this trial reads were built the same day (`internal/futures`)
and have never been backtested.

## 1. What is being tested, and why it is verification rather than selection

EWMAC (fast 16/32/64) and carry are inherited rules. They enter this project on
Carver's own out-of-sample evidence — 35 years, 40+ instruments — and their
parameters, including the forecast scalars, are his published ones, fitted
without reference to our data. LEDGER rows 1 and 2 have carried them as
"pending real data" since the ledger was created.

That makes this run a **verification, not a selection**: nothing is being
chosen here, nothing was searched over, and the four variations were fixed
before this file was written. Per the ledger's own note on rows 1-2, it
therefore declares `trials=0` and does not inflate M.

The bar is set accordingly, and stated in advance so it cannot be moved later:

- **Decision bar: monthly paired t > 2.0 against BOTH controls, on the
  holdout.** A conventional single-hypothesis bar, defensible only because no
  search happened.
- The workspace-wide Bonferroni bar (t = 4.08 at M = 1114) is reported
  alongside every result for context. If a rule clears 2.0 but not 4.08, the
  report says so and the ledger row says so.

Stories, unchanged from `internal/rules`: EWMAC is paid by disposition-effect
sellers, mandate-driven rebalancers and anchored traders who supply
non-forecast-driven flow; carry is paid by hedgers and constrained investors
shedding exposure they must not hold, and collects the premium for bearing the
skew they are offloading.

## 2. Universe — two sleeves, run separately

**Futures sleeve** (dynamic, leverage permitted by margin):
NIFTY, BANKNIFTY, GOLDM, SILVERM — the four underlyings whose curves survived
cleaning. CRUDEOILM is excluded: `internal/futures` refuses it (1202-day roll
spacing against a 31-day cycle, no reference series). Prices are the Panama
back-adjusted front series; opens carry the same adjustment as their closes.
Carry is the front-to-second basis annualized by the LISTED expiry gap.

**Cash/ETF sleeve** (long-only, no borrowing):
NIFTYBEES, JUNIORBEES, BANKBEES, GOLDBEES, MON100, LTGILTBEES, GILT5YBEES,
SILVERBEES. Forecasts clip to [0, +20]; gross leverage capped at 1.0. No carry
— an ETF has no second position to read a basis from, so the carry rule emits
NaN and is renormalized away.

Index spot (NIFTY, BANKNIFTY as IDX_I) is NOT traded: it is a reference series,
not an instrument.

## 3. Periods

- **Construction: 2015-01-01 → 2021-12-31.**
- **Holdout: 2022-01-01 → 2026-08-21** (the last date in the table). Burned
  once, under `carver-core-2022-01-to-2026-08`.

This data is untouched by every previous trial in the ledger: rows 3-15 are all
NSE cash equity. SILVERBEES only starts 2022-02, so it exists in the holdout
and not in construction; that is stated here rather than discovered later.

## 4. Execution, sizing and costs — fixed here

- Decide at the close of day t, fill at the open of t+1. Daily schedule.
- Volatility target 20% annualized, compounding on, capital Rs 2 crore.
  Rs 2 crore because a single futures contract is Rs 12-20 lakh of notional;
  the four-block test result per instrument is reported, and with it the
  minimum capital at which each instrument is tradable at all.
- IDM derived point-in-time from realized correlations (engine default);
  FDM 1.35 for the four-rule set; equal instrument weights; equal forecast
  weights across whichever rules are defined for that instrument.
- Costs, per side, pre-registered estimates:

  | Instrument | Point value | Spread (price points) | Fee/block | % of value |
  |---|---|---|---|---|
  | NIFTY fut | 75 | 1.0 | Rs 20 | 0.0002 |
  | BANKNIFTY fut | 35 | 3.0 | Rs 20 | 0.0002 |
  | GOLDM fut | 10 | 10.0 | Rs 20 | 0.0002 |
  | SILVERM fut | 5 | 150.0 | Rs 20 | 0.0002 |
  | ETFs | 1 | 0.05% of price | 0 | 0.0007 |

  Every headline number is reported again at double these costs.

## 5. Controls

Both run through the same engine, the same sizing, the same costs, the same
instruments and days — only the forecast differs:

- **C1, always-long.** A rule that emits +10 every day. This is "own the
  sleeve, vol-targeted, and never think": it strips out the fact that a trend
  system on a rising universe is long most of the time.
- **C2, time-shifted forecasts.** The real forecasts, circularly shifted along
  the time axis by a fixed offset, averaged over ten pre-registered offsets
  (251, 379, 503, 631, 757, 883, 1009, 1131, 1259, 1381 trading days). This
  preserves each rule's own distribution AND its autocorrelation — the same
  turnover, the same position sizes, the same cost bill — while destroying its
  alignment with prices. It is the time-series analogue of the cross-sectional
  shuffle, and it is drawn ONCE per offset rather than per day, which is the
  mistake LEDGER row 15 caught the hard way.

Primary statistic: paired monthly return difference (strategy minus control)
and its t-statistic, per sleeve, against each control.

## 6. Decision rule (binding)

- **Construction gate:** positive monthly mean difference with t > 1.5 against
  both controls, in at least one sleeve, before the holdout is opened. Lower
  than the equity trial's 2.0 because a 4-instrument sleeve has far less
  cross-sectional averaging and correspondingly noisier monthly differences.
- **Holdout bar:** t > 2.0 against both controls, as in section 1.
- **Trials: 0** (verification of a priori rules; see section 1).
- If EWMAC fails here, the honest conclusion is about THIS universe — four
  Indian futures and eight ETFs over eleven years — and not about trend
  following, whose evidence base is far wider than anything this data can
  overturn. That asymmetry is stated in advance so a negative result is not
  over-read.

---

## 7. Outcome (appended 2026-09-07, after the construction run — design above unchanged)

**Both sleeves failed the section 6 construction gate. The holdout was never
read and is not burned.** LEDGER row 16 has the numbers.

Three things this run taught that the design did not anticipate:

1. **Carry's story does not hold on equity index futures.** The NIFTY and
   BANKNIFTY basis is a financing cost — the interest on carrying the index —
   not hedgers paying to shed exposure they must not hold. The rule read that
   cost as a signal and stayed structurally short through a bull market:
   −6.23%/yr, maxDD 73.9%. This is a Law-1 failure. Carry belongs where the
   curve is shaped by storage or hedging pressure, and a future test of it
   should say so up front rather than including index futures by default.
2. **The engine had a bug that this trial found by being the first portfolio
   not to set instrument weights.** The daily loop read the unnormalized
   `cfg.InstrumentWeights` map instead of the normalized weights, so an unset
   map gave every instrument a weight of zero: a clean run, no error, a flat
   equity curve, 0.00% for every metric. Fixed with a regression test. A
   backtest that silently trades nothing is the most dangerous kind of green
   result, and nothing in the report would have flagged it — the controls were
   flat too.
3. **The always-long control was worth more than the shuffle control here.**
   On the ETF sleeve the time-shifted forecasts scored within 0.03%/month of
   the real system, which is informative but undramatic; the always-long
   control is what showed the system trailing the thing it was trading. Both
   were needed. Registering only one would have left the result ambiguous.
