# Advisory Split — inventory & migration plan

*2026-07-27. Companion to `DATA_CONTRACT.md`. Prepared from the systrader
side by static analysis (writers of contract tables + module naming); each
classification below is a PROPOSAL to verify against imports/crontab before
moving or archiving anything.*

Goal: stockey becomes a pure data platform; ALL research, TA, and stock
selection authority moves to systrader. Stripped code is **archived, not
deleted** (`advisory_attic/` or a git branch) — its experiment history is
evidence.

## Category A — PROMOTE to `data/` first (produce/validate contract tables)

These must survive the strip and move out of `advisory/` beforehand:

| Module | Why |
|---|---|
| `price_adjustment.py` | **writes `advisory_adjusted_ohlcv_daily` — systrader's primary price series.** The strip must not kill this producer. |
| `data_readiness.py`, `data_sync.py`, `sync_state.py` | data plumbing/health used by the pipeline (verify imports) |
| `current_prices.py` | live price fetch (verify it's data, not signal) |
| `identity_issues.py` | symbol identity hygiene, dim_security-adjacent |
| `feature_freshness.py`, `event_data_quality.py` | data-quality checks worth keeping if decoupled from advisory scoring |

## Category B — KEEP as operations/monitoring (not research)

`cron_status.py`, `operator_health.py`, `operator_smoke.py`,
`operator_snapshot.py`, `performance_slowlog.py`, `migration_drift.py`,
`fallback_telemetry.py`, `external_task_queue.py`, `live_notifier.py` —
plus `dashboard.py`/`live_dashboard.py` ONLY insofar as they display data
health; recommendation views die with the advisory engine.

## Category C — ARCHIVE (research/TA/selection → authority moves to systrader)

Everything else, notably these families (~120 of 145 modules):

- **Signals/TA**: `technical_*`, `price_factors.py`, `relative_strength.py`,
  `market_breadth.py`, `momentum_*`, `signal_quality_*`, `signal_refresh.py`,
  `intraday_features.py`, `exchange_features.py`, `macro_features.py`
- **Selection/screening**: `hypothesis_*`, `screener_*`, `theme_screeners.py`,
  `watchlist_builder.py`, `factor_graduation.py`, `factor_ic_sweep.py`,
  `factor_tilt.py`, `subscore_ic.py`
- **Strategy/backtest**: `adaptive_ensemble.py` (superseded by systrader's
  `adaptive_ensemble_strategy.md` implementation), `archetype_backtest.py`,
  `momentum_backtest.py`, `strategy_lab.py`, `strategy_registry.py`,
  `strategy_allocation_policy.py`, `north_star.py`
- **Regime**: `regime_engine.py`, `regime_overlay.py`,
  `regime_admission_policy.py`, `regime_shadow_ledger.py`
- **Events/models**: `event_*` (policy/meta_model/router/evidence…),
  `announcement_*`, `causal_event_*`, `news_*`, `model_training_runner.py`,
  `training_universe.py`
- **TimesFM/forecasting**: `ts_forecast_*` (the *idea* moves to systrader as
  vol/regime forecaster per `law_1_story.md`; this implementation is archived)
- **LLM decision stack**: `llm_*`, `prompt_*`, `adversarial_review*`,
  `company_memory_review.py`, `decision_trace.py`
- **Advisory trading stack**: `action_*`, `recommendation_diagnostics.py`,
  `portfolio_engine.py`, `portfolio_risk.py`, `position_lifecycle.py`,
  `execution_engine.py`, `risk_engine.py`, `rule_engine.py`,
  `wait_signals.py`, `deployment_timing.py`, `paper_*`,
  `operator_holdings.py`, `market_action_scan.py`, `market_context.py`,
  `context_*`, `exchange_context_overlays.py`, `macro_context_overlays.py`,
  `continuous_watch.py`, `missed_movers_analyzer.py`, `regret_ledger.py`,
  `return_attribution.py`, `superseded_failures.py`, `negative_pressure_evaluator.py`
- **Pipeline shells**: `pipeline.py`, `master_pipeline.py`,
  `full_stack_runner.py`, `research_evidence_runner.py` (whatever parts
  orchestrate A/B survive in slimmed form)

## Special handling (do NOT just archive)

1. **`research_ledger.py` + `multiple_testing.py`**: export their experiment
   records FIRST and fold them into `systrader/research/LEDGER.md` as prior
   trials (`trials=N` batch rows). Otherwise systrader's M-count — and every
   t-stat bar derived from it — is dishonest about what this data has
   already been asked.
2. **`cost_model.py`**: mine for the actual Dhan fee schedules before
   archiving (systrader open question #5 needs delivery/F&O/MCX fees).
3. **`adaptive_ensemble.py` + `config/adaptive_ensemble.json`**: any tuned
   parameters in there are CONTAMINATED (fitted without a burn registry).
   systrader re-derives everything from scratch on its Part A; do not port
   values.
4. **Cron entries** (`config/stockey.crontab.template`, `all_*.sh`): every
   archived module's schedule entry must be removed in the same change, or
   ops pages for dead jobs.

## Suggested order

1. Promote Category A out of `advisory/` (verify `price_adjustment.py`
   still runs from its new home; its output feeds systrader daily).
2. Export ledger/experiment records → systrader LEDGER (special #1).
3. Mine cost model (special #2).
4. Archive Category C + prune crontab/`all_*.sh` in one commit.
5. Slim Category B dashboards to data-health only.

---

**SCOPE WIDENED 2026-07-27 — see `docs/DATA_INVENTORY.md` (authoritative):**
operator decided PURE TA. The fundamental/news/event/screener stacks — listed
above as candidates to keep — now ALSO archive, along with their collectors
(announcements, economictimes, screenerin, sharpely-fundamentals, macro,
insider/deal-flow). Category A (data producers) and the price-adjustment
promotion are unchanged. All LLM-token consumption in stockey ends.
