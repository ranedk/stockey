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
10. Risk sizing
11. Portfolio planning
12. Lifecycle tracking
13. Execution planning

Important runtime decisions:

- canonical advisory OHLCV source: `dhan_ohlcv_daily`
- canonical fundamentals source: Sharpely tables from [`data/sharpelydata/sharpely_data.py`](/home/rane/code/stockey/data/sharpelydata/sharpely_data.py)
- official event source: exchange announcement pipeline
- non-official news source: Economic Times RSS
- legacy/reference only: NSE bhavcopy plus local adjusted-price pipeline

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
- full orchestrator: [`advisory/pipeline.py`](/home/rane/code/stockey/advisory/pipeline.py)
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
- allocation/execution layer: `advisory_allocations`, `advisory_portfolio_orders`, `advisory_position_lifecycle`, `advisory_rebalance_actions`, `advisory_execution_orders`, `advisory_execution_fills`

## Day-to-day commands

Use the project venv:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m ...
```

### Full advisory dry-run

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.pipeline --include-watch --include-news --dry-run
```

### Full advisory write run

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.pipeline --include-watch --include-news
```

### Through portfolio only

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.pipeline --dry-run --stop-at portfolio
```

### Specific stages

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.screener_parser
/home/rane/code/stockey/.xstockey/bin/python -m advisory.technical_features
/home/rane/code/stockey/.xstockey/bin/python -m advisory.fundamental_snapshot
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.watchlist_builder
/home/rane/code/stockey/.xstockey/bin/python -m advisory.announcement_watch
/home/rane/code/stockey/.xstockey/bin/python -m data.economictimes.rss
/home/rane/code/stockey/.xstockey/bin/python -m advisory.news_watch --refresh-feeds
/home/rane/code/stockey/.xstockey/bin/python -m advisory.llm_event_evaluator
/home/rane/code/stockey/.xstockey/bin/python -m advisory.risk_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.portfolio_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.position_lifecycle
/home/rane/code/stockey/.xstockey/bin/python -m advisory.execution_engine --dry-run
/home/rane/code/stockey/.xstockey/bin/python -m advisory.symbol_trace HDFCBANK --format text
/home/rane/code/stockey/.xstockey/bin/python -m advisory.setup_trace LARGECAP_BREAKOUT_V1 --format text
/home/rane/code/stockey/.xstockey/bin/python -m advisory.dashboard --format text
```

### Regression and cleanup

```sh
/home/rane/code/stockey/.xstockey/bin/python -m pytest tests/test_advisory_regression.py
/home/rane/code/stockey/.xstockey/bin/python scripts/cleanup_deprecated_tables.py --dry-run
```

## How to add a new screener

The system currently uses Screener.in as the active advisory screener source.

### Step 1: add the URL to the registry

Example:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-new-screen/"
```

Optional display name:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-new-screen/" --name "My New Screen"
```

List registered screeners:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_registry list
```

Inspect latest rows:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_registry latest --screener my-new-screen
```

Remove a screener:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_registry remove my-new-screen
```

### Step 2: sync the raw screener snapshot

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_parser
```

### Step 3: normalize into the advisory universe

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.screener_parser
```

### Step 4: verify the results

```sh
/home/rane/code/stockey/.xstockey/bin/python scripts/sql_query_runner.py --read-only "select date, screener_slug, count(*) as row_count from advisory_screener_constituents group by 1,2 order by 1 desc,2"
```

## How to add or change a setup

Setups are configured in [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml).

Each setup supports:

- `setup_id`
- `setup_name`
- `screener_slug`
- `allowed_regimes`
- `market_cap_min`
- `market_cap_max`
- `min_avg_traded_value_20d`
- `max_breakout_extension_pct`
- `min_dist_52w_high`
- `technical_rules`
- `fundamental_rules`
- `watch_reasons`

### Example pattern

```yaml
- setup_id: MY_SETUP_V1
  setup_name: My setup
  screener_slug: my-new-screen
  allowed_regimes:
    - STABLE
    - BULL_NARROW
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
  fundamental_rules:
    - column: debt_to_equity_vs_sector
      operator: lte
      value: 0.2
  watch_reasons:
    - earnings
    - order wins
```

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
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine --dry-run
```

If the output looks correct:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.watchlist_builder
```

## How to inspect why a stock passed or failed

The fastest way is:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.symbol_trace HDFCBANK --format text
```

### Passed candidates

```sh
/home/rane/code/stockey/.xstockey/bin/python scripts/sql_query_runner.py --read-only "select * from advisory_candidates order by asof_date desc, setup_id, symbol limit 50"
```

### Rejections

```sh
/home/rane/code/stockey/.xstockey/bin/python scripts/sql_query_runner.py --read-only "select * from advisory_candidate_rejections order by asof_date desc, setup_id, symbol limit 100"
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
./all_downloads.sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.pipeline --include-watch --include-news
```

### If you only changed rules

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine
/home/rane/code/stockey/.xstockey/bin/python -m advisory.watchlist_builder
```

### If you only changed screeners

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.screenerin.screener_parser
/home/rane/code/stockey/.xstockey/bin/python -m advisory.screener_parser
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine
```

### If you want event processing only

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.announcement_watch
/home/rane/code/stockey/.xstockey/bin/python -m data.economictimes.rss
/home/rane/code/stockey/.xstockey/bin/python -m advisory.news_watch
/home/rane/code/stockey/.xstockey/bin/python -m advisory.llm_event_evaluator
```

## Troubleshooting

### Dhan token problems

Use:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m data.dhanlive.auth_cli status
/home/rane/code/stockey/.xstockey/bin/python -m data.dhanlive.auth_cli refresh --clear-cache-first
/home/rane/code/stockey/.xstockey/bin/python -m data.dhanlive.auth_cli validate
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
/home/rane/code/stockey/.xstockey/bin/python -m pytest tests/test_advisory_regression.py
```

5. run at least one relevant dry-run command for the changed stage
