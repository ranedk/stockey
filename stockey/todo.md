# Stockey Priority Todo

Updated: `2026-07-13`

Single active planning document. Priority-ordered backlog + current-state summary.
This rewrite supersedes the 2026-06-23 audit backlog: that arc (correctness, migrations,
OOM, data-layer robustness) is largely shipped, and the project has moved into the
**discovery-engine / alpha** phase. The governing design is `docs/specs/discovery_engine.md`
(read it first) and `docs/specs/actor_critic_closed_loop.md`.

## How To Use This File (LLM Task Contract)

Each task is a self-contained card: **Why** (problem + evidence), **Files** (the only files to
open; use `rg`, line numbers drift), **Do** (steps), **Guardrail** (authority/point-in-time
boundary), **Validate** (smallest check + acceptance), **Confidence** (`verified` /
`verify-first`).

Global rules (see `CLAUDE.md` + `docs/specs/discovery_engine.md`):
- **Adaptive tracking, not convergent optimization.** Data is fixed (~13 months). Favor simple,
  low-dimensional, robust methods (pooled cross-section, rank-IC, walk-forward). Seed intuition-
  based rules; let the daily corrective run nudge them. Never "solved", always "current".
- **Robustness is the moat.** No discovered relationship earns a knob change without walk-forward
  + multi-definition robustness + enough matured labels + false-discovery control.
- **Priors, not proofs.** Take market truths as ~90% priors; do not re-derive them; size positions
  so the ~10% leaks are absorbed as portfolio risk.
- Review-only everywhere; `broker_execution_allowed=false`; master flags OFF; append-only
  migrations; typed evidence/provenance on every decision; point-in-time (no look-ahead).
- Narrow tests per behavior; env/docs/cron-preflight/migration-drift audits green per commit.

---

## Current State (shipped this arc)

- **Dynamic screener fabric**: regime admission policy, cross-sectional RS ranking, hypothesis
  strategy instances (universe/alignment/retirement), theme screeners, watch retention with
  recorded exits, watch tiering.
- **Coverage lanes**: market-action scan (new-high breakout), volume-surge scan (base breakout),
  momentum-trend scan (multi-day grind) — each a graded `dynamic_source`.
- **Momentum-continuation entry archetype**: archetype-aware technical engine (momentum replaces
  `base_too_deep` with a parabolic guard + orderly-trend structure) + `MOMENTUM_CONTINUATION_V1`
  sleeve. The engine can finally buy a runner (SUVEN: REJECT → NEAR_PIVOT).
- **Measurement substrate**: regret ledger (1,717 matured labels), missed-movers analyser,
  outcome labeling, walk-forward multi-archetype backtest (`archetype_backtest.py`).
- **Conditional allocation Phase A**: momentum sleeve cap — currently NEUTRALIZED (the regime
  tilt was OOS-confirmed backwards; `MOMENTUM_SLEEVE_TILT_ENABLED=false`).
- **Data-layer robustness**: bhavcopy zero-byte guard + morning catch-up, benchmark freshness
  gate (stale NIFTY zeroed rs_vs_benchmark market-wide), Sharpely cache-poisoning fix + encrypted
  v2 endpoint reverse-engineered + fast-source fundamentals, technical-feature bhavcopy fallback,
  data-readiness gate on all long jobs, daily log rotation.

Key learned facts (do not relitigate): entry-confirmation gate is VALIDATED (regret ledger:
−2.86% forward excess on what it blocks); costly gates are admission/coverage; the "buy strength
when weak" edge is fragile/definition-sensitive; regime is fragile and should be discovered
(IC-drift), not imposed.

---

## Build Queue

### T1 — Sub-score IC validation + slow adaptive reweighting  `[BUILD FIRST]`

- **Why.** The scoring function is the heart of every decision and is entirely hand-weighted
  (sub-score point allocations, the 78 buy bar, sub-score minimums). We have never measured whether
  a high sub-score actually predicts a higher forward return. This single build pays triple:
  reweights scoring from evidence, hands us the buy-bar knee, and its IC-drift is the *discovered
  regime signal* that retires the fragile regime layer. See `docs/specs/discovery_engine.md` §3.
- **Files.** NEW `advisory/subscore_ic.py`; `advisory/technical_engine.py` (the sub-score
  functions + `DEFAULT_THRESHOLDS`/weights); reuse `advisory/archetype_backtest.py` harness
  patterns (walk-forward, forward-return SQL, regime tagging).
- **Do.**
  1. For each sampled historical date, compute each sub-score (trend/structure/participation/RS/
     tradability) per name (point-in-time) and its forward 5/10/20d benchmark-excess.
  2. Metric = **rank-IC** (Spearman of sub-score vs realized forward excess), computed on the
     **pooled cross-section** per archetype (NOT per-stock). Report each sub-score's IC + a
     multi-weight fit's IC.
  3. Fit IC-maximizing sub-score weights on the pooled cross-section, per archetype, with
     **shrinkage** toward the current hand-set weights (data-frugality; do not overfit 13 months).
  4. **Slow update**: the applied weights move a small bounded step toward the fit (not a jump);
     env `SUBSCORE_IC_UPDATE_STEP`. Persist a daily row (weights + IC + drift).
  5. **IC-drift alarm**: track aggregate IC vs its trailing baseline; a sharp collapse = regime
     change → surface it (feeds the risk-off safety floor / de-risk signal). Per-stock residual =
     secondary anomaly flag only.
  6. Buy-bar knee: report, per archetype, the score threshold where forward expectancy peaks
     (research-only; do not auto-apply the bar yet).
