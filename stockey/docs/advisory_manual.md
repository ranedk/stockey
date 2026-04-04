# Advisory Manual

This is the practical operator and maintenance guide for the investment advisory system.

Use this document when you want to:

- run the advisory stack end to end
- run one stage manually
- add or change Screener.in screeners
- add or change setup rules
- understand which module owns which behavior
- inspect outputs and debug failures

Use [`docs/implementation.md`](/home/rane/code/stockey/docs/implementation.md) for the target strategy spec. Use [`todo.md`](/home/rane/code/stockey/todo.md) for current implementation status and remaining gaps.

For common edits with copy-paste commands, use [`docs/advisory_change_cookbook.md`](/home/rane/code/stockey/docs/advisory_change_cookbook.md).

## Core design

The advisory runtime path is:

1. Screener.in universe
2. Dhan OHLCV
3. Sharpely fundamentals and peers
4. Macro snapshot
5. Regime classification
6. Rule engine
7. Watchlist
8. Official announcements and ET RSS news
9. LLM event evaluation
10. Event meta-model scoring
11. Adversarial event review
12. Risk sizing
13. Portfolio planning
14. Lifecycle tracking
15. Execution planning

Important runtime decisions:

- canonical advisory OHLCV source: `dhan_ohlcv_daily`
- canonical fundamentals source: Sharpely tables from [`data/sharpelydata/sharpely_data.py`](/home/rane/code/stockey/data/sharpelydata/sharpely_data.py)
- official event source: exchange announcement pipeline
- non-official news source: Economic Times RSS
- legacy/reference only: NSE bhavcopy plus local adjusted-price pipeline

## Production vs Research

Keep two separate Screener.in modes:

- production mode:
  - only recurring setup or theme screeners live in `screenerin_screeners`
  - the daily downloader syncs only those registered screeners
  - use this path for stable advisory universes and historical reruns
- research mode:
  - use [`data/screenerin/ad_hoc_query.py`](/home/rane/code/stockey/data/screenerin/ad_hoc_query.py) for one-off raw queries
  - do not register every research idea as a recurring screener
  - only promote a query into the registry when it becomes a stable production input

This keeps the daily advisory pipeline smaller and more deterministic without losing exploratory flexibility.

Theme detection and theme-to-screener mapping now use a single active config:

- [`config/investment_themes.yaml`](/home/rane/code/stockey/config/investment_themes.yaml)

The older fallback theme config was removed.

## Snapshot policy

The advisory system now uses a simple snapshot policy:

- `advisory_screener_constituents.date` is the anchor date for a run
- regime and overlay use the latest available row on or before that date
- technicals, fundamentals, and intraday features use the latest available per-symbol row on or before that date
- setup-level `freshness_policy` decides how stale each input is allowed to be

This is intentional. The system no longer rolls the entire advisory run back to the oldest common snapshot date across all datasets.

## Latest update

The latest advisory change added an intraday confirmation layer on top of the daily setup engine.

- the base advisory logic is still daily-first
- intraday candles are now pulled on demand from Dhan for the active screener universe
- derived daily intraday features are persisted in `advisory_intraday_features_daily`
- setups can reference those fields with `intraday_rules`
- this is intended as a confirmation layer for breakouts and event setups, not as a full intraday trading engine

## Snapshot policy

The advisory snapshot policy is intentionally simple:

- the screener date is the anchor date
- regime, overlay, technicals, fundamentals, and intraday use the latest available snapshot on or before that anchor date
- each setup decides how stale those inputs are allowed to be through `freshness_policy`
- stale inputs should usually reduce confidence or move a name to `WATCH_*`, not silently drag the whole run back to an older market date

This avoids the old behavior where the whole advisory run could fall back to one old common date just because one snapshot family was lagging.

## Main files

These are the main files you will edit when maintaining the advisory system:

