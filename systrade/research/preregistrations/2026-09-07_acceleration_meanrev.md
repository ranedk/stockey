# Pre-registration — acceleration and long-horizon mean reversion, NSE cash equities

Written 2026-09-07, BEFORE any return, P&L or hit rate has been computed for
these rules on any period. LEDGER row 14 (the correlation study that produced
the shortlist) consulted no performance data, so nothing about this trial's
outcome is known yet.

Amendments to this file after the first run of section 6 are forbidden; a
changed design is a NEW pre-registration and a new ledger row.

## 1. Hypotheses (Law 1 — who pays, and why they keep paying)

**H1 — Acceleration (`accel16`, `accel32`, `accel64`).** Herding builds
gradually: the flow that creates a trend arrives in a queue, as slower
investors act on the same information at different times (allocators on
quarterly cycles, retail following the news cycle). Absorbing that flow is
worth something while the queue is still forming and nothing once it has
emptied. We therefore bet on the RATE OF CHANGE of the move rather than the
move, and expect to be paid for entering while the queue is still arriving.

**H2 — Mean reversion at a 5-year window (`meanrev1280`).** Investors
extrapolate multi-year returns: money chases what has already compounded and
abandons what has already fallen (De Bondt-Thaler), and career risk keeps
institutions in that same queue because owning the fallen name is the one that
gets you fired. We are paid for taking the unglamorous side and waiting years —
a horizon most capital cannot commit to.

Both stories were written before the code (`internal/rules`), not after seeing
any result.

## 2. What is NOT being tested, and why

`breakout40/80/160/320` and `meanrev512` are implemented and will NOT be
tested. LEDGER row 14 measured them as duplicates of rules we already run
(breakout vs EWMAC rho 0.84-0.94; meanrev512 vs ewmac64_256 rho -0.95), and
the admission protocol's rho > 0.7 bar rejects a duplicate before it is ever
scored. Testing them anyway would spend sample and inflate M to re-answer a
question already answered on cheaper evidence.

## 3. Universe and data

- NSE cash equities, corporate-action adjusted, from the local bar cache
  (`internal/bars`, built once from `advisory_adjusted_ohlcv_daily`).
- Eligibility, evaluated point-in-time at the decision close: 60-bar median
  turnover >= Rs 1cr/day. No survivorship filter of any kind — delisted and
  dead symbols stay in the cache and drop out of eligibility naturally.
- Long-only. Forecasts clip to [0, +20] (Indian cash equity cannot be shorted
  overnight), which is the sleeve these rules would actually trade.

## 4. Periods — declared before the first run

- **Construction: 2013-07-01 → 2021-12-31.** Everything in section 6 is
  developed, debugged and read here.
- **Holdout: 2022-01-01 → 2026-06-30.** Untouched until the construction
  config is frozen. Run ONCE, then burned in `research/holdout_burns.json`
  under `accel-meanrev-equity-2022-01-to-2026-06`. A second look does not
  exist: if the holdout says no, the answer is no.

Note on prior consumption: the COI family (rows 11-13) scanned this same
2013-2026 sample. That work was a candle-pattern study whose signals share no
construction with a vol-normalized trend derivative, but the honest statement
is that this data is not virgin, and the holdout result should be read as one
notch weaker than a first-ever look would be.

## 5. Execution and cost model (fixed here, not tunable later)

- Decide at the close of day t using only data through t; fill at the OPEN of
  t+1. Same convention as `internal/backtest`.
- Position weight per eligible symbol proportional to `forecast_i / vol_i`,
  the whole book normalized so gross exposure is 1.0 every day. No leverage,
  no vol targeting at the portfolio level — this trial asks whether the SIGNAL
  is worth anything, not how to size it.
- Costs: 50 bps round trip on traded value, charged on turnover, matching the
  COI study so the two are comparable. Every headline number is reported again
  at 100 bps (Law: a strategy that dies at 2x costs is a broker donation).

## 6. Controls and the statistic

Two matched controls, both run on exactly the same eligible universe, the same
days, the same gross exposure and the same cost model:

- **C1, beta control** — equal weight across that day's eligible symbols. This
  is "just be long the market". Row 14 measured mean raw EWMAC at +0.43 to
  +1.41 on this universe, i.e. trend forecasts here carry a standing long, and
  LEDGER row 4 already caught that beta masquerading as selection alpha.
- **C2, cross-sectional shuffle** — the SAME forecast values that day, randomly
  reassigned among that day's eligible symbols, averaged over 10 seeds. Same
  exposure profile, same concentration, no information about WHICH stock.
  Fixed seeds 1..10, declared here.

Primary statistic: the paired monthly return difference (rule minus control),
its mean, and its t-statistic across months, reported separately against C1
and C2. Secondary: Sharpe, skew, max drawdown, turnover, cost drag, and every
headline at 2x costs.

## 7. Decision rule (binding)

- **Construction gate:** to be taken to the holdout at all, a rule must show a
  positive monthly mean difference with t > 2.0 against BOTH controls. This
  gate is deliberately weaker than the admission bar — it exists only to stop
  a dead rule from spending the holdout.
- **Admission bar:** the holdout must clear the Bonferroni bar implied by the
  ledger's M. `research.CountM` reads **M = 1110** today, giving **t = 4.08**.
  That is a high bar and it is meant to be: this workspace has run over a
  thousand trials, and a rule that cannot clear it does not get capital.
- **Trials declared: 4** (accel16, accel32, accel64, meanrev1280). No further
  variation, parameter tweak or exit rule may be added to this row. Anything
  else is a new hypothesis, a new row, and a new holdout.
- Either outcome is logged. A rejection is the expected result and is worth
  exactly as much as an admission.

---

## 8. Outcome (appended 2026-09-07, after the construction run — design above unchanged)

**All four rules failed the section 7 construction gate. The holdout was never
read and is not burned.** LEDGER row 15 has the numbers.

Two things this file got wrong, recorded here so the next pre-registration
does not repeat them:

1. **Control C2 was a straw man.** Re-shuffling the weights every day gives the
   control ~160%/day turnover and about −55%/yr of pure cost bleed. Every rule
   "beat" it by 5-6% a month at t up to +22. If the beta control C1 had not
   also been registered, this design would have sent four worthless rules to
   the holdout with triumphant statistics. A permutation control must be drawn
   ONCE per symbol, not once per day, so that it holds positions like a real
   book. (Implemented after the run as C3 in `internal/sleeve`, diagnostics
   only — it decided nothing here.)
2. **The gross/net split should have been registered from the start.** Whether
   a rule fails on selection or on costs is the first question anyone asks
   about a negative result, and it costs nothing to compute. It was added
   post-hoc as a diagnostic; next time it is part of the design.

What the run does NOT license: any claim that acceleration or long-horizon mean
reversion cannot work anywhere. It was tested here as a long-only
cross-sectional stock selector on NSE cash equity, which is the sleeve these
rules would trade today. A time-series application on the futures/ETF sleeve is
a different hypothesis, on different instruments, and needs its own
pre-registration and its own holdout.
