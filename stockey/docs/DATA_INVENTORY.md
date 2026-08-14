# Data Inventory

Stockey is a pure data platform for TA: it collects, adjusts, and
identity-maps price/reference data and writes it to the cloud Postgres for
systrader to consume. It does no research, no fundamental analysis, no
news/announcement processing, and no LLM-token consumption in its
collectors — that all lives in systrader. The fundamentals screener
(`fundamentals/`) is a deliberate, separate carve-out from this boundary —
see `docs/FUNDAMENTAL_SCREENER_PRD.md`.

## Collectors and tables

| Source | Modules (`data/…`) | Tables |
|---|---|---|
| NSE bhavcopy | `nseindia/bhavcopy_{downloader,history,parser}` | `nseindia_ohlcv`, `nseindia_mcap`, `nseindia_mto`, `nseindia_52wk`, `nseindia_cmvolt`, `nseindia_circuit_hit`, `nseindia_cat_turnover`, `nseindia_catg`, `nseindia_var1`, `nseindia_short_selling`, `nseindia_block_deals`, `nseindia_bulk_deals` |
| NSE corporate actions | `nseindia/corporate_action_events`, `nseindia/adjusted_prices` | `nseindia_corporate_actions_bc_raw`, `nseindia_corporate_actions_normalized`, `events_dividend`, `events_capital_change` |
| Price adjustment | `data/nseindia/price_adjustment.py` | `nseindia_adjustment_factors` (written); `advisory_adjusted_ohlcv_daily` is a VIEW over it × `nseindia_ohlcv` (systrader's PRIMARY series, not a written table) |
| NSE indices | `nseindia/indices_{downloader,parser}` | `nseindia_indices` |
| NSE calendar | `nseindia/holidays` | `nseindia_holidays`, `dim_trading_days` |
| Dhan broker | `dhanlive/*` (incl. auth/web_login) | `master_dhan_instruments`, `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` (1-min bars) |
| RBI/FBIL | `rbi/*` | `rbi_bank_rates`, `rbi_currency_rates`, `fbil_gsec_par`, `fbil_gsec_quote` |
| Identity | `company_master`, `nseindia/security_history` | `company_master`, `dim_security*` |
| Sharpely identity/sector mapping | `sharpelydata/scrip_master.py` | `master_sharpely_equity`, `master_sharpely_funds` — feeds `company_master`'s `sharpely_id` identity fallback and `fundamentals/collectors/sector_data.py`'s NSE→BSE sector-code mapping |
| Download run state | `data/download_runner.py` (via `utils/sync_state.py`) | `advisory_sync_state` (load-bearing — never drop) |
| Promoted utility tables | `utils/fallback_telemetry.py`, `utils/external_task_queue.py`, `utils/identity_issues.py` | `advisory_fallback_events`, `advisory_external_task_queue`, `advisory_identity_issues` |

`historical_mcap` is kept as a frozen archive (2012+, ~970 symbols) — no
longer written, but holds pre-2024 market-cap history `nseindia_mcap`
doesn't have. `nseindia_corporate_actions` (~30 rows) is a frozen historical
leftover, no longer written — `nseindia_corporate_actions_bc_raw`/
`_normalized` are the live corporate-actions source.

`nseindia_earnings_events` and `nseindia_events` (NSE earnings-date and
board-meeting/AGM calendars) have collectors that exist but are deliberately
not scheduled — frozen, not deleted, kept for possible future FnO
event-vol research.

## Cron

Six core jobs (`config/stockey.crontab.template`, times are intended IST
wall-clock — the crontab file itself is written in UTC since go-crond has no
`CRON_TZ`/`TZ` support):

1. `complete_data.sh` (07:10 + 17:30) — `data.download_runner --phase all`:
   downloads + parses NSE bhavcopy/indices/corporate-actions/holidays, Dhan
   scrip master + OHLCV, RBI/FBIL rates, and normalizes corporate actions.
   `data/download_runner.py`'s `DOWNLOADER_STEPS`/`PARSER_STEPS` are the
   source of truth for the exact registry and order.
2. `all_downloaders_queue.sh` + `all_external_workers.sh` (08:30/12:30/16:30
   and +5 min) — queue single-client NSE/Dhan work
   (`data/download_queue.py`) and drain it (`utils/external_task_queue.py`)
   so parallel NSE/Dhan sessions don't collide. `all_downloaders_queue.sh`
   routes every `data.nseindia.*` module plus Dhan `scrip_master`/`ohlcv`
   into queues; cutting `all_external_workers.sh` while keeping the queue
   job would silently no-op that entire lane.
3. `all_ohlcv_reconcile.sh` (18:45) — backfills any universe symbol whose
   latest Dhan daily bar predates the last completed trading day
   (`data/dhanlive/ohlcv_reconcile.py`; universe from
   `utils/universe.py`'s `get_equity_universe()`).
4. `all_price_adjustment.sh` (18:50, right after the reconcile) — rebuilds
   `nseindia_adjustment_factors`; `advisory_adjusted_ohlcv_daily` (systrader's
   PRIMARY series) is a view over it, not a written table.
5. `all_data_readiness.sh` (22:30) — `data/data_readiness.py --fix`: checks
   freshness and runs bounded repairs.
6. `all_data_coverage_report.sh` (daily) — non-fatal per-table coverage
   report (`docs/DATA_COVERAGE.md`), monitoring only, not a data producer.
7. Log rotation (06:50, `scripts/rotate_logs.sh`).

Plus, for the fundamentals-screener carve-out:

8. `all_fundamentals_screener.sh` (19:15, weekdays) —
   `fundamentals.run_pipeline`: sector reference, L1/L2 refresh, event
   collectors, OCR + structured extraction, sector capital-cycle, L3 alerts,
   descriptive technicals, and the watchlist/narrative/email pipeline, in
   dependency order. `fundamentals/run_pipeline.py`'s `STEPS` is the source
   of truth.
9. `all_fundamentals_api.sh` (every 5 min) — long-running FastAPI service
   (`fundamentals/api/app.py`) serving the `screener/` Nuxt frontend.

## Not collected

No BSE price or corporate-action data — `company_master` currently has zero
BSE-only companies (everything tracked is NSE-listed or NSE+BSE
cross-listed, and a cross-listed company's corporate actions are already
covered by the NSE feed). No macro data beyond RBI/FBIL rates. No insider/
deal-flow data beyond block/bulk/short-selling. No fundamentals data in
stockey's own pure-TA scope — that's `fundamentals/`'s job, a separate
carve-out with its own PRD.