- setup config: [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml)
- screener registry: [`data/screenerin/screener_registry.py`](/home/rane/code/stockey/data/screenerin/screener_registry.py)
- screener sync: [`data/screenerin/screener_parser.py`](/home/rane/code/stockey/data/screenerin/screener_parser.py)
- advisory screener normalization: [`advisory/screener_parser.py`](/home/rane/code/stockey/advisory/screener_parser.py)
- technical features: [`advisory/technical_features.py`](/home/rane/code/stockey/advisory/technical_features.py)
- fundamentals snapshot: [`advisory/fundamental_snapshot.py`](/home/rane/code/stockey/advisory/fundamental_snapshot.py)
- regime engine: [`advisory/regime_engine.py`](/home/rane/code/stockey/advisory/regime_engine.py)
- rule engine: [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py)
- watchlist builder: [`advisory/watchlist_builder.py`](/home/rane/code/stockey/advisory/watchlist_builder.py)
- official announcement watch: [`advisory/announcement_watch.py`](/home/rane/code/stockey/advisory/announcement_watch.py)
- ET RSS ingest: [`data/economictimes/rss.py`](/home/rane/code/stockey/data/economictimes/rss.py)
- ET RSS watch matching: [`advisory/news_watch.py`](/home/rane/code/stockey/advisory/news_watch.py)
- LLM event evaluation: [`advisory/llm_event_evaluator.py`](/home/rane/code/stockey/advisory/llm_event_evaluator.py)
- risk sizing: [`advisory/risk_engine.py`](/home/rane/code/stockey/advisory/risk_engine.py)
- portfolio planning: [`advisory/portfolio_engine.py`](/home/rane/code/stockey/advisory/portfolio_engine.py)
- lifecycle: [`advisory/position_lifecycle.py`](/home/rane/code/stockey/advisory/position_lifecycle.py)
- execution planning: [`advisory/execution_engine.py`](/home/rane/code/stockey/advisory/execution_engine.py)
- research ledger: [`advisory/research_ledger.py`](/home/rane/code/stockey/advisory/research_ledger.py)
- master orchestrator: [`advisory/master_pipeline.py`](/home/rane/code/stockey/advisory/master_pipeline.py)
- component orchestrator: [`advisory/pipeline.py`](/home/rane/code/stockey/advisory/pipeline.py)
- symbol trace utility: [`advisory/symbol_trace.py`](/home/rane/code/stockey/advisory/symbol_trace.py)
- setup trace utility: [`advisory/setup_trace.py`](/home/rane/code/stockey/advisory/setup_trace.py)
- all-setups dashboard: [`advisory/dashboard.py`](/home/rane/code/stockey/advisory/dashboard.py)

## Main tables

These are the core advisory tables to know:

- raw screeners: `screenerin_screeners`, `screenerin_screener_snapshots`
- normalized screener universe: `advisory_screener_constituents`
- price history: `dhan_ohlcv_daily`, `dhan_ohlcv_intraday`
- Sharpely data: `stmt_income`, `stmt_balancesheet`, `stmt_cashflow`, `shareholding_category`, `historical_mcap`, `sharpely_stock_meta`, `sharpely_stock_peers`
- daily snapshots: `advisory_macro_daily`, `advisory_technical_daily`, `advisory_fundamentals_daily`, `advisory_market_regime`
- rule outputs: `advisory_candidates`, `advisory_candidate_rejections`
- watch layer: `advisory_watchlist`, `advisory_watch_events`, `economictimes_rss_items`, `advisory_news_events`
- LLM/event layer: `advisory_event_evaluations`, `advisory_event_risks`
- event meta-model layer: `advisory_event_model_scores`
- adversarial review layer: `advisory_event_reviews`
- allocation/execution layer: `advisory_allocations`, `advisory_portfolio_orders`, `advisory_position_lifecycle`, `advisory_rebalance_actions`, `advisory_execution_orders`, `advisory_execution_fills`

## Day-to-day commands

Use the project venv:

```sh
python -m ...
```

### Full advisory dry-run

```sh
python -m advisory.master_pipeline --dry-run
```

### Full advisory write run

```sh
./all_advisory.sh
python -m advisory.master_pipeline
```

### Through portfolio only

```sh
python -m advisory.pipeline --dry-run --stop-at portfolio
```

### Specific stages

