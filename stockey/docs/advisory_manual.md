# Advisory Manual

This is the practical operator and maintenance guide for the investment advisory system.

Use this document when you want to:

- run the advisory stack end to end
- run one stage manually
- add or change Screener.in screeners
- add or change setup rules
- understand which module owns which behavior
- inspect outputs and debug failures

Use [`docs/implementation.md`](implementation.md) for the current architecture summary. Use [`todo.md`](../todo.md) for the active roadmap and remaining bottlenecks.

For common edits with copy-paste commands, use [`docs/advisory_change_cookbook.md`](advisory_change_cookbook.md).

For the short operator runbook, use [`docs/operators_manual.md`](operators_manual.md).

## Core design

The advisory runtime path is:

1. Screener.in universe
2. Dhan OHLCV
3. Sharpely fundamentals and peers
4. Macro snapshot
5. Regime classification
6. Top-50% market context universe
7. Rule engine
8. Watchlist
9. Official announcements and ET RSS news
10. LLM event evaluation
11. Event interpretation and optional event meta-model scoring
12. Experimental OHLCV time-series forecast features
13. Adversarial event review
14. Risk sizing
15. Portfolio planning
16. Lifecycle tracking
17. Consolidated action recommendations with mandatory reason contract
18. Execution planning

## End-to-End Summary

1. Run raw ingestion for Dhan, Sharpely, macro, NSE, announcements, and ET RSS.
2. Sync only registered production Screener.in screeners.
3. Use ad hoc Screener.in queries separately for research.
4. Normalize stored Screener snapshots into `advisory_screener_constituents`.
5. Build daily advisory snapshots for macro, fundamentals, technicals, and optional intraday features.
6. Convert the macro snapshot into `advisory_macro_features_daily` for point-in-time regime, model, and risk inputs.
7. Build the top-50% market context universe from market cap, traded value, liquidity, technical leadership, exchange events, news, announcements, and regime context.
8. Normalize NSE block/bulk/short/insider/corporate/earnings data into exchange events and features.
9. Build the base regime and the lightweight news overlay.
10. Detect active investment themes and map them to theme-linked production screeners when available.
11. Run the rule engine on each setup using screener universe plus snapshots, regime, overlay, and optional intraday confirmation.
12. Score candidates and assign states like `PASS_NOW`, `WATCH_*`, `ABSTAIN`, or `REJECT`.
13. Build the watchlist with screener, regime, overlay, and theme provenance.
14. Ingest announcements and relevant news for watched names and use the top-50% market-context summary as background evidence.
15. Use the LLM only to extract structured event tensors from text, with bounded exchange-event and market-context evidence where relevant.
16. Run deterministic event/playbook interpretation and adversarial review to clear, penalize, flag manual review, or veto.
17. Feed candidate state plus event outputs into risk sizing and allocation.
18. Rank approved allocations in the portfolio engine with overlap and setup caps.
19. Build lifecycle and rebalance outputs.
20. Consolidate buy, sell, partial sell, buy-more, hold, watch, and review actions into one action table.
21. Attach a mandatory logical reason contract explaining why each stock was screened, selected, sized, held, rejected, or exited.
22. Build execution-planning outputs from consolidated actions.
23. Record research runs in the research ledger when research-only paths are used.
24. Optionally prepare/train event models or build TS forecast features for research-only work.
25. Keep interpretation, prediction, policy, and execution separate.

For the main operator path, run:

```sh
./complete_data.sh
./all_advisory.sh
```

That sequence:

1. refreshes all raw data and parser outputs
2. runs event interpretation, playbook action planning, technical timing, risk sizing, and portfolio policy

Use model training separately only when you want research prep or training-only work:

```sh
./all_ml.sh
```

That command runs prep first, checks whether the requested horizon is actually ready, and only then trains and scores.

For the lightweight continuous watch loop, use:

```sh
./all_watchers.sh --loop
```

This loop:

1. polls 1-minute intraday OHLCV only for active watchlist symbols
2. polls announcements and ET/news incrementally
3. routes fresh alerts and events into symbol-level advisory reevaluation
4. writes live watch alerts when entry zones or invalidations are hit
5. records operator frontend status in `advisory_sync_state`

The static `live_dashboard/` refresh path is deprecated. Use the Nuxt operator frontend with `advisory.api.app` for current portfolio, watch, event, trace, and investor playbook views.

Trace inspection now has three operator paths:

- `python -m advisory.symbol_trace --symbol RELIANCE`
- `python -m advisory.decision_trace --unique-id <event-id>`
- `./all_frontend.sh`, then open the Decision Trace page in the Nuxt app

Decision Trace currently shows event evaluation, adversarial review, technical state, risk sizing, macro context, exchange-event context, portfolio allocation, lifecycle, rebalance, consolidated action conflicts, execution planning, execution safety, live submission, and reconciliation.

The live router currently prioritizes aggressively but does not impose a default hard cap:

- symbols that remain on the advisory watch path continue to be monitored
- open positions also stay monitored for exit-related alerts
- price alerts are still prioritized ahead of event-only items
- invalidation- and stop-related alerts are prioritized above softer watch hits
- explicit caps can still be supplied manually to the router CLI if needed for debugging

Important runtime decisions:

- canonical advisory OHLCV source: `dhan_ohlcv_daily`
- canonical NIFTY benchmark source: parsed `nseindia_indices` `Nifty 50`, synced into `dhan_ohlcv_daily` by `data.benchmark_sync` after index parsing
- canonical fundamentals source: Sharpely tables from `data/sharpelydata/sharpely_data.py`
- official event source: exchange announcement pipeline
- non-official news source: Economic Times RSS
- legacy/reference only: NSE bhavcopy plus local adjusted-price pipeline

Continuous-watch constraints:

- the polling layer is incremental and stateful through `advisory_sync_state`
- it watches only active advisory names, not the full market
- OHLCV polling is based on recent intraday windows, not deep historical loops
- announcements and ET/news are still deduped and persisted through the existing watch pipelines
- the LLM should remain a selective evaluator downstream, not the component that decides raw polling on every cycle

## Production vs Research

Keep two separate Screener.in modes:

- production mode:
  - only recurring setup or theme screeners live in `screenerin_screeners`
  - the daily downloader syncs only those registered screeners
  - use this path for stable advisory universes and historical reruns
- research mode:
  - use `data/screenerin/ad_hoc_query.py` for one-off raw queries
  - do not register every research idea as a recurring screener
  - only promote a query into the registry when it becomes a stable production input

This keeps the daily advisory pipeline smaller and more deterministic without losing exploratory flexibility.

Theme detection and theme-to-screener mapping now use a single active config:

- [`config/investment_themes.yaml`](../config/investment_themes.yaml)

The older fallback theme config was removed.

For event-model data generation there is also a separate research-only training-universe path:

