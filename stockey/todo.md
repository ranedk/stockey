# Investment Advisory TODO

This file maps the target workflow in [`docs/implementation.md`](/home/rane/code/stockey/docs/implementation.md) to the current codebase state as of 2026-03-23.

## Implemented

- Task 1 screener normalization:
  - [`advisory/screener_parser.py`](/home/rane/code/stockey/advisory/screener_parser.py) materializes `advisory_screener_constituents` from `dhan_screener_snapshots`.
- Task 2 OHLCV ingestion foundation:
  - [`data/dhanlive/ohlcv.py`](/home/rane/code/stockey/data/dhanlive/ohlcv.py) ingests `dhan_ohlcv_daily` and `dhan_ohlcv_intraday`.
- Task 3 macro snapshot:
  - [`advisory/macro_snapshot.py`](/home/rane/code/stockey/advisory/macro_snapshot.py) materializes `advisory_macro_daily`.
  - Existing macro tables are being reused: `rbi_bank_rates`, `fbil_gsec_par`, `mospi_cpi`, `eaindustry_wpi`, `macro_usa`.
- Peer and Sharpely sync:
  - [`data/sharpelydata/sharpely_data.py`](/home/rane/code/stockey/data/sharpelydata/sharpely_data.py) persists `sharpely_stock_meta` and `sharpely_stock_peers`.
  - [`advisory/peer_sync.py`](/home/rane/code/stockey/advisory/peer_sync.py) incrementally refreshes peer metadata, fundamentals, and missing peer OHLCV.
- Task 4 regime engine:
  - [`advisory/regime_engine.py`](/home/rane/code/stockey/advisory/regime_engine.py) materializes `advisory_market_regime` using `REGIME_ALGO_V1`.
- Task 5 technical parameter calculator:
  - [`advisory/technical_features.py`](/home/rane/code/stockey/advisory/technical_features.py) materializes `advisory_technical_daily`.
  - TA-Lib features are in place, including peer-based `rs_vs_sector`.
- Point-in-time fundamentals:
  - [`advisory/fundamental_snapshot.py`](/home/rane/code/stockey/advisory/fundamental_snapshot.py) materializes `advisory_fundamentals_daily`.
  - Peer-relative fundamental deltas vs sector peers are in place.
- Task 6 setup registry and rule engine:
  - [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml) defines setup rules.
  - [`advisory/setup_registry.py`](/home/rane/code/stockey/advisory/setup_registry.py) loads the setup registry.
  - [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py) materializes `advisory_candidates` and `advisory_candidate_rejections`.
- Task 7 watchlist layer:
  - [`advisory/watchlist_builder.py`](/home/rane/code/stockey/advisory/watchlist_builder.py) materializes `advisory_watchlist` from passed candidates.
  - [`advisory/announcement_watch.py`](/home/rane/code/stockey/advisory/announcement_watch.py) drives the managed announcement pipeline for active watchlist rows and materializes `advisory_watch_events`.
- Agent-safe execution surface:
  - JSON runners exist in [`scripts/sql_query_runner.py`](/home/rane/code/stockey/scripts/sql_query_runner.py), [`scripts/redis_query_runner.py`](/home/rane/code/stockey/scripts/redis_query_runner.py), and [`scripts/s3_query_runner.py`](/home/rane/code/stockey/scripts/s3_query_runner.py).
  - Curated registry execution exists in [`scripts/agent_tool_runner.py`](/home/rane/code/stockey/scripts/agent_tool_runner.py).

## Partially implemented or still missing

### Task 1. Screener crawler

- Current normalized screener coverage is still shallow.
  - `advisory_screener_constituents` currently reflects one live ScanX screener slug.
  - All four setups are temporarily mapped onto the same screener universe in [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml).
- Still needed:
  - more setup-specific screeners
  - support for multiple upstream screener sources
  - stronger setup-to-screener curation

### Task 2. OHLCV crawler

- Dhan OHLCV ingestion works, but the advisory stack still lacks:
  - a unified price inventory table such as `advisory_price_inventory`
  - missing-bar detection and freshness QA
  - sector/index price coverage beyond the current peer-basket approximation
  - a Dhan-adjusted price view if execution should depend on Dhan bars directly
- Current blocker surfaced by the rule engine:
  - some live screener symbols, for example `GUJALKALI`, do not yet exist in `nseindia_ohlcv_adjusted`, so they fail technical evaluation even though screener parsing works

### Task 3. Macro crawler

- Current gap remains:
  - `NEW_MACRO_SOURCE_REQUIRED`: sector-linked commodity prices
- Still needed:
  - explicit policy-window flags
  - explicit shock-window flags

### Task 4. Regime engine

- `advisory_market_regime` exists and emits only the documented labels.
- Still needed:
  - breadth proxies
  - manual shock override table
  - richer policy-event features instead of only threshold-based macro pressure flags

### Task 5. Technical parameter calculator

