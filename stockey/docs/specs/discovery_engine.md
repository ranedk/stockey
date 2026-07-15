# Spec: The Discovery Engine — adaptive tracking, not convergent optimization

Status: design of record (operator + engineering, 2026-07-13)
Companion to: `docs/specs/actor_critic_closed_loop.md` (this doc is the governing philosophy
and the concrete near-term design; the actor/critic spec is the broader mechanism).
Grounded in: the momentum episode (2026-07-12/13) where a confident reasoned prior was
*backwards* and a striking single-sample finding was *fragile* — both caught by the walk-forward +
multi-definition robustness harness.

> **Read §8 first.** An adversarial review (2026-07-13) found that the build queue as originally
> written points the strongest machinery (self-tuning entry weights) at the one target the data
> cannot yet reward, while the things that actually decide personal P&L — realistic costs on the
> illiquid names we surface, portfolio-level risk, and the honest comparison to just owning NIFTY —
> were the quietest parts of the spec. §8 records those findings and the resulting re-sequencing.
> They are binding.

---

## 0. The one idea

We do not encode beliefs (momentum works, breakouts work) as hardcoded arithmetic. We build a
machine that **finds** beliefs from our own data and then **tries to kill them** (walk-forward,
multi-definition robustness, false-discovery control). The discovery is not the moat — the
**robustness machinery is the moat**. Without it, "learn the trick from data" is overfitting
with extra steps; with it, it is science.

---

## 1. Governing philosophy — three caveats that reshape everything

These are binding constraints, not preferences. They turn the system from an *optimizer* into a
*tracker*.

1. **Data is fixed and small (~13 months; no more within the ROI timeline).** No method that
   needs to *converge to the true optimum* is viable — 13 months barely supports 3 walk-forward
   windows. We do not chase the optimum; we **track a moving target with a simple rule.**
2. **High dimensionality may never converge → seed with documented, intuition-based rules, and
   let the daily corrective run nudge them.** The goal is not "solve for the right weights"; it is
   **keep the weights closest to *current* conditions.** Never solved, always current. Control
   theory / online tracking, not batch optimization.
3. **Take market truths as ~90% priors; do not re-derive them; let the portfolio absorb the ~10%
   leaks.** We do NOT spend the engine proving "momentum exists" or "trends persist." We point it
   only where *our specific data* can inform — the *weights, thresholds, and timing for our
   universe* — and size positions so no single leak is catastrophic.

Net philosophy: **simple intuition-seeded rules + slow online correction + risk-budgeted leaks.**
More honest, and more robust with little data, than a grand auto-tuner.

---

## 2. The unifying insight — the regime signal is *discovered*, not imposed

The explicit regime layer (risk_on/neutral/risk_off admission, deployment timing) is fragile —
proven by the walk-forward backtest (the "buy strength when weak" edge held only under one regime
definition and vanished under trend-direction). It is also partly redundant: the screeners already
breathe with the market (risk-on → they surface more; risk-off → fewer).

**Replacement:** the scoring model's own predictive health *is* the regime signal. When the model's
information coefficient (IC — the rank correlation of score vs realized forward return) **collapses**,
high scores have stopped predicting winners — *that is the regime change*, discovered from our own
data instead of imposed by a fragile external definition. So regime does not disappear; it moves from
a fragile top-of-funnel gate to an **emergent property of the model's health.**

What we keep of "regime": a coarse **risk-off safety floor** (do not deploy full risk into an actual
crash) — a capital-protection rule, allowed to be a simple documented rule, not an alpha bet.

---

## 3. The scoring model — the heart, and the first build

> **Measured 2026-07-14 (`advisory/subscore_ic.py`, T1 descriptive half).** The belief below is now
> tested: on 13 months / 1,708 liquid names, NO sub-score has robust cross-regime selection alpha.
> Every positive-IC component (trend, structure_momentum, rs, structure_base) is favorable-regime-only
> (IC alive when NIFTY>50DMA, ~0 or inverted in a weak tape) → `benchmark_beta_not_alpha`; the scorer's
> edge is rising-tape beta, not all-weather selection — an independent IC-side confirmation of §9/§10
> (the cycle dominates the archetype). `participation_base` is robustly INVERTED (`do_not_relax`), and
> the engine `rs` sub-score is NOT the `rs_percentile` that actually works (§9). This is why the loop
> below stays DESCRIPTIVE: reweighting on ~13 blocks (§8.1) would fit rising-tape beta. See todo.md T1.