- broad ad hoc Screener.in queries live in [`config/event_model_training_universes.yaml`](../config/event_model_training_universes.yaml)
- they sync directly into normalized `advisory_screener_constituents`
- they feed the research-only setup `EVENT_MODEL_TRAINING_V1`
- normal advisory runs exclude that setup unless it is explicitly selected

## Snapshot policy

The advisory snapshot policy is intentionally simple:

- the screener date is the anchor date
- regime, overlay, technicals, fundamentals, and intraday use the latest available snapshot on or before that anchor date
- each setup decides how stale those inputs are allowed to be through `freshness_policy`
- stale inputs should usually reduce confidence or move a name to `WATCH_*`, not silently drag the whole run back to an older market date

This avoids the old behavior where the whole advisory run could fall back to one old common date just because one snapshot family was lagging.

## Current roadmap summary

The codebase has moved past the earlier multi-screener and overlay build-out. The main open work now is:

1. extend feature dependency and freshness contracts stage-by-stage; the `actions` stage now emits a feature gate and final action consolidation already downgrades positive broker actions to Manual Review when required decision-time inputs are blocked
2. standardize downloader/parser run-state reporting across all ingestion sources
3. add DB schema migration/version tracking before more table/offload changes
4. implement hot/cold retention for old trace and intraday rows
5. continue UI-first operations for remaining manual, research, S3 artifact, and reviewed-config workflows
6. tighten continuous-watch routing with cooldowns, duplicate suppression, and explicit per-source failure counters

LLMs are intentionally kept in:

- structured event extraction
- adversarial review
- company-memory/manual-review context
- playbook action-plan assistance

They are intentionally not the final trade-decision engine.

## Main files

These are the main files you will edit when maintaining the advisory system:

- setup config: [`config/advisory_setups.yaml`](../config/advisory_setups.yaml)
- screener registry: `data/screenerin/screener_registry.py`
- screener sync: `data/screenerin/screener_parser.py`
- advisory screener normalization: `advisory/screener_parser.py`
- technical features: `advisory/technical_features.py`
- experimental time-series forecasts: `advisory/ts_forecast_features.py`
- fundamentals snapshot: `advisory/fundamental_snapshot.py`
- regime engine: `advisory/regime_engine.py`
- rule engine: `advisory/rule_engine.py`
- watchlist builder: `advisory/watchlist_builder.py`
- official announcement watch: `advisory/announcement_watch.py`
- ET RSS ingest: `data/economictimes/rss.py`
- ET RSS watch matching: `advisory/news_watch.py`
- LLM event evaluation: `advisory/llm_event_evaluator.py`
- risk sizing: `advisory/risk_engine.py`
- portfolio planning: `advisory/portfolio_engine.py`
- lifecycle: `advisory/position_lifecycle.py`
- action consolidation: `advisory/action_recommender.py`
- execution planning: `advisory/execution_engine.py`
- decision traces: `advisory/decision_trace.py`
- operator API: `advisory/api/app.py`
- operator frontend: `apps/operator-web`
- research ledger: `advisory/research_ledger.py`
- master orchestrator: `advisory/master_pipeline.py`
- component orchestrator: `advisory/pipeline.py`
- symbol trace utility: `advisory/symbol_trace.py`
- setup trace utility: `advisory/setup_trace.py`
- all-setups dashboard: `advisory/dashboard.py`

## Main tables

These are the core advisory tables to know:

- raw screeners: `screenerin_screeners`, `screenerin_screener_snapshots`
- normalized screener universe: `advisory_screener_constituents`
- price history: `dhan_ohlcv_daily`, `dhan_ohlcv_intraday`
- Sharpely data: `stmt_income`, `stmt_balancesheet`, `stmt_cashflow`, `shareholding_category`, `historical_mcap`, `sharpely_stock_meta`, `sharpely_stock_peers`
- daily snapshots: `advisory_macro_daily`, `advisory_macro_features_daily`, `advisory_exchange_events`, `advisory_exchange_features_daily`, `advisory_technical_daily`, `advisory_fundamentals_daily`, `advisory_market_regime`
- experimental forecast features: `advisory_ts_forecasts_daily`
- experimental forecast evaluation: `advisory_ts_forecast_evaluations`, `advisory_ts_forecast_eval_summary`
- experimental TS watchlist: `advisory_ts_forecast_watchlist`
- rule outputs: `advisory_candidates`, `advisory_candidate_rejections`
- watch layer: `advisory_watchlist`, `advisory_watch_events`, `economictimes_rss_items`, `advisory_news_events`
- LLM/event layer: `advisory_event_evaluations`, `advisory_event_risks`
- event meta-model layer: `advisory_event_model_scores`
- adversarial review layer: `advisory_event_reviews`
- trace/conflict layer: `advisory_decision_traces`, `advisory_decision_trace_steps`, `advisory_event_processing_runs`, `advisory_action_conflicts`, `advisory_action_conflict_rules`
- allocation/execution layer: `advisory_allocations`, `advisory_portfolio_orders`, `advisory_position_lifecycle`, `advisory_rebalance_actions`, `advisory_action_recommendations`, `advisory_execution_orders`, `advisory_execution_fills`

## Macro Feature Layer

`advisory/macro_snapshot.py` remains the point-in-time as-of join over raw CPI, WPI, RBI, FRED/US macro, and G-sec sources. `advisory/macro_features.py` converts that snapshot into `advisory_macro_features_daily`, which is the feature input used by regime classification, event-model training/scoring, and risk sizing.

The first implemented feature groups are:

- inflation pressure: CPI/CFPI 60-day level changes and selected WPI category changes
- rates pressure: RBI repo-rate 90-day change, G-sec 10Y change, and 10Y-2Y curve slope
- global pressure: VIX, US 10Y change, dollar index return, WTI return, and INR/USD move
- data quality: stale and missing macro source counts from snapshot freshness columns
- policy input: `macro_stress_score`, `macro_risk_state`, and `macro_sizing_multiplier`

The feature builder loads a historical lookback window for rolling changes but only persists the requested as-of dates. This keeps single-day advisory runs causal without losing 20/60/90-day macro deltas.

Macro/regime context is also applied at final action consolidation as a one-way risk control. `advisory/action_recommender.py` can reduce positive broker-action sizing in cautious markets, or block `BUY`/`BUY_MORE` into `MANUAL_REVIEW`/`HOLD` during risk-off or weak-breadth regimes. This layer never upgrades a watch/review/hold into a buy.

## Exchange Event Layer

`advisory/exchange_events.py` normalizes NSE block deals, bulk deals, short-selling rows, insider deals, corporate actions, and earnings-calendar rows into `advisory_exchange_events`. It keeps `event_date` and `known_on` separate so later stages can avoid lookahead.

