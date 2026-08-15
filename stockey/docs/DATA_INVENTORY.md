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
| NSE bhavcopy | `nseindia/bhavcopy_{downloader,history,parser}` | `nseindia_ohlcv`, `nseindia_mcap` |
| NSE corporate actions | `nseindia/corporate_action_events`, `nseindia/adjusted_prices` | `nseindia_corporate_actions_bc_raw`, `nseindia_corporate_actions_normalized`, `events_dividend`, `events_capital_change` |
| Price adjustment | `data/nseindia/price_adjustment.py` | `nseindia_adjustment_factors` (written); `advisory_adjusted_ohlcv_daily` is a VIEW over it × `nseindia_ohlcv` (systrader's PRIMARY series, not a written table) |
| NSE indices | `nseindia/indices_{downloader,parser}` | `nseindia_indices` |
| NSE calendar | `nseindia/holidays` | `nseindia_holidays`, `dim_trading_days` |
| Dhan broker | `dhanlive/*` (incl. auth/web_login) | `master_dhan_instruments`, `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` (1-min bars) |
| RBI/FBIL | `rbi/*` | `rbi_bank_rates`, `rbi_currency_rates`, `fbil_gsec_par` |
| Identity | `company_master`, `nseindia/security_history` | `company_master`, `dim_security*` |
| Sharpely identity/sector mapping | `sharpelydata/scrip_master.py` | `master_sharpely_equity` — feeds `company_master`'s `sharpely_id` identity fallback and `fundamentals/collectors/sector_data.py`'s NSE→BSE sector-code mapping |
| Download run state | `data/download_runner.py` (via `utils/sync_state.py`) | `advisory_sync_state` (load-bearing — never drop) |
| Per-file ingestion state | `utils/ingestion_state.py` | `ingestion_file_state` — tracks per-object-key processed/failed status for the S3-backed parsers (`bhavcopy_parser.py`, `indices_parser.py`), backing `should_consider_key()`'s dedup and `scripts/ingestion_state_runner.py`'s inspection CLI |
| Promoted utility tables | `utils/fallback_telemetry.py`, `utils/external_task_queue.py`, `utils/identity_issues.py` | `advisory_fallback_events`, `advisory_external_task_queue`, `advisory_identity_issues` |

`historical_mcap` is kept as a frozen archive (2012+, ~970 symbols) — no
longer written, but holds pre-2024 market-cap history `nseindia_mcap`
doesn't have. `nseindia_corporate_actions` (~30 rows) is no longer written,
but — unlike `historical_mcap` — is NOT just a passive leftover: confirmed
live 2026-08-14, `data/nseindia/adjusted_prices.py`'s
`load_corporate_actions_sources()` still reads it and unions it with
`nseindia_corporate_actions_bc_raw` into every `nseindia_corporate_actions_
normalized` rebuild, so its frozen ~30 rows are actively folded into a live
write pipeline on every run, not just sitting inert.

`dim_security_overrides` is a manual-curation table (see `docs/identity.md`)
— `data/nseindia/security_history.py` reads it (`load_security_overrides()`)
but nothing writes it; confirmed live 2026-08-14 it's empty (0 rows) and has
no populating code path anywhere — a human is expected to hand-edit rows
here, which apparently hasn't happened yet.

`nseindia_earnings_events` and `nseindia_events` (NSE earnings-date and
board-meeting/AGM calendars) have collectors that exist but are deliberately
not scheduled — frozen, not deleted, kept for possible future FnO
event-vol research.

### Retired 2026-08-15 (write-only, zero readers anywhere, confirmed live)

An audit of every table against actual read call sites (not just docs) found
these had never had a consumer since the day they were first written. Per
"unused tables and code should be removed," each was archived in full to S3
(`archives/retired_2026-08-15/<table>/`, gzip CSV, one object per month for
the large ones) and then dropped from the live DB — not just stopped, fully
retired:

- `nseindia_mto`, `nseindia_52wk`, `nseindia_cmvolt`, `nseindia_circuit_hit`,
  `nseindia_cat_turnover`, `nseindia_catg`, `nseindia_var1` — the `bhavcopy_
  parser.py` parsers for all seven deleted along with the dispatch that fed
  them; `nseindia_var1`'s only-ever consumer was `advisory/event_evidence_
  store.py`, deleted in the pure-TA cut, orphaning it.
- `nseindia_short_selling`, `nseindia_block_deals`, `nseindia_bulk_deals` —
  the whole `data/nseindia/offmarket.py`/`offmarket_parser.py` collector
  deleted (it was also independently broken: NSE download-trigger timeouts
  on every recent run, see `docs/DATA_COVERAGE.md`'s log-sweep note).
- `master_sharpely_funds` — `sharpelydata/scrip_master.py` no longer
  requests instrumentType 0/1 (non-stock entities) from the sharpely API at
  all, only the equity type it actually stores.
- `fbil_gsec_quote` — `rbi/download_fbil_gsec.py` no longer parses the
  "G-Sec" sheet's full quote table, only the trade-date cell (still needed
  to stamp `fbil_gsec_par`, which is consumed) and the "Par Yield" sheet.
- `fundamentals_screenerin_query_results` — the deleveraging screen
  (`fundamentals/collectors/screenerin.py`'s standalone step) removed from
  `run_pipeline.py`'s `STEPS`; the module's shared screener.in scraping
  infra (`build_authenticated_session`/`run_query`/etc., used by L1/L2)
  stays.
- `fundamentals_industry_group_reference`, `fundamentals_basic_industry_
  reference` — `fundamentals/collectors/sector_data.py` no longer extracts
  the finer two levels of Sharpely's sector hierarchy, only the top
  (sector) level `sector_cycle.py` actually groups by.
- `nseindia_ohlcv_adjusted` — separately confirmed empty and dropped
  2026-08-14 (see above), an earlier adjusted-price design superseded by
  the factor-table + view.
- `nseindia_reg`, `nseindia_pe`, `nseindia_csqr` — `bhavcopy_parser.py`'s
  `parse_reg`/`parse_pe`/`parse_csqr` deleted; these tables never existed
  in the DB at all (confirmed live 2026-08-14: the globs matching `REG_*.
  CSV`/`PE_*.CSV`/`CSQR_*.CSV` inside the downloaded bhavcopy archive had
  never matched a single file since this collector's inception — dead
  code, not a stopped collector).

## Cron

Seven core jobs (`config/stockey.crontab.template`, times are intended IST
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
6. `all_data_coverage_report.sh` (17:10, weekdays) — non-fatal per-table coverage
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
deal-flow data at all (block/bulk-deals, short-selling collection retired
2026-08-15 — see above). No fundamentals data in stockey's own pure-TA
scope — that's `fundamentals/`'s job, a separate carve-out with its own
PRD.