Today the sub-scores (trend / structure / participation / RS / tradability), the 78 buy bar, and the
sub-score minimums are all **hand-set arithmetic** — a belief about what predicts, never measured.

**Metric: rank-IC, not price-within-5%.** Stock return *levels* are ~all noise; no model predicts
them to 5%, so an absolute-error target would thrash forever. What is achievable and what matters is
*ranking*: do high-scoring names outperform low-scoring ones? Even an IC of 0.03–0.05 is real money
across many names. The loop keeps the weights **maximizing IC on recent data.**

**Pool to learn, split to monitor.** Per-stock weight fitting will never converge (too few points per
name; some trend, some do not). So:
- **Learn the weights on the pooled cross-section** (all stocks together, per archetype) — thousands
  of observations, stable, uses all the data.
- **Per-stock residual** (how far a name's realized return is from its feature-prediction) is a
  *secondary anomaly flag* — a large residual = "not behaving as its features say" (an event, or a
  name to distrust), never a place to fit weights.
- **Aggregate IC / error** is the regime detector (§2).

**Slow online update, not daily thrash.** Weights nudge slowly (exponential decay / small bounded
steps) so one good week does not swing the book. The portfolio only shifts when the **IC-drift alarm**
actually fires.

**Pays triple.** This single build (a) reweights scoring from evidence, (b) hands us the **buy-bar
knee** for free (once you have a predictive score, the threshold is where forward expectancy peaks —
per archetype), and (c) its IC-drift gives the **discovered regime signal** that retires the fragile one.

---

## 4. Exits — treat exit like entry (scored reversal), with a risk floor underneath

Exits are the unmeasured half of the P&L. The frame is **exit-as-a-scored-decision, symmetric to
entry**: a distinct set of *reversal* features (distribution-day clusters, break of the rising MA, RS
deterioration, momentum divergence, up-day volume dry-up), validated by the *same* harness (do these
predict a forward drawdown?), and you exit when the reversal score crosses a **discovered** threshold —
giving holding-horizon and exit-timing from data, not a guess.

Literature sits **underneath** as the disaster floor, not the primary exit:
- **ATR / chandelier trailing stop** — catches a gap-down the daily reversal score would miss; but it
  whipsaws in choppy markets (well documented), so it is the floor, not the driver.
- **Time cap** — momentum decays; a max-holding prevents dead money.
- Recurring literature finding: exits matter more for *downside protection* than upside capture, and
  rigid trailing stops sacrifice upside — which argues for the scored/signal exit as primary.

Design: **scored reversal decision (primary) + ATR/gap disaster-stop (floor) + time cap.** Needs a short
literature-informed design pass before build.

---

## 5. Deliberately NOT built (operator decisions, 2026-07-13)

- **Source-lane weighting — dropped.** The lanes (screeners, breakout, surge, momentum, hypotheses,
  themes) are junk-eliminating *filters*, not weighted contributors; weighting them is a category
  error, and marginal-value measurement is genuinely high-dimensional. Keep them as pipelines; keep
  the regret ledger + missed-movers as the *directional* health check (prune a losing lane manually).
- **Catalyst standalone predictor — not built.** ORDER_WIN / RESULTS_POSITIVE are taken as a ~90%
  prior (operator has traded them profitably in bull tapes); they act as a **conditional conviction /
  size enhancer when the technical pipeline also fires** in a favorable tape, never a standalone
  trigger. Matches the existing "enhance or veto, never gate by absence" principle.
- **Fine-grained regime conditioning — demoted.** Fragile; replaced by the discovered IC-drift signal
  (§2) plus a coarse risk-off safety floor.

---

## 6. The daily discovery run + trust gate

- **Daily run:** re-fit the IC-optimal scoring weights on recent data; report what moved, whether it
  is robust (walk-forward + multi-definition), and the IC-drift state — a human-legible "what can we
  do better" report.
- **Trust gate (a knob moves only after ALL of):** survives walk-forward, robust across definitions,
  enough matured labels, and **false-discovery control** (a daily scan of many knobs manufactures
  false winners unless corrected — this is THE danger of an autonomous daily tuner and must be
  first-class), then a small bounded reversible step, monitored for degradation with auto-revert.
- Slow adaptation, floors/caps, reversibility throughout.

---

## 7. Build queue (priority)

Re-sequenced 2026-07-13 after the §8 review. The heart (sub-score IC) is still built early, but it
is **split into a descriptive half (build) and a prescriptive half (defer)**, and a cheap
invalidation gate runs *before* any of it.

0. **Invalidation gate — costs, capacity, survivorship, and the north-star** (§8.4) — cheap, and it
   can kill or reshape everything downstream. A name-specific cost/liquidity model (spread +
   size-scaled impact, not a flat 25bps), a survivorship-honest backtest (drops of picks that
   delist mid-window handled explicitly), and one north-star scorecard: **net portfolio return vs
   buy-and-hold NIFTY, after realistic costs.** RUN FIRST.
1. **Sub-score IC *validation*** (§3, descriptive half) — measure and rank each sub-score's rank-IC
   on the pooled cross-section; report the buy-bar knee. This is well-powered cross-sectionally and
   is the genuine payoff. BUILD. **The prescriptive half — auto-reweighting live scoring — is
   deferred** (§8.1): at most one human-reviewed reweighting off the IC ranking, no daily auto-tuner.
2. **Portfolio & risk layer** (§8.3) — position sizing, correlation-aware construction, a drawdown
   floor. For a personal account this dominates returns more than another decimal of entry score,
   and the spec currently only *asserts* "the portfolio absorbs the leaks" with no risk math.
3. **Exit-as-scored-decision** (§4) — symmetric harness + volatility floor + time cap; short
   literature-informed design pass first.
4. **Daily discovery run + trust gate** (§6) — DEFERRED until §8.1's ~13-independent-period problem
   eases (more data) or T1's descriptive pass proves the signal-to-noise supports self-tuning. A
   daily multi-knob auto-tuner on ~13 independent time blocks manufactures false winners faster than
   false-discovery control can catch them. IC-drift ships here only as a **warning light**, never a
   portfolio driver (§8.2).

Everything else: intuition-seeded rules + monitoring (§5).

---

## 8. Critical review — where the data can and cannot reward this (binding, 2026-07-13)

An adversarial second read, grounded in queries against our own `nseindia_ohlcv` (275 EQ trading
days, 2025-06-02 → 2026-07-10). What survived the check is recorded here as binding constraints on
the build queue. The discipline (validate OOS, widen before building, don't hardcode) is the crown
jewel and stays — but the same skepticism now points at the discovery program itself.

### 8.1 Cross-sectional breadth is NOT time-series power — the load-bearing correction

"Pool to learn" (§3) gives thousands of observations **cross-sectionally** (≈2,700 names ranked per
day), so the *descriptive* question — *does sub-score X predict forward return?* — is well-powered.
But the *prescriptive* act T1 originally wanted — *change the weights and claim next-period
improvement* — depends on the number of **independent time periods**, and 275 days ÷ 20-day forward
windows ≈ **13 independent blocks** (~6 per regime side). Cross-sectional breadth does not buy
time-series power, and it is the time-series that decides whether a reweighting is signal or luck.

Consequence: the momentum episode (a re-fit edge that proved fragile) is not a one-off — with ~13
independent blocks it is the *expected* outcome of any prescriptive re-fit. So:
- **Build the descriptive half** (measure/rank sub-score ICs, report the buy-bar knee).
- **Defer the prescriptive half.** No daily auto-reweighter. At most one human-reviewed reweighting
  off the IC ranking, then leave it. When we do adapt, the step must be tiny and the success test
  explicit: **the adaptive weights must beat frozen hand-set weights out-of-sample, or adaptation is
  switched off.** Elegance without a kill-switch is how these systems quietly bleed.

Also fold in, when the IC harness lands: overlapping forward windows autocorrelate, so naive counts
and sign-consistency **overstate** significance. Report IC confidence intervals from a
**block-bootstrap** (or Newey-West), not point estimates, and shrink hard.

### 8.2 IC-drift = regime is elegant but is another fragile timing bet — demote to a warning light

§2 replaces the fragile imposed-regime gate with "IC-collapse *is* the regime change, discovered
from our data." But *discovered-from-13-months is still 13-months-fragile*. A single 20-day IC
reading has large sampling error; smoothing it to call a "collapse" adds lag, so it confirms after
the move — the exact regime-timing trap we just escaped. And acting on it (de-risk on IC drop) is
itself a timing bet on the same data that made the last one fragile. The word "discovered" was doing
rhetorical work the data can't back.

Binding: IC-drift ships as a **diagnostic alert** ("model predictive power dropped — look"), never
an auto-driver of portfolio de-risking, until multiple regime cycles exist (we do not have them).
The coarse risk-off safety floor (§2) remains the only allowed regime action, as a documented
capital-protection rule, not an alpha bet.

### 8.3 The missing layer that actually dominates personal P&L — portfolio & risk

The spec asserts "size positions so the ~10% leaks are absorbed as portfolio risk" but carries **no
risk math**: no correlation structure across held names, no strategy-level drawdown estimate, no
sizing model. For a personal account the risk/portfolio layer decides returns more than squeezing
the entry score from 73 → 78. This is now T2 in the queue (ahead of exits and the discovery loop).

### 8.4 Existential checks that can invalidate the coverage thesis — run before more building (T0)

Grounded against our data:
- **Costs/liquidity — real, and the flat 25bps is optimistic.** The momentum picks have median
  avg-daily turnover **₹41.5cr** and a 25th-percentile **₹19cr**. Tradeable at small personal size,
  but on Indian mid/small-caps the bid-ask spread alone often runs 20-50bps, so 25bps flatters the
  backtest — and the measured edge (~2-3% / 10d) is thin enough that realistic costs eat a third to
  half of it. **Re-run every edge number under a name-specific cost model** (spread + size-scaled
  impact) before trusting it.
- **Survivorship — checked, better than feared.** The history contains **171 of 2,166** names that
  traded in mid-2025 and then disappeared, so it is *not* survivor-only. Residual bias is only the
  backtest silently dropping picks that delist mid-forward-window — second-order, but **handle it
  explicitly** (count and label such drops) rather than letting them vanish.
- **The north-star that's missing entirely.** For "serious personal ROI" the one honest scorecard is
  **net portfolio return vs buy-and-hold NIFTY, after realistic costs and taxes** — not "is the IC
  positive." Every sub-score, edge, and archetype is a means to that end. Stand it up as the top-line
  metric now, and let it be allowed to say "a simpler, diversified, low-turnover approach wins," if
  that is what the data says.

**T0 built (2026-07-13) and the north-star already spoke.** `advisory/cost_model.py` (name-specific:
statutory + turnover-spread + size-impact — median pick ~45bps, not the old flat 25),
`archetype_backtest.py` now counts matured-but-gone picks instead of silently dropping them (324 in
the full run), and `advisory/north_star.py` scores the paper portfolio. Measured: the raw
momentum+breakout lane, equal-weight, returned **−9.24% net of cost over 13 months vs NIFTY −3.12%
— trailing the index by ~6pts** (one −10% window dominates; 13 rebalances = wide error bars, §8.1).
This is exactly the §8.5 outcome the review told us to budget for, and it is measured on the
*unfiltered lane population* — the honest next step is to re-score on the gated recommendation set
before concluding anything about the live system, but the raw result is a strong caution against
leaning coverage/momentum harder without a cost-surviving, index-relative reason.

**The gated north-star (`north_star --source gated`) then said two things.** (1) *Structural, high
confidence:* the strict gated BUY set is **empty** — zero PASS_NOW+entry-confirmed candidates, zero
BUY action codes, zero broker-executable rows across the whole recorded funnel history. The live
system has recommended no buys, so there is literally nothing it *would trade* to score. This is the
§8.1/§8.5 "the learner has almost nothing to learn from" concern, now a hard fact, and it is the
single most important thing to internalize before building T1/T4 on "outcomes." (2) *Proxy, low
confidence but encouraging:* scoring the loosest proxy — PASS_NOW flags — appeared to **beat NIFTY**
(+0.6/+1.3/+4.3% excess). **CORRECTED 2026-07-13 (§12 correctness net):** that pass was POLLUTED — 47%
of PASS_NOW rows are `research_only` RESEARCH_TRAINING label-harvesting rows (a low 0.58 pass_now bar),
not recommendations. `north_star` now excludes them; the honest recommendation set (n=186) is
**5d −0.94%, 10d −0.35%, 20d +1.06% excess (day-level t −0.48/0.45/0.68)** — the "gate beats NIFTY"
finding **evaporates**; it was a research-row artifact. Honest read: the gated recommendation set is
~neutral vs NIFTY on this one ~2.5-month regime. (This correction is the correctness net paying for
itself: the audit flagged the pollution, and fixing it overturned a previously-"encouraging" result.)

### 8.5 The strategic risk, stated plainly

Enormous high-quality effort has gone into the *discovery machinery*, and the one concrete edge it
found was fragile. With 13 months of one market's data the correct **prior is that most edges we
"discover" will not survive more data or real costs** — as momentum didn't. Budget explicitly for
the outcome that there is no robust, cost-surviving systematic edge findable here yet, and that the
highest-ROI path is a simpler cost-aware strategy with the discovery engine running quietly as
*research* that earns a small tilt only once it has years — not months — of evidence.

---

## 9. Winner-backward: what we missed, and what survived the kill-test (2026-07-13)

Prompted by the operator: stop mining the funnel's own output (it emits no BUYs — §8's empty gated
set — so it is circular), and measure against **reality** — the stocks that actually moved.

**Ground truth.** Over the 13 months, among liquid names (>=5cr/day at entry), 197 ran up >=50%, 57
doubled, 16 tripled — clustered in a silver/precious-metals theme and an India-capex/defense/
electronics-manufacturing cluster, mostly sustained trends (with a visible hindsight-peak trap: some
spiked and gave it all back).

**Winner-backward feature study (point-in-time state -> actual forward 60d move).** By *absolute*
expected return, the states the admission layer targets were the worst: at-52w-high mean -1.4%, hot
momentum (+30% already) mean -3.2% with a 16.5% blow-up rate, clean uptrend 0.0%. The states the
funnel *excludes by construction* were the best: deep-below-52w-high mean +9.7%, and beaten-down +
RS-turning mean +12.4% with the lowest blow-up rate (4.1%). Survivorship-stress-tested (delisted
filled at -60%): +9.7% -> +7.8%, edge survives. This looked like a philosophical error in the entry
philosophy: buy weakness-that-turns, not strength-at-highs.

**Then the kill-test (T0.5) ran — benchmark-EXCESS, walk-forward, regime split — and it did NOT
survive.**
- Reversal (beaten-down + RS-turning) 40d **excess** walk-forward: W1 -6.2%, W2 +10.5%, W3 +2.0% =
  **INCONSISTENT** (one window carries it). Regime attribution self-contradicts (edge sits in a
  mostly-*rising* window yet the regime split credits *weak*-day decisions); with ~13 independent
  blocks (§8.1) window and regime cannot be disentangled. **Verdict: NOT promoted** — no reversal
  lane built. It was the momentum prior inverted; the harness caught it.
- Foil, breakout-at-high, same harness: 40d **excess** W1 +2.0% / W2 +6.2% / W3 +3.7% = CONSISTENT,
  and holds in both regimes (fav +3.6% / unfav +3.9%). The more robust *excess* signal — despite its
  poor *absolute* return.

**The reconciliation is the real finding.** Absolute and excess diverge because states cluster at
different points in the market cycle: beaten-down names sit near bottoms (absolute win = recovery
BETA, not repeatable alpha — one episode here), breakouts sit near tops (absolute loss, but
*defensive*: they fall less than the index -> consistent positive excess). So the funnel is not
fishing in an empty pond — it selects defensive relative-strength names, and their weak absolute
payoff is a **market-timing** problem, not a stock-selection one.

**Implication (feeds the deferred #2 rethink, `[T0.75]`).** The dominant variable is the market
cycle, not the archetype: the same state flips from gift to trap with the cycle. This is in direct
tension with §2/§5, which *demoted* fine-grained regime conditioning as fragile. #2 must resolve
that tension with evidence, not a third over-correction. Standing rule reaffirmed: judge signals on
benchmark-excess (do not bank market/recovery beta as alpha), and kill-test every "discovered" edge
on walk-forward + regime split before it touches the funnel.

---

## 10. The cycle question, resolved: robustness not timing (2026-07-13)

§9 ended pointing at the market cycle as the dominant variable and asked whether we can time it.
Answered empirically against our own NIFTY history, and it resolves the §2/§5-vs-§9 tension:

**How much cycle is in the data?** NIFTY net **-2.1%** over the 13 months (a choppy sideways grind,
not a bull run); worst drawdown **-15.2%**; **0** transitions of the 200DMA regime (one episode; the
200DMA barely exists given <200d of prior history). The entire "cycle" is essentially **one event** —
a sideways grind (Jun 2025-Feb 2026) -> a ~-15% crash in March 2026 -> a weak choppy recovery.

**Therefore cycle-TIMING is unlearnable here.** A regime->archetype switcher would be fit to N=1
transition = a memorized anecdote, the exact false-discovery trap of §8. So §2/§5 were RIGHT to demote
fine-grained regime conditioning; §9's "the cycle dominates the archetype" is true but does NOT license
timing it, because we have one cycle. This is now a settled constraint, not an open question.

**A coarse capital-protection floor is legitimate — as risk control, not alpha.** A pre-committed
(not fitted) "hold cash when NIFTY < its 50DMA" overlay on NIFTY over the sample cut max drawdown
-15.2% -> -8.7% and vol 13% -> 6%, and sat out 33/34 days of the -13% March crash — but it COST return
(-2.1% -> -4.4%) because trend filters whipsaw in a choppy tape (well documented; §4 notes the same for
ATR stops). So the floor is a genuine risk/return trade, justified on principle (survive the next
March), sized conservatively, and NEVER sold as return-enhancing. Tested on NIFTY; a high-beta
small/mid book would crash harder, so the floor's protective value (and whipsaw cost) would both be
larger.

**Resolution — the near-term strategy is cycle-ROBUST, on three legs, none of which times the cycle:**
1. Lean on the one signal that survived the regime split on benchmark-excess: relative-strength /
   breakout (held +3.6%/+3.9% in both the choppy and weak sub-periods, §9). Modest, but unbroken.
2. **Risk control does the heavy lifting** — position sizing, cost-awareness, no over-deployment.
   This is `[T2]` (portfolio & risk layer), now doubly confirmed as the real near-term priority.
3. A coarse crash floor as capital protection (leg above).

Net arc (momentum -> reversal -> cycle): do NOT chase archetypes (both fragile), do NOT time the
cycle (one transition), DO control risk and judge everything on benchmark-excess. `[T0.75]` (#2, the
funnel-philosophy rethink) is thereby answered in principle: the funnel's weakness is not a
re-philosophizable selection error but marginal selection edge in this data — so leverage sits in
not-losing (T2) and the single cross-regime-robust signal, with the discovery engine kept as slow
research (§8.5) until years of data exist.

---

## 11. Portfolio & risk layer built (T2, 2026-07-13) -- the near-term priority, per the arc

§8.3 + §10 concluded the leverage is NOT-LOSING, not selection. `advisory/portfolio_risk.py` builds
that, as an advisory/report layer that DEFERS to the deterministic caps (max 5 positions, 5%/name,
25%/sector, 100% total from `LLM_DECISION_*`) and can only be MORE conservative:

- **Volatility-targeted sizing** (`size_position`) -- risk a fixed fraction of capital (default 0.75%)
  to a 2.5*ATR stop, so a volatile name gets a smaller position for the same rupee risk (12% ATR ->
  2.5% weight; calm names bind on the 5% per-name cap). The system had caps but never SIZED by vol.
- **Book concentration** (`book_metrics`) -- portfolio vol, effective-number-of-bets,
  diversification ratio, avg pairwise correlation. Surfaces "one bet wearing many tickers" (div ratio
  ~1) that per-name caps structurally miss -- the real risk for a themed book (the §9 winners
  clustered in silver / defense-capex, i.e. highly correlated).
- **Crash floor** (`crash_floor_multiplier`) -- the §10 pre-committed NIFTY<50DMA exposure cut, as
  capital protection only (report: maxDD -15%->-10%, vol 12%->8%, costs return in chop). Insurance,
  never alpha; sized conservatively.
- Plus `portfolio_heat` (aggregate open risk vs a heat cap) and `sector_exposure`.

Guardrail held: report-only, no live authority, defers to the hard limits. Deferred follow-up: a
LIVE advisory hook into `portfolio_engine.py` (populate an advisory sized-weight/heat field for
operator visibility) -- held back on purpose because it touches live sizing (CLAUDE.md: do not mutate
broker/portfolio behavior unprompted); wire it only when explicitly asked. This closes the near-term
arc: costs honest (T0), selection marginal (8-9), cycle untimeable (10), risk layer built (11) --
the discovery engine stays slow research (8.5) until years of data exist.

---

## 12. Funnel correctness net (Part A, 2026-07-13) -- prove the code, not just the data

Operator concern after the scale bug: "nothing works if the code has big bugs like these" -- we kept
explaining "no BUYs" with data/strategy reasoning while a silent, non-crashing code bug (`technical_score`
0-1 compared to a 0-100 threshold -> dead branches) was the real defect, found by luck. Part A builds the
net so this class is caught by design:

- **Scale registry** (`advisory/score_scales.py`) -- single source of truth for which score fields are
  0-1 (`technical_score`, `setup_score`, ...) vs 0-100 (`technical_total_score`, sub-scores,
  `conviction_score`, `rs_percentile`), plus `to_100()`. The 0-1-vs-0-100 confusion was root-cause.
- **Invariants audit** (`scripts/funnel_invariants.py`, in the validation checklist, `--strict`): a
  static **impossible-gate detector** (a 0-1 field compared `>1.0` can never be True -- self-updating,
  re-scans source each run, would have caught `technical_score>=78`), plus real-data **range** and
  **cross-field** checks (`technical_entry_confirmed=True => BUY_TRIGGERED`; `PASS_NOW => BUY_TRIGGERED
  or override`). It immediately surfaced a real provenance warning (99 PASS_NOW rows with neither).
- **Golden-path reachability tests** -- the canary the codebase lacked: an *ideal* candidate must reach
  `BUY_TRIGGERED -> PASS_NOW -> technical_entry_confirmed` end-to-end, a *garbage* one must not, quality
  is *monotonic*, and the funnel *ceiling is PASS_NOW, not a BUY action*. Piecewise tests missed the
  scale bug because they baked in the same wrong scale; an end-to-end reachability assertion cannot.
- **Scale bug fixed** (`to_100` in `company_memory_review` -- capped review-only WATCH per authority +
  section 9 -- and `action_recommender`), and a new no-BUY cause
  `confirmed_entry_exists_but_no_promotion_bridge` in `recommendation_diagnostics`.

**Confirmed: the "no BUYs" is architectural, not a bug swarm.** `action_recommender` emits BUY only from
an already-approved portfolio row (:6512); there is no promotion bridge candidate->approved->BUY (the
human/[P-LLM-AUTH] step is unbuilt). The funnel's honest ceiling for a fresh candidate is
`PASS_NOW + technical_entry_confirmed`. Part B (the paper decision loop) is built next on this verified
ground, deliberately bypassing the missing bridge to generate real forward outcomes.