`advisory/exchange_features.py` converts those events into `advisory_exchange_features_daily`. The current feature set includes block/bulk net value, deal clusters, insider net buying/selling, short-selling pressure, upcoming earnings, corporate-action count, and accumulation/distribution scores.

The LLM receives only bounded exchange context: the latest feature row plus a small recent event list for the symbol being evaluated. This helps classify significance and contradiction without sending unfiltered NSE history or giving the LLM trade authority.

## Experimental TS Forecast Layer

`advisory/ts_forecast_features.py` builds research-only time-series forecast features from `dhan_ohlcv_daily` and writes them to `advisory_ts_forecasts_daily`.

The current adapter is `naive_momentum_v1`. It is deliberately simple and dependency-free so it can act as a baseline before TimesFM, Chronos, or Moirai adapters are added.

Current forecast fields include:

- forecast horizon in trading days
- expected return
- forecast price
- downside and upside return quantiles
- probability of positive return
- realized 20-day volatility
- 20-day and 60-day momentum
- signal quality
- `EXPERIMENTAL_*` action hint

Important guardrails:

- TS forecasts do not create `BUY`, `SELL`, or `BUY_MORE` actions today.
- TS forecasts are not sent to Dhan execution.
- Any foundation-model adapter must first beat the simple baseline and existing technical/action flow in walk-forward paper evaluation after costs and slippage.

`advisory/ts_forecast_evaluator.py` evaluates matured forecast rows against future `dhan_ohlcv_daily` returns. It uses the first close strictly after forecast `asof_date` as entry, never the same-day close, and stores a point-in-time return contract with each in-memory evaluation payload. It stores row-level realized results in `advisory_ts_forecast_evaluations` and grouped model/horizon/action-hint metrics in `advisory_ts_forecast_eval_summary`.

`advisory/ts_forecast_workflow.py` ties the research path together: optional Screener.in ad hoc query, Dhan daily OHLCV refresh, forecast generation, and an experimental TS watchlist. If no symbols or query are supplied, it uses `config/ts_forecast_screeners.yaml`.

The Nuxt operator UI shows these rows as TS Watch Recommendations and also shows the latest matured forecast evaluation summary in an Experimental TimesFM Watch section. This is display-only research evidence and does not affect the consolidated action queue.

The dashboard collapses multiple forecast horizons into one symbol-level TS card:

- Swing window: 5-day and 10-day forecasts
- Position window: 20-day forecasts
- Combined state: `ALIGNED_POSITIVE`, `SWING_ONLY`, `POSITION_ONLY`, `MIXED_TS_SIGNAL`, `TS_WEAK`, or `TS_CLOSED`
- Update history: recent forecast changes by date and horizon

If the latest forecast state becomes `TS_CLOSED`, `TS_WEAK`, or `MIXED_TS_SIGNAL`, treat the previous TS research watch as closed until a later positive update reopens it.

The default TS screener is intentionally a candidate generator: liquid enough, profitable enough, and above key moving averages. Cron runs it a few times per weekday, not every watcher tick, because the current TS workflow consumes daily OHLCV. Cron caps the run with `TS_FORECAST_MAX_SYMBOLS` so TimesFM is not accidentally run over the full market. If Screener.in fails, the workflow logs the error and falls back to a capped Dhan/tracked universe.

Manual run:

```sh
python -m advisory.ts_forecast_features --dry-run --symbols RELIANCE TCS
python -m advisory.ts_forecast_features --symbols RELIANCE TCS --horizons 5 10 20
python -m advisory.ts_forecast_features --refresh-ohlcv --symbols RELIANCE TCS --model-name timesfm_2p5_200m --horizons 5 10 20
python -m advisory.ts_forecast_evaluator --dry-run --from-date 2026-04-01 --to-date 2026-04-30
python -m advisory.ts_forecast_evaluator --from-date 2026-04-01 --to-date 2026-04-30 --cost-bps 25
python -m advisory.ts_forecast_paper_portfolio --dry-run --from-date 2026-04-01 --to-date 2026-04-30
python -m advisory.ts_forecast_paper_portfolio --from-date 2026-04-01 --to-date 2026-04-30 --log-research-ledger
python -m advisory.ts_forecast_workflow --symbols RELIANCE TCS --model-name timesfm_2p5_200m
python -m advisory.ts_forecast_workflow --model-name timesfm_2p5_200m
```

`advisory/ts_forecast_paper_portfolio.py` is the promotion gate between forecast rows and any future policy integration. It converts forecast rows into research-only `PAPER_BUY` / `PAPER_SKIP` decisions, evaluates matured outcomes after costs, compares them with a simple momentum baseline, records advisory-action alignment, and can write a research-ledger row. It does not write action recommendations, portfolio rows, or Dhan execution orders.

If the paper-portfolio table is not present yet, the TS forecast promotion-check API returns a blocked research-only payload instead of failing the operator UI. The operator action is to run `all_ts_forecast_paper_portfolio.sh` after forecast/evaluation rows exist.

The Operator home page and generated legacy dashboard show the latest compact TS paper summary: model, horizon, paper decision, evaluated trades, win rate, after-cost average return, momentum baseline average, advisory alignment count, and exit-conflict count. Use it to decide whether the forecast layer deserves a manual promotion review; do not trade directly from it.

`advisory/ts_forecast_promotion_check.py` is the read-only gate for that manual review. It requires enough evaluated paper trades, enough distinct dates and symbols, positive after-cost performance, lift versus the naive momentum baseline, and low conflict with advisory exit actions. A passing result returns `review_candidate`; it still does not change policy or execution. A failing result returns `hold_research_only`.

```sh
python -m advisory.ts_forecast_promotion_check --format json
python -m advisory.ts_forecast_promotion_check --model-name timesfm_2p5_200m --horizon-days 10
```

`advisory/ts_forecast_promotion.py` is the manual review layer after the read-only gate passes. It writes review and operator-decision audit rows plus copyable `ts_forecast_review_rules` guidance. It does not edit config, action rules, portfolio rows, or broker behavior.

```sh
python -m advisory.ts_forecast_promotion --model-name timesfm_2p5_200m --horizon-days 10 --dry-run
```

The Operator API exposes this as `/api/research/ts-forecast-promotion-review`, `/api/research/ts-forecast-promotion-reviews`, and `/api/research/ts-forecast-promotion-review/decision`. Approval records operator intent only; a separate reviewed config-change step is still required before TS forecasts can become low-weight inputs.

After an operator records an approved TS promotion decision, create a preview-only disabled config diff with:

```sh
python -m advisory.config_change_assistant --source-type ts_forecast_review_rule --model-name timesfm_2p5_200m --horizon-days 10 --dry-run
```

The Operator API exposes the same preview at `/api/config-change/ts-forecast-preview`. The generated diff adds a disabled `ts_forecast_review_rules` entry and does not apply policy.

