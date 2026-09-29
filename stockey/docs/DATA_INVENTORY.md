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
| NSE bhavcopy | `nseindia/bhavcopy_{downloader,history,parser}` | `nseindia_ohlcv`, `nseindia_mcap`, `nseindia_mto` (security-wise delivery position) and `nseindia_circuit_hit` (price-band hits), both from the same daily zip, both revived 2026-09-11 and read by systrader as slicing traits |
| NSE corporate actions | `nseindia/corporate_action_events`, `nseindia/adjusted_prices` | `nseindia_corporate_actions_bc_raw`, `nseindia_corporate_actions_normalized`, `events_dividend`, `events_capital_change` |
| Price adjustment | `data/nseindia/price_adjustment.py` | `nseindia_adjustment_factors` (written); `advisory_adjusted_ohlcv_daily` is a VIEW over it × `nseindia_ohlcv` (systrader's PRIMARY series, not a written table) |
| BSE bhavcopy (BSE-only companies) | `data/bseindia/bhavcopy.py` | `bseindia_ohlcv` — one market-wide file/day, closes a fundamentals-screener gap (43 active L1-universe companies with no NSE listing had zero price/technicals coverage, confirmed live 2026-08-15); not systrader's PRIMARY series (that stays NSE-only) |
| BSE price adjustment | `data/bseindia/price_adjustment.py` | `bseindia_adjustment_factors` (written, price-step-derived only — no BSE-side corporate-actions feed to corroborate against); `bse_advisory_adjusted_ohlcv_daily` is a VIEW over it × `bseindia_ohlcv`, keyed by `scrip_code` (BSE's real identity key, not its ticker text) |
| NSE indices | `nseindia/indices_{downloader,parser}` | `nseindia_indices` |
| NSE calendar | `nseindia/holidays` | `nseindia_holidays`, `dim_trading_days` |
| NSE off-market deals | `data/nseindia/{offmarket,offmarket_parser}.py` (revived 2026-08-29, see "Retired 2026-08-15" below for history) | `nseindia_block_deals`, `nseindia_bulk_deals`, `nseindia_short_selling` — fundamentals-screener deal-flow signal (PRD §12), not systrader's PRIMARY series |
| NSE surveillance indicators | `data/nseindia/surveillance_indicator.py` (2026-09-25) | `nseindia_surveillance_indicator` — NSE's daily REG1_IND file: GSM/ASM/ESM/IRP stages, pledge and encumbrance flags per security (100 = not flagged), full row in `raw`; from 2025-01. Read by the universe rebuild's Layer 1 (`docs/UNIVERSE_PRD.md`) |
| Economic Times news (fundamentals carve-out) | `fundamentals/collectors/et_news.py` (2026-09-29, hourly `all_fundamentals_news.sh`) | `fundamentals_news_item` — 17 ET RSS feeds (markets, economy, policy, sector sections), one row per article keyed by link, `first_seen_at` never updated, `sector_hint` from the feed's section. Feeds the event-driven re-evaluation of fundamental scores; not gated on the fundamentals pause |
| screener.in quarterly results (fundamentals carve-out) | `fundamentals/screens/l2_state.py` crawl + `fundamentals/screens/results_reading.py` (2026-09-29) | `fundamentals_quarterly_results` — the 13-quarter table from each company's screener.in page (sales, operating profit, OPM, other income, PBT, net profit; lenders' Revenue / Financing Profit map onto the same fields), one row per (company_id, period_end), first-seen values kept, `first_seen_at` the point-in-time clock; `fundamentals_results_reading` — the dated per-company reading of the latest quarter against its own trend |
| Re-evaluation (fundamentals carve-out) | `fundamentals/screens/news_tagging.py`, `fundamentals/screens/reeval.py` (2026-09-29, every 30 min `all_fundamentals_reeval.sh`) | `fundamentals_news_tag` — ET items tagged to universe companies / sectors with direction, dimension, materiality; `fundamentals_reeval_queue` + `fundamentals_reeval_watermark` — what touched which company; `fundamentals_story_score_live` — each company's current story score; `fundamentals_story_score_change` — material score changes |
| Dhan broker | `dhanlive/*` (incl. auth/web_login) | `master_dhan_instruments`, `dhan_ohlcv_daily`, `dhan_ohlcv_intraday` (1-min bars) |
| Intraday backfill state | `scripts/backfill_intraday_5yr.py` | `dhan_intraday_backfill_state` — per-(ticker, interval) completion marker for the one-off 5-year fill. Added 2026-08-31: the previous resume signal ("earliest stored bar reaches target_start") could never match for a symbol whose history genuinely starts inside the window, so every restart re-fetched and re-upserted those symbols in full. Bookkeeping only; no market data, no readers outside that script |
| RBI/FBIL | `rbi/*` | `rbi_bank_rates`, `rbi_currency_rates`, `fbil_gsec_par` |
| Identity | `company_master`, `nseindia/security_history` | `company_master`, `dim_security*` |
| Sharpely identity/sector mapping | `sharpelydata/scrip_master.py` | `master_sharpely_equity` — feeds `company_master`'s `sharpely_id` identity fallback and `fundamentals/collectors/sector_data.py`'s NSE→BSE sector-code mapping; since 2026-09-25 also the finer exchange industry codes (`industry_code`, `nse_basic_ind_code`) read by the universe groups |
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
  **`nseindia_mto` REVIVED 2026-09-11**: systrader reads delivery % as a
  slicing trait (speculative vs investor volume). `parse_mto` and its dispatch
  restored from git as they stood at retirement (the `.DAT` layout and the
  first-row/trailer fixes intact, plus `sr_no` pinned to text so the column
  type never flips on a day without a trailer); history reloaded from this
  archive by `scripts/restore_retired_table.py`, days after it by the parser's
  `--force` backfill. No new cron job: it rides the existing bhavcopy parse.
  **`nseindia_circuit_hit` REVIVED 2026-09-11** the same way and for the same
  reason (which stocks hit their upper or lower price band — retail frenzy and
  forced selling): `parse_circuit_hit` and its PR-zip dispatch restored from
  git, history from this archive by `scripts/restore_retired_table.py`, days
  after it by `--force`.
- `nseindia_short_selling`, `nseindia_block_deals`, `nseindia_bulk_deals` —
  the whole `data/nseindia/offmarket.py`/`offmarket_parser.py` collector
  deleted (it was also independently broken: NSE download-trigger timeouts
  on every recent run, see `docs/DATA_COVERAGE.md`'s log-sweep note).
  **REVIVED 2026-08-29** (fundamentals PRD §12 todo #1: the ownership-axis
  confluence signal needs secondary-market deal-flow, which nothing else in
  this inventory captures) — same module names, same three tables, root
  cause fixed (the old downloader skipped the homepage-warmup navigation
  every other nseindia collector does before a deep-page `nse_goto`, and
  requested up to 365 days of deals in one CSV; now capped at 30 days/request
  with a homepage visit first). See the "Collectors and tables" table below.
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

The schedule lives in **`config/stockey.crontab.template`** (the source of truth) with a
convenience table in **`CLAUDE.md`**'s "Current Architecture".

This document used to carry its own copy of the job list. It was removed 2026-09-06
because it had silently drifted: after the collection times were retimed, this copy still
showed the old ones and was wrong by hours on nearly every job, while claiming to be
authoritative. Two copies of a schedule do not stay in step, and the stale one is worse
than none — check the template, or `python builder.py --check-crontab` for drift between
the template and the generated file.

What remains authoritative here is the **collector/table inventory** above: which module
writes which table, and what is deliberately out of scope.

## Not collected

No BSE price or corporate-action data — `company_master` currently has zero
BSE-only companies (everything tracked is NSE-listed or NSE+BSE
cross-listed, and a cross-listed company's corporate actions are already
covered by the NSE feed). No macro data beyond RBI/FBIL rates. No
litigation/regulatory-action data (SEBI orders, tax/GST notices). Block/bulk
deals and short-selling ARE now collected again (`data/nseindia/offmarket.py`,
revived 2026-08-29 — see "Retired 2026-08-15" above for the history), and
block/bulk deals feed two new fundamentals L3 triggers
(`fundamentals/screens/deal_flow.py`, PRD §12 todo #3) — but no promoter-name
identification exists anywhere in this pipeline (only `promoter_pct`, a
percentage, not a name string), so a bulk/block deal can never be classified
as "promoter selling" specifically, only as a named client's buy/sell —
deliberately not guessed at from `client_name` string-matching. `short_selling`
is collected but not yet used by any fundamentals trigger (anonymous
aggregate, no client_name — a different kind of signal, needing its own
spike/baseline design). No fundamentals data in stockey's own pure-TA scope — that's `fundamentals/`'s
job, a separate carve-out with its own PRD.