- **Guardrail.** Research/propose-only first — do NOT auto-apply weights to live scoring until a
  trust gate exists (T3). Walk-forward + robustness required before any weight is trusted. Point-in-
  time: only pre-decision data in the fit; forward windows are the outcome. Deterministic risk
  limits are never touched.
- **Validate.** Unit tests: rank-IC math on a synthetic panel (known ordering); pooled-vs-per-stock
  (per-stock refused/insufficient); shrinkage bounds; slow-step never jumps; drift alarm fires on a
  synthetic IC collapse. Live: report each sub-score's IC over history + the fitted weights + the
  buy-bar knee per archetype. `pytest -q tests/test_advisory_regression.py -k subscore_ic`.
- **Confidence.** `verified` (design grounded in the shipped harness; sub-score functions confirmed
  in `technical_engine.py`).

### T2 — Exit-as-scored-decision (design pass, then build)

- **Why.** Exits are the unmeasured half of the P&L. Frame: symmetric to entry — a reversal score
  from features that predict a forward drawdown, validated by the same harness, with an ATR/gap
  disaster-stop and time cap underneath. See `docs/specs/discovery_engine.md` §4.
- **Files.** design note first; then NEW `advisory/exit_engine.py` + reversal features in
  `advisory/technical_features.py`; `advisory/portfolio_engine.py` / lifecycle for exit rows.
- **Do.**
  1. **Design pass (do this first, no code):** short literature-informed note — reversal feature set
     (distribution-day clusters, break of rising DMA20/50, RS deterioration, momentum divergence,
     up-day volume dry-up), the primary=scored-reversal + floor=ATR/gap-stop + time-cap structure,
     and the metric (does the reversal score predict forward drawdown?).
  2. Build the reversal features + a `score_reversal` validated by the archetype-backtest harness
     (exit-side sibling: measure forward *drawdown* after the reversal score crosses a threshold).
  3. Exit decision = scored reversal crossing a discovered threshold, with the ATR/gap stop as the
     hard floor and a max-holding time cap. Review-only rows; no broker execution.
- **Guardrail.** Review-only; `broker_execution_allowed=false`. Exit thresholds discovered/validated,
  not hand-forced. The ATR/gap stop is a capital-protection floor (allowed as a simple rule).
- **Validate.** Reversal-score predicts forward drawdown on history (backtest); unit tests on the
  reversal features + threshold logic + the disaster-stop floor.
- **Confidence.** `verify-first` (needs the design pass; exit lifecycle integration points to confirm).

### T3 — Daily discovery run + trust gate

- **Why.** Wrap T1 (and later T2) into the self-correcting loop the operator asked for: a daily
  "what can we do better" run + knobs that self-adjust within guardrails. See
  `docs/specs/discovery_engine.md` §6 and `actor_critic_closed_loop.md` (Stage 1→3).
- **Files.** NEW `advisory/discovery_run.py`; a governed-parameter store (from the actor/critic
  spec §5) so knobs are versioned/revertable; cron wrapper + entry.
- **Do.**
  1. Daily run: re-fit T1 (and T2) on recent data; emit a human-legible ranked report — which knobs,
     moved how much, would improve OOS expectancy; which findings are robust vs fragile.
  2. **Trust gate (first-class):** a knob moves only after walk-forward + multi-definition robustness
     + enough matured labels + **false-discovery control** (the danger of a daily multi-knob scan —
     correct for it), then a small bounded reversible step, monitored with auto-revert.
  3. Governed-parameter store: knobs read through a resolver (env/YAML fallback) so the run can
     propose/apply/revert with provenance.
- **Guardrail.** Default-OFF master flag for any auto-apply (mirror `[P-LLM-AUTH]`). Tier-C
  (risk/stop/exposure/authority) never auto-touched. Every proposal + application carries typed
  evidence/provenance.
- **Validate.** Trust-gate unit tests (rejects a finding that fails FDR / lacks labels / regresses
  OOS; accepts a robust one); dry-run report on real data; auto-revert on a synthetic degradation.
- **Confidence.** `verify-first` (largest piece; build after T1 proves the signal-to-noise supports it).

---

## Standing Decisions (do NOT build — keep as intuition-seeded rules + monitoring)

- **Source-lane weighting — dropped.** Lanes are junk-eliminating filters, not weighted
  contributors. Keep the regret ledger + missed-movers as the directional health check; prune a
  losing lane manually.
- **Catalyst standalone predictor — not built.** ORDER_WIN / RESULTS_POSITIVE are a ~90% prior;
  they act as a conditional conviction/size enhancer ONLY when the technical pipeline also fires in
  a favorable tape. Never a standalone trigger.
- **Fine-grained regime conditioning — demoted.** Replaced by the discovered IC-drift signal (T1)
  + a coarse risk-off safety floor (a simple capital-protection rule, not an alpha bet).

---

## Deferred / Lower Priority

- Phase B conditional allocation (performance-following sleeve tilt) — only after T1's IC-drift +
  matured sleeve grades exist; the regime tilt stays neutralized until then.
- Theme-screener suggested-query auto-execution (manual Screener.in registration stays).
- Watch tiering decay / theme watch-tiering refinements.
- `sharpely_stock_meta` was cache-poisoned then rebuilt; keep an eye on the weekly refresh cadence.