If an operator manually applies that disabled diff, verify what the backend sees with `/api/research/ts-forecast-review-rules`. The endpoint validates required model/horizon fields, unsafe authority values, and broker-execution flags. It is read-only and reports `policy_auto_promotion_allowed=false`. The Operator home TS forecast section shows the same configured-rule counts, statuses, and issues.

The Operator API also exposes `/api/config-change/applications` and `/api/config-change/application-decision` for audit-only reviewed-diff application decisions. Valid decisions are `approved_to_apply`, `marked_applied`, `rejected`, and `needs_more_data`. These rows record operator intent and, for TS forecast review rules, verify the matching disabled config rule when possible. They do not write config files, create action rows, alter portfolio rows, or enable broker execution.

For a dependency-free dry run, use `--model-name naive_momentum_v1`. For TimesFM, install the optional TimesFM torch package first.

Builder setup for TimesFM:

```sh
python builder.py
```

This installs `torch` and the current Google Research TimesFM package from GitHub into the project virtualenv by default. It avoids the older PyPI package path that can pull Pax/Lingvo dependencies. For a lightweight setup without TimesFM, run:

```sh
python builder.py --skip-timesfm-install
```

## Day-to-day commands

Use the project venv:

```sh
python -m ...
```

### Full advisory dry-run

```sh
python -m advisory.master_pipeline --dry-run
```

### Advisory write run

```sh
./all_advisory.sh
python -m advisory.master_pipeline --skip-downloads
```

### Through portfolio only

```sh
python -m advisory.pipeline --dry-run --stop-at portfolio
```

### Specific stages

```sh
python -m advisory.screener_parser
python -m advisory.technical_features
python -m advisory.ts_forecast_features --dry-run --symbols RELIANCE TCS
python -m advisory.ts_forecast_evaluator --dry-run --from-date 2026-04-01 --to-date 2026-04-30
python -m advisory.ts_forecast_workflow --dry-run --symbols RELIANCE TCS --model-name naive_momentum_v1
python -m advisory.fundamental_snapshot
python -m advisory.rule_engine
python -m advisory.watchlist_builder
python -m advisory.announcement_watch
python -m data.economictimes.rss
python -m advisory.news_watch --refresh-feeds
python -m advisory.llm_event_evaluator
python -m advisory.event_meta_model score --dry-run
python -m advisory.adversarial_review
python -m advisory.risk_engine
python -m advisory.portfolio_engine
python -m advisory.position_lifecycle
python -m advisory.execution_engine --dry-run
python -m advisory.symbol_trace HDFCBANK --format text
python -m advisory.setup_trace LARGECAP_BREAKOUT_V1 --format text
python -m advisory.dashboard --format text
```

Portfolio output is a planning state, not a holding state. Rows in `advisory_portfolio_orders` now carry `position_state` and `state_transition_contract_json`; approved or trimmed rows are `PLANNED_ENTRY` and require `advisory.position_lifecycle` to prove the paper entry from the first available close on or after portfolio `published_on`. Deferred rows are `DEFERRED`. Broker execution still requires a separate `advisory.execution_engine --dry-run` preview and the approval/reconciliation/evidence/live-allowance gates below. If a portfolio row is legacy or ambiguous and does not carry `PLANNED_ENTRY` plus a valid transition contract, execution planning creates a `submit_blocked` preview and skips Dhan identity lookup instead of silently treating the row as executable. The Operator Action Queue shows this as a portfolio handoff boundary: current state, required entry-evidence rule, broker-direct block status, and any transition-contract issues are visible before an operator opens raw JSON.

After dry-run execution previews exist, use `/execution-approvals` or `/api/execution/approvals` to inspect the latest `advisory_execution_orders` safety contracts, missing operator approval, missing broker reconciliation, missing live-evidence checklist, and live-submission blockers. The page can record audit-only operator review decisions through `/api/execution/approval-decision`; those rows do not approve execution, mutate execution rows, update safety contracts, reconcile broker state, or submit orders. A second reviewed action, `/api/execution/approval-contract-update`, can mark `operator_approval_status=approved` only when the latest audit decision is `approve_dry_run`; it leaves reconciliation unchanged, forces `live_submission_allowed=false`, and still does not submit broker orders. Broker reconciliation can be previewed or persisted through `/api/execution/reconcile`; preview is dry-run by default, apply requires `confirm=true`, and the route only reads broker order state plus persists reconciliation/fill rows. It does not change operator approval, allow live submission, or submit orders. Live execution also requires the default-on evidence checklist: `live_evidence_status=passed` and at least `STOCKEY_EXECUTION_MIN_EVIDENCE_SUCCESSFUL_RUNS` successful dry-run/reconciliation cycles. Evidence can be previewed or applied through `/api/execution/evidence-review`; confirmed apply writes `advisory_execution_evidence_reviews`, updates only `live_evidence_*`, keeps `live_submission_allowed=false`, and still does not submit orders. Final live allowance can be previewed or applied through `/api/execution/live-allowance`; confirmed apply requires approval, reconciliation, passed evidence, exact phrase, and rationale, writes `advisory_execution_live_allowance_reviews`, and sets only `live_submission_allowed=true`. `/api/execution/live-submit-preflight` and the `/execution-approvals` page can then generate the current order-set token, blockers, required environment, and exact manual CLI command. The preflight is read-only and still does not submit broker orders; live submission remains a separate CLI execution path requiring `STOCKEY_LIVE_TRADING_ENABLED=true` and the current order-set token.

### Regression and cleanup

```sh
python -m pytest tests/test_advisory_regression.py
python scripts/cleanup_deprecated_tables.py --dry-run
```

## Research priorities

The main production priority is not training. The live system should act from structured event extraction, reliability-tracked investor playbooks, macro/regime gates, technical timing, and risk policy. Research remains useful only when it tests whether a model adds incremental value over that knowledge-driven flow.

Focus on:

1. point-in-time discipline across screeners, prices, macro, news, and announcements
2. false-discovery control with a research ledger and leakage-resistant evaluation
3. structured event extraction from messy text
4. deterministic event/playbook action policies with macro-aware risk controls
5. policy separation from interpretation, prediction, and execution

Already implemented:

- a research ledger in `advisory_research_runs`
- opt-in logging from `advisory.pipeline` and `advisory.master_pipeline`
- stored fields for config, as-of date, validation protocol, result metrics, and status
- a formal abstain layer with `candidate_state=ABSTAIN` and `allocation_status=abstained` for low-edge or too-mixed setups
- a richer event tensor in `advisory_event_evaluations` with:
  - `direction`
  - `surprise`
  - `novelty`
  - `contradiction`
  - `expected_decay_days`
  - `source_reliability`
  - `affected_sectors_json`
  - `affected_peers_json`
  - `event_tensor_json`

