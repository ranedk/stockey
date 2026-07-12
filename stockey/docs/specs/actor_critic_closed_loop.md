# Spec: Two-System Closed-Loop Advisory (Actor + Critic)

Status: draft for operator review (2026-07-12)
Author: engineering
Related: `docs/llm_decision_authority.md`, `advisory/technical_threshold_calibration.py`,
`advisory/regret_ledger.py`, `advisory/missed_movers_analyzer.py`, `todo.md`

---

## 1. Framing

- **System 1 — the Actor.** The current advisory pipeline: screeners/scans → candidates →
  technical/risk/lifecycle gates → consolidated actions → review-only decisions. Its
  behaviour is governed by ~30 hand-set parameters (scan volume/breakout thresholds, the
  `buy_total_min` 78 bar, sub-score minimums, admission tolerances, RS weight, retention
  windows, per-regime scan caps, …).
- **System 2 — the Critic.** Observes System 1's calls **and its non-calls**, scores them
  against matured realized outcomes, and improves System 1 — *safely*.

We already own the measurement substrate. What is missing is the
**attribution → proposal → out-of-sample validation → bounded application** loop.

**The honest constraint.** A naive optimizer that nudges parameters toward whatever would
have helped yesterday is exactly what this platform forbids (CLAUDE.md: "technical
threshold calibration is research-only … must not auto-apply"; "do not promote thresholds
from one good-looking sample"; "keep false-discovery and backtest-overfitting risk
visible"). The intellectual work of this spec is capturing the continuous-improvement
benefit **without** the overfitting trap. Everything below is built around that.

---

## 2. What "right" and "wrong" mean (the objective)

Every day, System 1 makes two kinds of call on each liquid name:

|                    | Name went on to WIN (excess>cost) | Name LOST |
|--------------------|-----------------------------------|-----------|
| **We acted (BUY)** | true positive (good)              | commission error |
| **We did not act** | omission error / regret           | true negative (good) |

- **Commission errors** come from `advisory_llm_decision_outcomes` (BUYs that matured negative).
- **Omission errors** come from `advisory_regret_ledger` (gate-blocked would-be BUYs that
  won) and `advisory_missed_movers_daily` (names that never entered the funnel at all).

**Objective = net benchmark-excess-after-cost of the decision policy**, NOT accuracy. A "BUY
nothing" policy has perfect accuracy and zero alpha; the objective must reward acting on
winners and penalize both error types.

**Why not Sharpe-style risk-adjustment (operator decision 2026-07-12).** Total-volatility
penalties punish *upside* volatility, which is the signature of the momentum/breakout moves
we exist to catch — Sharpe would bias the whole system against its own edge. So we do NOT
penalize upside dispersion.

**Why not naive raw excess either.** Raw sum is outlier-dominated — it cannot tell "+40% on
one lucky name, −5% on twenty others" (luck) from "+8% consistently on twenty" (edge). For a
many-decision policy that distinction is the whole game.

**Chosen objective — raw net excess, measured as expectancy, guarded on the downside only:**

```
J(θ) = Σ_acted excess_after_cost  −  λ_omit · Σ_missed_winner excess_foregone
report/guard:  expectancy = hit_rate·avg_win − (1−hit_rate)·avg_loss
               downside_dev (Sortino-style, negative-excess dispersion only)
```

Optimize raw net excess (transparent, no upside penalty), but decompose every candidate
change into hit_rate / avg_win / avg_loss and reject "improvements" that raise total excess
while *lowering* hit_rate or worsening downside deviation — i.e. lumpy, luck-driven gains. A
straight-up winner has ~zero downside deviation and is fully rewarded; a one-outlier fluke is
caught. `λ_omit`, cost bps, and horizon (5/10/20d) are operator-set.

---

## 3. The overfitting problem is the design (defenses, not caveats)

With ~30 parameters and daily data, fitting noise is trivial. Every one of these is
mandatory, not optional:

1. **Walk-forward only.** Fit on `[T−N, T−M]`, validate on `[T−M, T]`, never read past `T`.
   No parameter proposal exists without an out-of-sample (OOS) validation window.
2. **Minimum matured labels per parameter.** A parameter is only tunable once ≥
   `TUNING_MIN_MATURED_LABELS` matured decisions actually exercised its active region
   (env; start 30–50). Below that, System 2 stays silent on it.
3. **Effect-size + significance gate.** A change must improve OOS `J` by more than a noise
   band **and** survive multiple-comparison / false-discovery control across the whole
   parameter set (Benjamini–Hochberg or a permutation null). "It would have helped once" is
   not evidence.
4. **Bounded step size.** Parameters move in small relative increments (≤
   `TUNING_MAX_STEP_PCT`, e.g. 10%) per cycle — nudges, never jumps. Structural discontinuity
   is impossible by construction.
5. **Do-no-harm gate.** Net OOS improvement is required; ties and regressions are rejected.
6. **Champion/Challenger shadow.** A proposed config runs as a *challenger* in research-only
   scoring alongside the live *champion* for `TUNING_SHADOW_CYCLES` cycles and must beat it
   OOS before it can be promoted. Nothing goes live on a single window.
7. **Regime conditioning.** Tuning is tagged by regime admission state (risk_on / neutral /
   risk_off). A parameter learned in risk_on does not transfer blindly to risk_off; at
   minimum it is regime-tagged, ideally regime-scoped.

If any defense cannot be satisfied for a parameter, System 2 reports "insufficient evidence"
for it — a first-class, expected output, not a failure.

---

## 3b. Attribution granularity — condition on the coarsest partition that still matters

The central bias-variance question: tune parameters globally, per-stock, per-sector, or
per-size? Decision (operator, 2026-07-12):

- **Per-stock — never.** A handful of decisions per name, no statistical power, character
  drifts. Pure overfitting.
- **Per signal lane — yes, already natural.** Each scan/source owns its parameters; a
  smallcap surge and a largecap breakout are different animals. Top-level split.
- **Size / liquidity bucket (small / mid / large) — the PRIMARY conditioning dimension.**
  Momentum genuinely differs by cap (smallcaps trend and reverse harder; largecaps
  mean-revert), and there are enough names per bucket for power.
- **Regime state (risk_on / neutral / risk_off) — the SECOND dimension.** Already in the
  system; signals behave differently across regimes.
- **Sector / industry — EVIDENCE-ONLY, never an auto-tuned split.** 22+ sectors → thin,
  overfit-prone cells, and sector effects are partly captured already by RS/participation.
  System 2 *reports* sector-level performance ("momentum works in capital goods, fails in
  pharma") for human/LLM review (Tier B); it does not auto-split parameters by sector.

**Auto-tuning grid: `lane × size-bucket × regime`** (~3×3 per lane, manageable) **with
hierarchical fallback (partial pooling).** A thin cell (e.g. `smallcap × risk_on`, 12
labels) borrows from its parent (`smallcap`, all regimes) rather than overfitting its own
sample — standard multilevel shrinkage, which solves data sparsity cleanly. A cell auto-tunes
only once it independently clears `TUNING_MIN_MATURED_LABELS`; otherwise it inherits.

**Corollary — attribute both sides at the same granularity.** Commission outcomes (BUYs) and
omission outcomes (regret + missed-movers) must both be tagged with size-bucket and sector so
the conditioned attribution sees the whole picture. `advisory_missed_movers_daily` and
`advisory_regret_ledger` gain size-bucket + sector columns.

**Governed-store consequence.** The parameter store holds *conditioned* rows
`(param_key, size_bucket, regime_state) → value` with resolution order
`specific cell → size-only → regime-only → global default`, not flat params.

## 4. Parameter taxonomy — what System 2 may touch

- **Tier A — tunable (continuous, monotonic effect, well-measured).** Scan thresholds
  (volume multiple, breakout %, surge change %, momentum lookback/return), the
  `buy_total_min` bar, sub-score minimums (participation/structure/RS/trend/tradability),
  admission tolerances (market-cap band, liquidity floor), RS priority weight, retention
  windows, per-regime scan caps. These are where the loop operates.
- **Tier B — propose-only (structural / discrete, needs human or LLM judgment).** Whether
  fundamentals are required for a setup, whether an announcement class vetoes, the regime
  state rule itself, adding/retiring a whole signal lane. System 2 supplies evidence; a
  human or the LLM-review path decides.
- **Tier C — never auto-touch (safety / authority).** Deterministic risk sizing, stop and
  exposure caps, the master authority flags, point-in-time discipline. System 2 has no
  write access here, ever. (CLAUDE.md "engineering safety" invariants.)

---

## 5. Architecture & data flow

**Substrate (exists):** `advisory_llm_decision_outcomes`, `advisory_regret_ledger`,
`advisory_missed_movers_daily`, the outcome labelers, `technical_threshold_calibration`.

**Prerequisite — governed config store.** Today tunable params live as env/YAML constants,
which System 2 cannot version, propose against, or revert with provenance. Introduce
`advisory_governed_parameters` (param key, current value, tier, bounds, regime scope,
provenance, updated_by, updated_at). System 1 reads params through a thin resolver that
falls back to the existing env/YAML defaults — so this is additive and fail-safe.

**New System 2 components:**
- **Decision ledger** (`advisory_decision_ledger`): one row per act/non-act call with the
  full parameter context (`θ` snapshot) at decision time — the join key for attribution.
- **Attribution engine:** matures outcomes onto the decision ledger; computes, per Tier-A
  parameter, the OOS counterfactual `ΔJ` of a bounded step, with label counts and
  significance.
- **Proposal engine:** emits typed proposals (`advisory_tuning_proposals`: param, from→to,
  regime scope, OOS `ΔJ`, labels, significance, evidence blob) — only for params clearing §3.
- **Champion/Challenger scorer:** runs challenger configs in shadow, records comparative OOS
  `J`, gates promotion.
- **Monitor / auto-revert:** watches live post-application outcomes; on degradation beyond a
  band, auto-reverts to the prior governed value and alerts (never blocks the pipeline).
- **Applied audit** (`advisory_tuning_applied`): every applied change + its revert triggers.

---

## 6. Staged rollout (mirrors the LLM-authority discipline)

- **Stage 0 — substrate (done).** Outcome labeling, regret ledger, missed-movers analyser.
- **Stage 1 — Critic, read-only (start here).** A daily **tuning report**: "these K Tier-A
  parameters, nudged thus, would have improved OOS net-excess by X (N matured labels,
  significant after FDR); these lack evidence." Applies nothing. Immediately useful, zero
  risk, and it tells us whether the signal-to-noise even supports Stage 2.
- **Stage 2 — Proposer (reviewed diffs).** System 2 emits bounded config-patch proposals into
  the existing reviewed-diff workflow; a human or the LLM-review path approves. Mirrors
  `technical_threshold_calibration`'s "candidate reviewed patch" contract, generalized.
- **Stage 3 — Bounded auto-apply (default-OFF master flag).** Challenger-validated,
  walk-forward-gated, step-bounded changes auto-apply with live monitoring + auto-revert.
  Gated exactly like `[P-LLM-AUTH]`: opt-in per deployment, bounded so no single change is
  catastrophic, monitored to catch systematic error.

We commit only to Stage 1 now; Stage 2/3 are decided on Stage 1's evidence quality.

---

## 7. The multi-day momentum signal — the first concrete Actor component

Build now (the missed-movers analyser's top `no_signal` bucket: 39 of 94 Friday up-movers,
ONMOBILE +44% / SUVEN +34%, multi-day moves to new highs the single-day breakout scan
misses). Design mirrors the existing scan lanes:

- New source `momentum-trend-scan-v1`: cumulative return ≥ `X%` over `N` trading days
  (default ~15% / 10d), still rising (above rising DMAs), 52w-high proximity ≥ threshold,
  junk filters identical to the other scans (price/turnover/circuit). Its own
  `dynamic_source` so the regret ledger + missed-movers analyser grade it independently.
- **Its parameters register in the governed config store from day one**, so it becomes
  System 2's first tuning target once matured labels accrue. This grounds the whole spec in
  a real, immediately valuable Actor improvement.

---

## 8. Guardrails (non-negotiable — from CLAUDE.md)

- Typed evidence/provenance + reason contract on every proposal and application.
- Point-in-time / walk-forward; no look-ahead anywhere in the tuning path.
- Master enable flag, default **OFF**, for any auto-apply (Stage 3).
- Deterministic risk/exposure/stop limits are Tier C — never tunable by System 2.
- Live monitoring + auto-revert on degradation; alerts never block the pipeline.
- Research-only proposals + reviewed diffs precede any auto-apply.
- False-discovery / overfitting risk surfaced in every report, not hidden.

---

## 9. Open decisions for the operator

1. **Config governance scope.** Introduce `advisory_governed_parameters` now (prereq for
   Stage 2+), or keep Stage 1 purely read-only against env/YAML and defer governance?
2. **Objective function.** Net excess-after-cost — raw, or risk-adjusted (Sharpe-like) with a
   drawdown penalty? Values of `λ_omit`, `λ_risk`, horizon.
3. **Regime granularity.** Tag-only vs fully regime-scoped parameters.
4. **Ambition.** Stage 1 only for now, or commit to the Stage 3 vision so we build the
   substrate (decision ledger, governed store) accordingly from the start?

---

## 10. Near-term concrete plan

1. **Now:** build `momentum-trend-scan-v1` (Actor coverage), params registered for future
   tuning. Immediately closes the largest measured coverage gap.
2. **Next:** Stage 1 Critic — the daily tuning report over existing matured outcomes + regret
   + missed-movers, with the full §3 defenses. Read-only.
3. **Then:** review Stage 1 evidence quality with the operator; decide Stage 2/3.
