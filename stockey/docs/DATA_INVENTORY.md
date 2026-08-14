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
| NSE corporate actions | nseindia/corporate_action_events, nseindia/adjusted_prices (scheduled 2026-08-05) | nseindia_corporate_actions_bc_raw, nseindia_corporate_actions_normalized, events_dividend, events_capital_change |
| Price adjustment | data/nseindia/price_adjustment.py | nseindia_adjustment_factors (written); advisory_adjusted_ohlcv_daily is a VIEW over it x nseindia_ohlcv (systrader's PRIMARY series, not a written table) |
| NSE indices | nseindia/indices_{downloader,parser} | nseindia_indices |
| NSE calendar | nseindia/holidays | nseindia_holidays, dim_trading_days |
| Dhan broker | dhanlive/* (incl. auth/web_login) | master_dhan_instruments, dhan_ohlcv_daily, dhan_ohlcv_intraday (future 1-min landing zone) |
| RBI/FBIL | rbi/* | rbi_bank_rates, rbi_currency_rates, fbil_gsec_par, fbil_gsec_quote |
| Identity | company_master, nseindia/security_history | company_master, dim_security* |
| Sharpely identity/sector mapping (mcap slice removed 2026-08-14, see REMOVE section note) | sharpelydata/scrip_master.py | master_sharpely_equity, master_sharpely_funds — feeds company_master's sharpely_id fallback + fundamentals/collectors/sector_data.py |
| Download run state | data/download_runner.py (via utils/sync_state.py, promoted from advisory/ 2026-07-28) | advisory_sync_state (load-bearing per Phase 2 audit; NEVER drop) |
| Promoted utility tables (correction 2026-08-14: the "ALL advisory_\* except adjusted_ohlcv_daily/sync_state" REMOVE wording below technically caught these too, but they're live Phase-2-promoted `utils/*` modules, not advisory research artifacts) | utils/fallback_telemetry.py, utils/external_task_queue.py, utils/identity_issues.py | advisory_fallback_events, advisory_external_task_queue, advisory_identity_issues |

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

## REMOVED — LLM-token consumers (dropped 2026-08-14)

Code was archived in the 2026-07-27/08-02 pure-TA cut; the tables themselves
physically stayed until the Phase 5 cleanup below finally ran:

- data/announcements/* (categorize.py/prompts.py classify with LLM calls) →
  announcement_pipeline_documents, announcement_pipeline_reports
- data/economictimes/rss.py → economictimes_rss_items (fed LLM news themes)
- Advisory LLM stack (llm_*, event policy, adversarial review, news themes)

## REMOVED — fundamental / non-TA (dropped 2026-08-14 unless noted)

- data/screenerin/* → screenerin_* tables
- Sharpely fundamentals → sharpely_stock_meta, sharpely_stock_peers,
  stmt_{balancesheet,cashflow,income}, shareholding_*, advisory_fundamentals_daily.
  Correction 2026-08-14: `master_sharpely_{equity,funds}` (written by
  `sharpelydata/scrip_master.py`) turned out to still be load-bearing —
  `data/company_master.py` uses `sharpely_id` as an identity-key fallback, and
  the fundamentals screener's `fundamentals/collectors/sector_data.py` (built
  2026-08-11, after this list was written) reads it for NSE→BSE sector-code
  mapping. Moved to KEEP above; `scrip_master.py` stays scheduled; these two
  tables were NOT part of the 2026-08-14 drop.
- Sharpely mcap slice (`sharpelydata/sharpely_data.py` → `historical_mcap`)
  removed 2026-08-14 — collector had regressed to a 2-symbol placeholder list
  (`config/tracked_symbols.txt`) with zero downstream consumers; `nseindia_mcap`
  (bhavcopy parser, free byproduct of the daily download) covers the same
  point-in-time universe for 2024-02+. Pre-2024 history in `historical_mcap`
  (2012+, 973 symbols) is real and non-trivial to re-collect — table itself
  kept as a frozen archive (2026-08-14 decision), NOT part of the drop below.
- nseindia_insider_deals — no live writer, dropped. Correction 2026-08-14:
  `nseindia_block_deals`/`nseindia_bulk_deals` were misattributed here — both
  are still written live by `data/nseindia/offmarket_parser.py`, the same
  scheduled parser that produces the KEEP-listed `nseindia_short_selling`.
  Moved to KEEP above; NOT part of the drop.
- nseindia/earnings_events → nseindia_earnings_events (BORDERLINE: LLM-free
  and useful for FnO event-vol later — collector frozen, NOT part of the
  drop; table stays)
- nseindia/recent_events → nseindia_events (corrected 2026-08-04: this table
  was previously misattributed to earnings_events.py above; it's actually a
  separate collector for NSE's board-meeting/AGM event calendar. Same
  BORDERLINE treatment as earnings_events: collector frozen, NOT part of the
  drop; table stays. Registered under `download_runner.DOWNLOADER_STEPS`
  purpose "events"; currently flaky against NSE's site — TimeoutError waiting
  for a download event, non-critical purpose so it doesn't block the pipeline)
- Macro: mospi_cpi, eaindustry_wpi, macro_usa*, macro_india_gdp, fii_*
  (data/mospi, data/eaindustry, data/fred, data/nsdl)
- features_* precomputed tables
- ALL advisory_* tables except advisory_adjusted_ohlcv_daily,
  advisory_sync_state, and the three promoted utility tables now in KEEP
  above (advisory_fallback_events, advisory_external_task_queue,
  advisory_identity_issues)

## Execution notes

- Archive code (git branch/attic), drop cron entries in the same commit.
- **Phase 5 (cloud-DB table drops) executed 2026-08-14**, ~3 weeks after Phase
  4 archived the code — the tables above had stayed physically present the
  whole time despite `PURE_TA_MIGRATION_PLAN.md`'s "no cloud-DB dump before
  drops" operator decision, found during a full DB audit. A targeted `pg_dump`
  of exactly the dropped tables (schema+data, `-Fc`) was taken first anyway as
  a local safety net (`~/stockey_db_dumps/`), on top of that decision, since
  it cost nothing and the tables' code had been gone long enough that nobody
  had eyes on whether any were still quietly worth reading. 152 tables
  dropped, ~15 GB freed. `data/backfill_company_master_ids.py`'s SPECS and
  `scripts/hot_table_retention.py`'s RETENTION_TABLES had their entries for
  dropped tables removed in the same pass so neither errors on next run.
  Two additional undocumented tables found by the same audit were held back
  pending a separate decision:
  - `nseindia_ohlcv_adjusted` — dropped, then found NOT dead: `systrade/scripts/
    sync_from_stockey.sh` had it in `FULL_TABLES` for its TR-adjusted columns,
    a real gap `DATA_CONTRACT.md`'s own prose didn't mention -- checking the
    doc wasn't enough, the actual sync script is the ground truth. Its own
    backup came back empty too (a second, unrelated finding: it's a
    TimescaleDB hypertable, and plain `pg_dump -t`/`\copy tablename` silently
    copy 0 rows for those -- see `DATA_CONTRACT.md`'s hypertable section).
    Net result: not restored -- superseded instead, by the `nseindia_adjustment_factors`
    + `advisory_adjusted_ohlcv_daily`-view redesign above, which now covers
    total-return adjustment from the full universe instead of 2 symbols.
  - `nseindia_var1_archive_pre_dedup_20260801` -- 209M rows / 52 GB, zero code
    or doc references anywhere. Its unique index
    (`for_date, entry_number, series, symbol, isin`) has one extra column vs
    `nseindia_var1`'s own (`for_date, series, symbol, isin`) -- confirms it's a
    genuine pre-dedup raw-ingestion snapshot from a 2026-08-01 cleanup, not an
    unrelated/mystery dataset. Pure VaR/margin technical data either way (not
    fundamentals), fully superseded by the deduplicated live `nseindia_var1`.
- systrader's sync list already matches the KEEP set; nothing to change
  downstream except deleting the never-built universe_screen design.