This keeps the LLM in an extraction role. The stable policy-facing fields like `event_class`, `state_transition_hint`, and `score_impact` still exist for the current watch, risk, and portfolio pipeline.

- a deterministic event-policy layer in `advisory_event_policy_actions` with:
  - `policy_class`
  - `action_type`
  - `action_status`
  - `policy_score`
  - `action_reason`
  - `checks_json`

This layer maps structured event classes into bounded operator actions: `BUY_WATCH`, `MANUAL_REVIEW`, `REDUCE_EXPOSURE_REVIEW`, or `NO_ACTION`. It currently covers order wins, positive/negative/mixed results, growth acceleration, margin expansion, capex, guidance changes, pledge up/down, promoter buying/selling, regulatory notices, management resignations, auditor/governance events, dilution, buybacks, dividends, analyst meets, policy sector events, and neutral corporate actions. These are review/risk overlays only; they do not create broker-executable trades.

Manual-review rows are refined further. Low-information rows are downgraded to `NO_ACTION` deterministically. Remaining `MANUAL_REVIEW` rows can be passed through Codex/LLM, capped by `EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS`, to produce `operator_notes_json` with possible action, future events to wait for, and questions for the operator. The LLM may also downgrade the row to `NO_ACTION` when review is unlikely to produce a useful decision.

The Manual Review UI should not show event-policy rows whose refinement says `final_action_type = NO_ACTION`. It should also suppress generic `advisory_action_recommendations` shadow rows when a detailed event-policy review row exists for the same `unique_id`. This keeps the queue focused on actionable operator decisions rather than duplicate or already-downgraded informational events such as routine analyst meets.

Operator-facing text must be human-readable. Internal enums such as `REGULATORY_NOTICE`, `REDUCE_EXPOSURE_REVIEW`, `MANUAL_REVIEW`, or `event_policy` may remain in raw source rows and trace payloads, but the Manual Review cards should lead with plain English: what happened, why it matters, what to check, and what action boundary applies.

Manual Review operator decisions are intentionally bounded. Closing decisions such as `downgrade_to_no_action` remove the item from the active Manual Review queue but do not change portfolio/action tables or submit trades. `watch_for_event` requires an explicit `Event to wait for`, keeps the item annotated, and creates an active row in `advisory_wait_signals`; the watcher/signal-refresh path can later match that row against fresh news, announcements, and announcement documents.

Use the operator frontend `/wait-signals` page to see active, matched, expired, and closed wait conditions. The page labels Manual Review-created waits separately from playbook-generated waits and shows the latest match evidence when a condition fires. Wait conditions are typed as `price_level`, `event_keywords`, `clarification_filing`, `result_update`, `management_commentary`, `sector_event`, or `expiry_only`; unknown condition types are surfaced as issues rather than hidden failures. Matched wait signals are evidence for review or signal refresh; they are not broker-executable trades by themselves. In signal refresh, negative wait matches become reduce-review signals, positive wait matches become WATCH, and ambiguous wait matches become MANUAL_REVIEW.

The operator frontend `/events` page exposes this layer directly. Use it to review action counts by class, inspect policy checks, read LLM/operator notes, and open the decision trace for the underlying event.

- a deterministic adversarial reviewer in `advisory_event_reviews` with:
  - `review_action`
  - `review_score`
  - `veto`
  - `review_reason`
  - `review_flags_json`
  - `feature_snapshot_json`

This reviewer can only clear, penalize, force manual review, or veto. It does not create bullish signals on its own.

- a train/score scaffold in `advisory.event_meta_model` that:
  - builds leakage-safe labels from point-in-time event rows plus later `dhan_ohlcv_daily` closes
  - joins intraday, macro, and exchange features on the event published-on date, not the future label anchor date
  - keeps first-trading-day forward returns as labels only and records the point-in-time contract in model metadata
  - trains an XGBoost classifier on sign-adjusted forward returns
  - stores current event scores in `advisory_event_model_scores`

This model is a research and scoring aid. It is not auto-trained inside the daily advisory pipeline.

## Optional model training prerequisites

The production advisory path does not require event-model training. Use this section only for research experiments or to test whether a tabular event model adds incremental value over playbooks, deterministic event policies, macro gates, and technical timing.

Persisted event-model scores are also research-only by default. Adversarial review ignores `advisory_event_model_scores` unless `STOCKEY_EVENT_MODEL_SCORE_POLICY_MODE=promoted` or `--event-model-score-policy-mode promoted` is set, and even then it fails closed unless `advisory.event_model_promotion_check` returns a usable scorecard. The promotion check also fails closed when model metadata lacks leakage-control, false-discovery-control, or transaction-cost-adjusted baseline evidence.

Event-model training writes those research controls into artifact metadata. Leakage control records the event-day feature cutoff and future-return label contract. False-discovery control records that the trainer used one fixed model configuration and one threshold rather than an automated sweep. Cost-adjusted baseline evidence compares predicted-positive holdout trades against a passive event baseline after `--cost-bps` transaction costs; this gate fails unless the model beats that after-cost baseline.

Use the prep command first:

```sh
python -m advisory.event_model_data_prep --format text
```

That command is the intended operator path for model-training preparation. It:

1. fills missing normalized screener dates from already stored Screener snapshots
2. reruns historical advisory rules -> watch -> evaluate -> review over those dates
3. refreshes daily OHLCV for symbols already present in `advisory_event_evaluations`
4. reports label coverage by horizon so you can decide whether training is justified
5. syncs broad ad hoc training universes for the current date when you are not running a historical-only window

Runtime constraints are intentional:

- historical model-backfill dates do not try to repair old daily or fundamental snapshot gaps on the fly
- historical model-backfill dates do not prefetch old intraday features on the fly
- same-day or very recent production-style runs can still repair missing snapshots when needed
- the goal is to avoid burning time on low-value historical rehydration while keeping current advisory runs strict

Useful variants:

```sh
python -m advisory.event_model_data_prep --from-date 2026-03-01 --to-date 2026-04-01 --format text
python -m advisory.event_model_data_prep --skip-event-backfill --skip-price-refresh --dry-run --format text
```

Do not train the event meta-model until all of these are true:

1. `advisory_event_evaluations` has enough historical rows across multiple dates and setups.
2. The evaluated symbols have enough fresh `dhan_ohlcv_daily` rows to cover the chosen forward-return horizon.
3. The labeled dataset has enough non-null `target_label` rows to justify training.
4. The research ledger records the config, horizon, date range, and validation protocol.

Practical operator checklist:

```sh
python scripts/sql_query_runner.py --read-only "select date(published_on) as published_date, count(*) as eval_count from advisory_event_evaluations group by 1 order by 1"
python scripts/sql_query_runner.py --read-only "select max(date) as max_price_date from dhan_ohlcv_daily"
python -m advisory.event_meta_model train --horizon-days 1 --cost-bps 25
python -m advisory.event_meta_model score --dry-run
```