- `advisory_technical_daily` exists and includes the required TA-Lib feature set.
- Still needed:
  - broader stock coverage in `nseindia_ohlcv_adjusted`
  - intraday feature layer for timing
  - sector/index benchmark mapping where a peer basket is not sufficient

### Task 6. Rule engine

- The first-pass rule engine is live and persists explicit reject reasons.
- Still needed:
  - better market-cap bucket and SME/mid/large tagging
  - setup-specific screener diversity
  - better handling for names missing technical or fundamental coverage
  - automatic watchlist materialization from passed candidates

### Task 7. Announcement watcher

- Setup-aware watchlist orchestration now exists.
- Still needed:
  - trigger taxonomy refinement by setup
  - better watch lifecycle states beyond `active`
  - tighter filtering of which announcement categories should become material watch events

### Task 8. Announcement parser + LLM evaluator

- OCR and storage exist, but investment evaluation does not.
- Still needed:
  - [`advisory/llm_event_evaluator.py`](/home/rane/code/stockey/advisory/llm_event_evaluator.py)
  - [`advisory/prompts.py`](/home/rane/code/stockey/advisory/prompts.py)
  - tables:
    - `advisory_event_evaluations`
    - `advisory_event_risks`

### Task 9. Risk profiling and allocation

- Not implemented.
- Still needed:
  - [`advisory/risk_engine.py`](/home/rane/code/stockey/advisory/risk_engine.py)
  - table `advisory_allocations`

## Cross-cutting work still needed

### 1. Advisory package expansion

- Existing advisory modules:
  - `advisory/__init__.py`
  - `advisory/screener_parser.py`
  - `advisory/macro_snapshot.py`
  - `advisory/peer_sync.py`
  - `advisory/regime_engine.py`
  - `advisory/setup_registry.py`
  - `advisory/technical_features.py`
  - `advisory/fundamental_snapshot.py`
  - `advisory/rule_engine.py`
  - `advisory/watchlist_builder.py`
  - `advisory/announcement_watch.py`
- Still needed:
  - `advisory/llm_event_evaluator.py`
  - `advisory/prompts.py`
  - `advisory/risk_engine.py`
  - `advisory/pipeline.py`

### 2. Point-in-time advisory tables

- Implemented:
  - `advisory_macro_daily`
  - `advisory_fundamentals_daily`
  - `advisory_technical_daily`
  - `advisory_market_regime`
  - `advisory_candidates`
  - `advisory_candidate_rejections`
  - `advisory_watchlist`
  - `advisory_watch_events`
- Still needed:
  - `advisory_event_evaluations`
  - `advisory_event_risks`
  - `advisory_allocations`

### 3. Market-cap and segment tagging

- Current setup rules use heuristic market-cap thresholds from screener data.
- Still needed:
  - maintained market-cap bucket dimension
  - SME / large-cap / mid-cap / defensive tags
  - canonical benchmark and sector-index mapping

### 4. Validation and data quality checks

- Still needed:
  - stale screener snapshot checks
  - macro staleness alerts
  - OHLCV gap detection
  - intraday availability checks
  - missing-coverage reports for live screener names
  - LLM evaluation source-trace validation

### 5. JSON-first CLIs and agent tools

- Implemented CLIs:
  - `python -m advisory.screener_parser`
  - `python -m advisory.macro_snapshot`
  - `python -m advisory.peer_sync`
  - `python -m advisory.regime_engine`
  - `python -m advisory.technical_features`
  - `python -m advisory.fundamental_snapshot`
  - `python -m advisory.rule_engine`
  - `python -m advisory.watchlist_builder`
  - `python -m advisory.announcement_watch`
- Still needed:
  - `python -m advisory.llm_event_evaluator`
  - `python -m advisory.risk_engine`
  - `python -m advisory.pipeline`

### 6. Research and backtest layer

- Still needed:
  - `advisory/models/dataset_builder.py`
  - `advisory/models/train_ranker.py`
  - `advisory/models/train_event_model.py`
  - `advisory/backtest.py`
- Use:
  - `pandas`
  - `scikit-learn`
  - `xgboost`

## Recommended next implementation order

1. Expand OHLCV and adjusted-price coverage for live screener symbols so the rule engine is not blocked by missing technical snapshots.
2. Add a maintained market-cap / segment mapping dimension instead of setup-local heuristics.
3. Build the LLM event evaluator and prompt contract.
4. Build risk sizing and allocation.
5. Add breadth, policy-window, and manual shock override inputs to `advisory_market_regime`.
6. Refine watch-event filtering so the watch layer prioritizes only material announcement categories.

## Notes for the implementing agent

- Prefer existing JSON runners in `scripts/` for inspection and verification.
- Prefer `company_master_id` as the join key wherever possible.
- Use point-in-time joins only. Do not use future fundamentals or macro values.
- Reuse `features/tutils.py` patterns for release-aware daily snapshots.
- Use TA-Lib for technicals, `pandas` for feature assembly, `scikit-learn` and `xgboost` only where a model is explicitly introduced.
- Keep raw ingestion, daily snapshots, rule outputs, and LLM outputs in separate tables.
