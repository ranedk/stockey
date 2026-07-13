# Spec: The Discovery Engine — adaptive tracking, not convergent optimization

Status: design of record (operator + engineering, 2026-07-13)
Companion to: `docs/specs/actor_critic_closed_loop.md` (this doc is the governing philosophy
and the concrete near-term design; the actor/critic spec is the broader mechanism).
Grounded in: the momentum episode (2026-07-12/13) where a confident reasoned prior was
*backwards* and a striking single-sample finding was *fragile* — both caught by the walk-forward +
multi-definition robustness harness.

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

1. **Sub-score IC validation + slow adaptive reweighting** (§3) — the heart; most tractable; pays
   triple (scoring weights + buy-bar knee + discovered regime). BUILD FIRST.
2. **Exit-as-scored-decision** (§4) — symmetric harness + volatility floor + time cap; short
   literature-informed design pass first.
3. **Daily discovery run + trust gate** (§6) — wraps 1 (and later 2) into the self-correcting loop
   with false-discovery control.

Everything else: intuition-seeded rules + monitoring (§5).