The right order is:

1. backfill and evaluate more historical announcements/news
2. refresh daily OHLCV for the evaluated symbols
3. train first on the shortest viable horizon, usually `1d`
4. widen to `3d`, `5d`, or longer horizons only after the label count supports it

Promotion gate:

Keep `all_ml.sh` as research evidence until all of these hold for several weekly runs:

1. Label coverage is broad enough across dates, sectors, event classes, and market regimes; avoid judging the model from one cluster of similar events.
2. Out-of-sample or walk-forward results beat simple baselines after costs, including passive benchmark, current deterministic event policy, and naive momentum.
3. The model improves precision in the top score buckets without increasing false positives in low-liquidity or stale-evidence names.
4. Results remain stable across at least `1d`, `5d`, and `10d` horizons, or the model is explicitly scoped to only the horizon where it works.
5. Feature importance and examples are explainable enough for operator review; no single leaky timestamp, source, or symbol artifact should dominate.
6. The research ledger records config, train/test dates, validation protocol, costs, baseline comparison, and known failure cases.
7. The model is first promoted only as a low-weight input into review/risk/action context, not as direct buy/sell authority.

If any of these fail, use the weekly run only to improve extraction quality, label coverage, feature design, and evaluation discipline.

Use the read-only gate command after weekly runs:

```sh
python -m advisory.event_model_promotion_check
python -m advisory.event_model_promotion_check --format json
```

The command returns `review_candidate` only when artifact metrics, label diversity, recent score freshness, and repeated successful weekly runs all pass the configured thresholds. A passing result means “review for possible low-weight integration”; it does not mean “auto-promote”.

After successful training, `all_ml.sh` uploads the trained model artifact, metadata, and manifest to S3-compatible storage. This is backup/reproducibility only; it does not promote the model into live policy.

Useful controls:

```sh
EVENT_MODEL_ARTIFACT_UPLOAD_ENABLED=false ./all_ml.sh
./all_ml.sh --skip-s3-upload
python -m advisory.event_model_artifact_store --dry-run
```

Default prefix: `models/advisory_event_meta_model`, configurable with `EVENT_MODEL_ARTIFACT_S3_PREFIX` or `--s3-prefix`.

Useful commands:

```sh
python -m advisory.research_ledger --limit 20
python -m advisory.pipeline --dry-run --log-research-ledger --ledger-label "baseline-v1"
python -m advisory.master_pipeline --dry-run --log-research-ledger --ledger-label "full-run-v1"
```

Deprioritized:

- multi-agent debate systems
- LLM-led trading decisions
- free-form research orchestration inside the production runtime

## How to add a new screener

The system currently uses Screener.in as the active advisory screener source for recurring production universes.
All Screener.in fetches use logged-in mode through the configured Chrome CDP session. Set `CDP_ENDPOINT`, `SCREENER_IN_LOGIN`, and `SCREENER_IN_PASSWORD`, then use `python -m data.screenerin.auth --check` or `python -m data.screenerin.auth` when debugging login state.

For one-off exploration, prefer ad hoc queries first:

```sh
python -m data.screenerin.ad_hoc_query --name "Deep Value ROCE" --query "Market capitalization > 500 AND Price to earning < 15 AND Return on capital employed > 22%"
```

Only use the registry path below when the screener should become a recurring production input.

### Step 1: add the URL to the registry

Example:

```sh
python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-new-screen/"
```

Optional display name:

```sh
python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-new-screen/" --name "My New Screen"
```

List registered screeners:

```sh
python -m data.screenerin.screener_registry list
```

Inspect latest rows:

```sh
python -m data.screenerin.screener_registry latest --screener my-new-screen
```

Remove a screener:

```sh
python -m data.screenerin.screener_registry remove my-new-screen
```

### Step 2: sync the raw production screener snapshot

```sh
python -m data.screenerin.screener_parser
```

### Step 3: normalize into the advisory universe

```sh
python -m advisory.screener_parser
```

### Step 4: verify the results

```sh
python scripts/sql_query_runner.py --read-only "select date, screener_slug, count(*) as row_count from advisory_screener_constituents group by 1,2 order by 1 desc,2"
```

## How to add or change a setup

Setups are configured in [`config/advisory_setups.yaml`](../config/advisory_setups.yaml).

Each setup supports:

- `setup_id`
- `setup_name`
- `screeners`
- `screener_mode`
- `allowed_regimes`
- `allowed_overlays`
- `blocked_overlays`
- `market_cap_min`
- `market_cap_max`
- `min_avg_traded_value_20d`
- `max_breakout_extension_pct`
- `min_dist_52w_high`
- `technical_rules`
- `intraday_rules`
- `fundamental_rules`
- `watch_reasons`

### Example pattern

```yaml
- setup_id: MY_SETUP_V1
  setup_name: My setup
  screeners:
    - my-new-screen
  screener_mode: union
  allowed_regimes:
    - STABLE
    - BULL_NARROW
  allowed_overlays:
    - NONE
  market_cap_min: 5000
  min_avg_traded_value_20d: 100000000
  max_breakout_extension_pct: 8
  min_dist_52w_high: -15
  technical_rules:
    - column: pass_above_dma_50
      operator: eq
      value: true
    - column: rs_vs_benchmark
      operator: gte
      value: 0.0
  intraday_rules:
    - column: intraday_close_vs_vwap_pct
      operator: gte
      value: 0.0
  fundamental_rules:
    - column: debt_to_equity_vs_sector
      operator: lte
      value: 0.2
  watch_reasons:
    - earnings
    - order wins
```

## Intraday feature layer

The advisory pipeline now has a separate intraday feature stage:

```sh
python -m advisory.intraday_features --date 2026-04-01
python -m advisory.intraday_features --date 2026-04-01 --intervals 1 5 15
```

What it does:

- syncs missing Dhan intraday history on demand for the active screener universe
- supports Dhan candle intervals `1`, `5`, `15`, `25`, and `60` minutes
- stores raw bars in `dhan_ohlcv_intraday`
- stores derived daily intraday pattern features in `advisory_intraday_features_daily`
- exposes columns such as:
  - `intraday_close_vs_vwap_pct`
  - `intraday_pct_bars_above_vwap`
  - `intraday_close_location_pct`
  - `intraday_opening_range_breakout_up`
  - `intraday_prev_day_breakout_up`
  - `intraday_failed_prev_day_breakout`
  - `intraday_volume_vs_20d`
  - `intraday_breakout_score`
  - `intraday_pattern_label`

These fields are optional setup inputs through `intraday_rules`. They are designed to support later pattern-model work such as XGBoost-based breakout confirmation without forcing that model path into the first rollout.

## ML roadmap