```sh
python -m advisory.screener_parser
python -m advisory.technical_features
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

### Regression and cleanup

```sh
python -m pytest tests/test_advisory_regression.py
python scripts/cleanup_deprecated_tables.py --dry-run
```

## Research priorities

The main research priority is validation, not more agent orchestration.

Focus on:

1. point-in-time discipline across screeners, prices, macro, news, and announcements
2. false-discovery control with a research ledger and leakage-resistant evaluation
3. structured event extraction from messy text
4. tabular alpha models with abstention
5. policy separation from prediction and execution

First concrete implementation now available:

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
  - trains an XGBoost classifier on sign-adjusted forward returns
  - stores current event scores in `advisory_event_model_scores`

This model is a research and scoring aid. It is not auto-trained inside the daily advisory pipeline.

## Model training prerequisites

Do not train the event meta-model until all of these are true:

1. `advisory_event_evaluations` has enough historical rows across multiple dates and setups.
2. The evaluated symbols have enough fresh `dhan_ohlcv_daily` rows to cover the chosen forward-return horizon.
3. The labeled dataset has enough non-null `target_label` rows to justify training.
4. The research ledger records the config, horizon, date range, and validation protocol.

Practical operator checklist:

```sh
python scripts/sql_query_runner.py --read-only "select date(published_on) as published_date, count(*) as eval_count from advisory_event_evaluations group by 1 order by 1"
python scripts/sql_query_runner.py --read-only "select max(date) as max_price_date from dhan_ohlcv_daily"
python -m advisory.event_meta_model train --horizon-days 1
python -m advisory.event_meta_model score --dry-run
```

The right order is:

1. backfill and evaluate more historical announcements/news
2. refresh daily OHLCV for the evaluated symbols
3. train first on the shortest viable horizon, usually `1d`
4. widen to `3d`, `5d`, or longer horizons only after the label count supports it

Useful commands:

```sh
python -m advisory.research_ledger --limit 20
python -m advisory.pipeline --dry-run --log-research-ledger --ledger-label "baseline-v1"
python -m advisory.master_pipeline --dry-run --log-research-ledger --ledger-label "full-run-v1"
```

Deprioritized for now:

- multi-agent debate systems
- LLM-led trading decisions
- free-form research orchestration inside the production runtime

## How to add a new screener

The system currently uses Screener.in as the active advisory screener source for recurring production universes.

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

Setups are configured in [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml).

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

These are implemented in [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py).

### How to choose rule columns

Use columns that actually exist in:

- [`advisory_technical_daily`](/home/rane/code/stockey/advisory/technical_features.py)
- [`advisory_fundamentals_daily`](/home/rane/code/stockey/advisory/fundamental_snapshot.py)

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

The main preflight logic is in [`advisory/data_sync.py`](/home/rane/code/stockey/advisory/data_sync.py) and [`advisory/peer_sync.py`](/home/rane/code/stockey/advisory/peer_sync.py).

## How news and events work

There are now two event paths.

### 1. Official exchange announcements

- watch builder: [`advisory/watchlist_builder.py`](/home/rane/code/stockey/advisory/watchlist_builder.py)
- event ingest: [`advisory/announcement_watch.py`](/home/rane/code/stockey/advisory/announcement_watch.py)
- upstream storage: `announcement_pipeline_documents`
- advisory event table: `advisory_watch_events`

### 2. Economic Times RSS

- raw ingest: [`data/economictimes/rss.py`](/home/rane/code/stockey/data/economictimes/rss.py)
- watch matching: [`advisory/news_watch.py`](/home/rane/code/stockey/advisory/news_watch.py)
- raw table: `economictimes_rss_items`
- advisory event table: `advisory_news_events`

### LLM evaluation

[`advisory/llm_event_evaluator.py`](/home/rane/code/stockey/advisory/llm_event_evaluator.py) reads both event sources and writes:

- `advisory_event_evaluations`
- `advisory_event_risks`

## Recommended operating flows

### Daily market run

```sh
./all_advisory.sh
python -m advisory.master_pipeline
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
python -m data.dhanlive.auth_cli validate
```

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
3. update [`todo.md`](/home/rane/code/stockey/todo.md) if implementation status changed
4. run:

```sh
python -m pytest tests/test_advisory_regression.py
```

5. run at least one relevant dry-run command for the changed stage
