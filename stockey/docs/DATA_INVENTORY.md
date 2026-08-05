# Data Inventory & Pure-TA Scope (decision 2026-07-27)

Operator decision: stockey goes **pure data platform for TA**. All LLM-token
consumers and all fundamental-analysis collection/analysis are removed.
Supersedes the keep-fundamentals variant discussed earlier the same day;
`docs/ADVISORY_SPLIT.md` remains valid for the advisory/ module mechanics,
with its Category scope widened by this document (fundamental/news/event
analysis now also archives).

Rationale: stockey's own FDR-gated research found no robust cross-regime
selection alpha in fundamentals/deal-flow/features; news & announcement
pipelines burn LLM tokens with no backtestable output; systrader (Carver
framework) needs only the price/CA/rates/identity core below.

## KEEP — collectors and tables (the entire go-forward API)

| Source | Modules (data/…) | Tables |
|---|---|---|
| NSE bhavcopy | nseindia/bhavcopy_{downloader,history,parser} | nseindia_ohlcv, nseindia_mcap, nseindia_mto, nseindia_52wk, nseindia_cmvolt, nseindia_circuit_hit, nseindia_cat_turnover, nseindia_catg, nseindia_var1, nseindia_short_selling |
| NSE corporate actions | nseindia/corporate_action_events, nseindia/adjusted_prices (`--only normalize`, scheduled 2026-08-05) | nseindia_corporate_actions_bc_raw, nseindia_corporate_actions_normalized, events_dividend, events_capital_change |
| Price adjustment | data/nseindia/price_adjustment.py (promoted from advisory/ 2026-07-28) | advisory_adjusted_ohlcv_daily (systrader's PRIMARY series) |
| NSE indices | nseindia/indices_{downloader,parser} | nseindia_indices |
| NSE calendar | nseindia/holidays | nseindia_holidays, dim_trading_days |
| Dhan broker | dhanlive/* (incl. auth/web_login) | master_dhan_instruments, dhan_ohlcv_daily, dhan_ohlcv_intraday (future 1-min landing zone) |
| RBI/FBIL | rbi/* | rbi_bank_rates, rbi_currency_rates, fbil_gsec_par, fbil_gsec_quote |
| Identity | company_master, nseindia/security_history | company_master, dim_security* |
| Sharpely (mcap slice ONLY) | sharpelydata/sharpely_data.py | historical_mcap |
| Download run state | data/download_runner.py (via utils/sync_state.py, promoted from advisory/ 2026-07-28) | advisory_sync_state (load-bearing per Phase 2 audit; NEVER drop) |

2026-08-05 completeness sweep findings, both fixed:
- `nseindia_corporate_actions_normalized` was 1+ year stale (last row
  2025-07-25) because its writer, `data/nseindia/adjusted_prices.py
  --only normalize`, was never scheduled anywhere — only ever run
  manually. This is load-bearing: `data/dhanlive/ohlcv.py`'s
  `has_recent_adjustment()` reads it to detect recent splits/bonuses and
  trigger a full Dhan history re-fetch for that symbol, so a year of
  staleness meant undetected splits could leave `dhan_ohlcv_daily`
  discontinuous around their ex-date. Now scheduled in
  `download_runner.PARSER_STEPS` (purpose `corporate_action_normalize`,
  runs after `bhavcopy_parser` so both its raw sources are fresh
  same-day). Verified: 741→100,068 rows, 367→5,728 symbols on first run.
- `data.nseindia.corporate_actions` (writing the plain
  `nseindia_corporate_actions` table via NSE's corporate-filings-actions
  API + Playwright) ran daily but only ever covered the 2 placeholder
  symbols in `config/tracked_symbols.txt` (SHAKTIPUMP, HDFCBANK) — nobody
  widened it after `nseindia_corporate_actions_bc_raw` (bhavcopy-feed
  parse, 5,728 symbols) became the comprehensive source. Removed from
  `download_runner.DOWNLOADER_STEPS` (operator decision: bc_raw is
  sufficient, not worth ~7,000 daily browser-automation calls to widen
  it instead). The `nseindia_corporate_actions` table itself is left in
  place (30 historical rows, harmless) but is no longer written to or
  part of the KEEP table list above — `_bc_raw`/`_normalized` are now the
  sole corporate-actions source.

Cron keeps: complete_data.sh, all_downloaders_queue.sh,
all_external_workers.sh, all_price_adjustment.sh, all_ohlcv_reconcile.sh,
all_data_readiness.sh, log rotation, plus (added 2026-08-05)
all_data_coverage_report.sh — a non-fatal monitoring/visibility job
(docs/DATA_COVERAGE.md), not a data producer, so it doesn't change the "6
jobs" framing elsewhere in this doc set in spirit. Everything else unschedules.

`all_external_workers.sh` was missing from this list until the Phase 4 prep
audit (2026-08-02) caught it: `data.download_queue.classify_step` (invoked by
`all_downloaders_queue.sh`) routes every `data.nseindia.*` module plus Dhan
`scrip_master`/`ohlcv` into the `nse`/`dhan` queues instead of running them
inline — `all_external_workers.sh` (`-m advisory.external_task_queue --worker
--drain`, now backed by `utils/external_task_queue.py`) is the only thing
that drains those queues. Cutting it while keeping `all_downloaders_queue.sh`
would silently no-op the entire queued NSE/Dhan downloader lane (tasks
enqueued, never executed) — `complete_data.sh` alone would still cover the
same modules inline, but only at its 2x/day cadence, losing the
3x/day (08,12,16) intraday-coverage frequency the queue path exists for.

## REMOVE — LLM-token consumers

- data/announcements/* (categorize.py/prompts.py classify with LLM calls) →
  announcement_pipeline_documents, announcement_pipeline_reports
- data/economictimes/rss.py → economictimes_rss_items (fed LLM news themes)
- Advisory LLM stack (llm_*, event policy, adversarial review, news themes)

## REMOVE — fundamental / non-TA

- data/screenerin/* → screenerin_* tables
- Sharpely fundamentals → sharpely_stock_meta, sharpely_stock_peers,
  master_sharpely_{equity,funds}, stmt_{balancesheet,cashflow,income},
  shareholding_*, advisory_fundamentals_daily
- nseindia/insider_deals + deal-flow tables (nseindia_insider_deals,
  nseindia_{block,bulk}_deals) — sweep showed IC ≈ 0
- nseindia/earnings_events → nseindia_earnings_events (BORDERLINE: LLM-free
  and useful for FnO event-vol later — freeze the collector rather than
  delete if cheap)
- nseindia/recent_events → nseindia_events (corrected 2026-08-04: this table
  was previously misattributed to earnings_events.py above; it's actually a
  separate collector for NSE's board-meeting/AGM event calendar. Same
  BORDERLINE treatment as earnings_events: LLM-free, freeze rather than
  delete. Registered under `download_runner.DOWNLOADER_STEPS` purpose
  "events"; currently flaky against NSE's site — TimeoutError waiting for a
  download event, non-critical purpose so it doesn't block the pipeline)
- Macro: mospi_cpi, eaindustry_wpi, macro_usa*, macro_india_gdp, fii_*
  (data/mospi, data/eaindustry, data/fred, data/nsdl)
- features_* precomputed tables
- ALL advisory_* tables except advisory_adjusted_ohlcv_daily and
  advisory_sync_state (Phase 2 audit 2026-07-28 confirmed
  data/download_runner.py persists standardized run state to it via
  utils/sync_state.py — load-bearing, NEVER drop)

## Execution notes

- Archive code (git branch/attic), drop cron entries in the same commit.
- Table drops on the cloud DB: take a final dump first; drops free space on
  the small instance.
- systrader's sync list already matches the KEEP set; nothing to change
  downstream except deleting the never-built universe_screen design.