The ML path for advisory intraday confirmation should be feature-first, not raw-bar-first.

Recommended sequence:

1. persist and validate deterministic intraday features
2. build labeled outcomes from later daily follow-through or failure
3. train a separate breakout-confirmation model, likely XGBoost, on those persisted features
4. write `model_name` and `model_score` back into `advisory_intraday_features_daily`
5. use the model score as a soft scoring input inside the rule engine

Do not wire an uncalibrated model directly into pass/reject logic. The deterministic intraday features should remain readable and usable even if the ML layer is disabled.

### Supported operators

The rule engine currently supports:

- `eq`
- `gte`
- `gt`
- `lte`
- `lt`

These are implemented in `advisory/rule_engine.py`.

### How to choose rule columns

Use columns that actually exist in:

- `advisory/technical_features.py`
- `advisory/fundamental_snapshot.py`

Typical technical columns:

- `pass_above_dma_20`
- `pass_above_dma_50`
- `pass_above_dma_200`
- `atr_compression_pct`
- `avg_traded_value_20d`
- `rs_vs_benchmark`
- `rs_vs_sector`
- `breakout_extension_pct`
- `dist_52w_high`

### Technical Threshold Calibration

Technical-engine defaults are not promoted automatically. Use the calibration module to compare threshold grids against realized point-in-time forward returns from `dhan_ohlcv_daily`:

```sh
python -m advisory.technical_threshold_calibration --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20 --max-configs 512 --progress-every 128
python -m advisory.technical_threshold_calibration --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
python -m advisory.technical_threshold_calibration --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
```

Use `--max-configs` for first-pass diagnosis when investigating missing BUYs; omit it for the full grid. Bounded runs are marked `bounded_first_pass_only` / `diagnostic_only_no_promotion` and are not eligible for threshold promotion. The command emits progress to stderr so long runs do not look stuck. Persisted full-grid runs write `advisory_technical_threshold_evaluations` and `advisory_technical_threshold_eval_summary`. Treat these as research evidence only; update live setup thresholds manually after checking sample size, hit rate after costs, average return after costs, and spread versus rejected candidates.

The summary `archetype_breakdown_json` groups outcomes by setup type. New candidate rows should carry `technical_setup_archetype`; older rows are assigned a research-only effective bucket from `technical_trigger_type`, `candidate_state`, `technical_state`, and `setup_id`. Each breakdown row states whether the bucket was `explicit` or `inferred`. Inferred buckets are diagnostic labels only and do not change live scoring, portfolio policy, or broker behavior.

Event-policy classes are evaluated separately, also as research-only evidence:

```sh
python -m advisory.event_policy_evaluator --dry-run --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
python -m advisory.event_policy_evaluator --from-date 2026-01-01 --to-date 2026-05-01 --horizons 5 10 20
```

It writes row-level realized checks to `advisory_event_policy_evaluations` and grouped metrics to `advisory_event_policy_eval_summary` by action type, policy class, event class, score bucket, confidence bucket, and combined groups. Treat `candidate_policy_strengthen` and `candidate_policy_tighten_or_downgrade` as review prompts only; they do not change live event-policy thresholds automatically.

Action-conflict rules can be re-applied after editing `advisory_action_conflict_rules`:

```sh
python -m advisory.action_conflict_resolver --dry-run --format json
python -m advisory.action_conflict_resolver --format json
python -m advisory.action_conflict_resolver --symbol BSE --unresolved-only --format text
```

This updates `advisory_action_conflicts` resolution fields and buckets unresolved combinations for manual resolution. It does not yet change action ranking; live winners still come from `advisory.action_recommender`.

Resolved action conflicts are intentionally not sent to Manual Review. Manual Review is an action-required queue and should only include conflicts where `requires_manual_resolution = true` or `resolution_status` is `unresolved` / `manual_required`. Resolved conflicts remain available for audit/debug through Decision Trace, symbol detail pages, and the operator Conflict Rules page.

Repeated unresolved conflict rows for the same symbol/action pair are collapsed in the Manual Review API to the newest active item, with `summary.duplicate_suppressed` reporting how many duplicate queue rows were hidden. Closed operator decisions are also matched by canonical conflict identity, so the same symbol/action conflict does not re-enter active Manual Review just because the source row has a new transient key.

Ask Codex/LLM for a manual promotion review after choosing a candidate config:

```sh
python -m advisory.technical_threshold_promotion --setup-id EVENT_OPPORTUNITY_V1 --config-id CONFIG_ID --dry-run
```

The review writes to `advisory_technical_threshold_promotion_reviews` when not run with `--dry-run`. It produces reviewer rationale and a pending patch payload for `config/advisory_setups.yaml`, but it never applies threshold changes automatically.

The operator frontend `/technical-calibration` page also shows recent promotion reviews. Use its `Approve`, `Needs Data`, or `Reject` controls to write an audit-only decision row to `advisory_technical_threshold_promotion_decisions`. `Approve` means “accepted for manual config editing”; it still does not edit the YAML file or affect live advisory behavior.

Typical fundamental columns:

- `total_revenue_qoq_growth_vs_sector`
- `ebitda_qoq_growth_vs_sector`
- `profit_after_tax_qoq_growth_vs_sector`
- `debt_to_equity_vs_sector`
- `promoter_total_vs_sector`
- `fii_vs_sector`

### After changing a setup

Run:

```sh
python -m advisory.rule_engine --dry-run
```

If the output looks correct:

```sh
python -m advisory.rule_engine
python -m advisory.watchlist_builder
```

## How to inspect why a stock passed or failed

The fastest way is:

```sh
python -m advisory.symbol_trace HDFCBANK --format text
```

### Passed candidates

```sh
python scripts/sql_query_runner.py --read-only "select * from advisory_candidates order by asof_date desc, setup_id, symbol limit 50"
```

### Rejections

```sh
python scripts/sql_query_runner.py --read-only "select * from advisory_candidate_rejections order by asof_date desc, setup_id, symbol limit 100"
```

### Common rejection reasons

- `regime_not_allowed`
- `liquidity_below_min`
- `market_cap_below_min`
- `market_cap_above_max`
- `technical_<column>`
- `fundamental_<column>`
- `missing_company_master_id`

## How missing data is handled

The advisory stack checks the database first, then fetches missing data.

Current behavior:

- missing OHLCV: fetched from Dhan
- missing fundamentals or stock meta: fetched from Sharpely
- peer data needed for peer-relative features: fetched through Sharpely plus Dhan peer sync

The main preflight logic is in `advisory/data_sync.py` and `advisory/peer_sync.py`.

## How news and events work

There are now two event paths.

### 1. Official exchange announcements

- watch builder: `advisory/watchlist_builder.py`
- event ingest: `advisory/announcement_watch.py`
- upstream storage: `announcement_pipeline_documents`
- advisory event table: `advisory_watch_events`
- OCR, concise document summaries, and structured report extraction: Codex CLI when `OCR_USING=codex` and `SUMMARIZE_WITH=codex`

Set `CODEX_CLI_OCR_MODEL` and `CODEX_CLI_SUMMARIZE_MODEL` to choose the smaller Codex model used for these document tasks.

### 2. Economic Times RSS

- raw ingest: `data/economictimes/rss.py`
- watch matching: `advisory/news_watch.py`
- raw table: `economictimes_rss_items`
- advisory event table: `advisory_news_events`

### LLM evaluation

`advisory/llm_event_evaluator.py` reads both event sources and writes:

- `advisory_event_evaluations`
- `advisory_event_risks`

For official filings, the evaluator reads `advisory_announcement_evidence` first so Codex/LLM context is compact, point-in-time, and source-attributed. It falls back to `announcement_pipeline_documents` only when a compact evidence row is missing, and that fallback is marked in the persisted context snapshot. The same payload also carries latest compact bhavcopy evidence from `advisory_bhavcopy_evidence_daily` when available.

Use `ADVISORY_EVENT_EVAL_MODEL=codex` and `CODEX_CLI_EVENT_MODEL` to run this structured event evaluation through Codex CLI with local Pydantic validation.

Company-memory review is a separate review-only signal layer. `advisory/company_memory_review.py` writes `advisory_company_memory_reviews` by combining compact announcement evidence, compact bhavcopy evidence, current technical/candidate state, recent event-policy rows, wait signals, and latest action rows for each symbol. It defaults to deterministic V1 and only calls Codex when explicitly enabled. Its `authority_scope` is always `review_input_only`; deterministic action consolidation and execution safety gates remain authoritative.

```sh
python -m advisory.company_memory_review --dry-run --symbols RELIANCE --limit 1
python -m advisory.company_memory_review --dry-run --symbols RELIANCE --llm
```

The operator API enriches visible action rows with the latest matching `company_memory_review`. The Nuxt Action Queue and Symbol Detail pages render the review signal, confidence, thesis, evidence used, risk flags, wait-for items, and `review_input_only` authority boundary. This is display evidence only; it does not approve broker actions.

Consolidated action decisions can write manual revision pointers through Codex CLI:

- set `ACTION_MANUAL_REVISION_POINTERS_ENABLED=true`
- set `ACTION_MANUAL_REVISION_POINTERS_MODEL=codex` or `codex:<model>`
- read `manual_revision_summary` and `manual_revision_pointers_json` from `advisory_action_recommendations`

These pointers are for operator review only. They do not change the final action and do not grant execution authority.

Action consolidation also writes a reason contract:

- `recommendation_reason_json`: machine-readable explanation of source, primary reason, evidence sections, risk fields, and competing candidates
- `reason_contract_status`: `complete` or `incomplete_downgraded`
- incomplete broker-action contracts are downgraded to `MANUAL_REVIEW`
- execution planning blocks any stale broker-action row whose reason contract is missing or not `complete`
- the operator frontend renders the reason contract as separate screener, technical, event, playbook, macro/regime, risk, and competing-candidate panels on action cards and decision traces
- before validation, action consolidation enriches candidate raw context from the latest matching `advisory_candidates` row and the latest `advisory_market_regime` row, so screener, technical, setup-score, and macro/regime evidence are available even when the immediate source table is sparse
- before ranking, action consolidation applies latest top-context market summary and macro/regime context as a risk-reduction-only adjustment. Positive broker actions can be resized or downgraded, but never upgraded.
- action consolidation also enriches event/playbook decisions from latest matching `advisory_event_evaluations`, `advisory_event_reviews`, and `advisory_playbook_action_plans`, including event id, event class, verdict, reviewer action/veto, playbook id, and review checks

### 3. Investor playbooks

- versioned config: `config/hypotheses.yaml`
- import command: `python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml`
- dry run: `python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml --dry-run`
- scan command: `python -m advisory.hypothesis_engine --run-scan`

Playbook status controls action-overlay behavior:

- `draft`: stored but not scanned by the active run
- `active_review`: scanned and action-planned, but does not affect action consolidation
- `trusted_overlay`: can become a `review_only` risk overlay in `advisory_action_recommendations`
- `retired`: ignored

## Recommended operating flows

### Daily market run

```sh
./all_advisory.sh
python -m advisory.master_pipeline --skip-downloads
```

### If you only changed rules

```sh
python -m advisory.rule_engine
python -m advisory.watchlist_builder
```

### If you only changed screeners

```sh
python -m data.screenerin.screener_parser
python -m advisory.screener_parser
python -m advisory.rule_engine
```

### If you want event processing only

```sh
python -m advisory.announcement_watch
python -m data.economictimes.rss
python -m advisory.news_watch
python -m advisory.llm_event_evaluator
```

## Troubleshooting

### Dhan token problems

Use:

```sh
python -m data.dhanlive.auth_cli status
python -m data.dhanlive.auth_cli refresh --clear-cache-first
python -m data.dhanlive.auth_cli refresh --clear-cache-first --auto-login
python -m data.dhanlive.auth_cli validate
```

If `CDP_ENDPOINT`, `DHAN_LOGIN_MOBILE`, `DHAN_TOTP_SECRET`, and `DHAN_LOGIN_PIN` are configured, the regular Dhan clients use the automated Playwright login automatically when the cached access token is missing or expired. Chrome/CDP is a required dependency for this path; missing or unreachable Chrome fails the preflight and advisory run. Use `DHAN_AUTO_LOGIN_STEP_TIMEOUT_MS` only to allow slower Dhan mobile, TOTP, PIN, or redirect transitions.

### Screener looks empty

Check:

1. the Screener.in URL still works
2. the slug is registered in `screenerin_screeners`
3. raw rows exist in `screenerin_screener_snapshots`
4. normalized rows exist in `advisory_screener_constituents`

### Rule engine returns zero candidates

Check:

1. current regime in `advisory_market_regime`
2. latest screener universe size
3. top rejection reasons in `advisory_candidate_rejections`
4. whether OHLCV and fundamentals were available for the symbols

### News matching returns zero rows

Check:

1. `economictimes_rss_items` has fresh rows
2. the watchlist is non-empty
3. the stock symbol or company name appears in the ET title/description

### Event evaluator errors

Check:

1. `OPENAI_API_KEY`
2. input rows exist in `advisory_watch_events` or `advisory_news_events`
3. the event is not already evaluated unless you use `--include-evaluated`

## Change discipline

When you change the advisory system:

1. update code
2. update this manual if the operator workflow changed
3. update [`todo.md`](../todo.md) if implementation status changed
4. run:

```sh
python -m pytest tests/test_advisory_regression.py
```

5. run at least one relevant dry-run command for the changed stage
