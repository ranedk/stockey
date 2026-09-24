# Data audit, 2026-09-23

The operator's brief: tests pass and nothing errors, yet data is repeatedly not captured,
not stored, stored in the wrong type, or stored and never used. This audit went field by
field through the fundamentals pipeline (collectors -> L1/L2 -> L3 -> confluence ->
watchlist -> portfolio -> API/frontend). Method: a column-level profile of every
`fundamentals_*` table (all < 10 MB, so full scans were safe), four read-only code audits
(one per stage), and every claim confirmed against the live DB with small, bounded
queries before anything was changed. Hypertable views were only ever read date-bounded.

The trigger was confluence v1: its trajectory axis tested `increasing`/`decreasing`
against a field L2 writes as `steady_increase`/`accelerating_decline`/..., scored 0 of
137 names for weeks, and its tests used the same wrong words.

Status key: **fixed** (code + data, tested) · **open** (recorded, not done, with why).

## Step ordering

| # | Finding | Status |
|---|---|---|
| O1 | Portfolio (16:30 UTC) ran before the screener finished (65-109 min from 15:30) on 5 of 6 days: decisions on today's L2 beside yesterday's confluence and technicals. | **fixed** -- `all_portfolio_ruleset.sh` waits on the screener lock (5h cap -> no run); ruleset refuses confluence older than `PORTFOLIO_CONFLUENCE_MAX_AGE_DAYS`. |
| O2 | Confluence scores only the active watchlist, synced in the LAST step, so new names score next day. | **fixed 2026-09-24** -- a `watchlist` step runs after `llm_triage` and before `confluence_score`; the portfolio enters from that night's score. |
| O3 | `rating_action` and `capital_raise` were judged before OCR/extraction; OCR'd-but-unextracted rows counted as finished. `rating_downgrade` had never fired. | **fixed** -- both wait for the document; extraction status NULL no longer means done. |

## Not captured

| # | Finding | Status |
|---|---|---|
| C1 | ~1,000 BSE PDFs failed OCR permanently: only `AttachLive/` was tried; BSE moves older filings to `AttachHis/` (confirmed by hand). | **fixed** -- archive fallback; ~414 results/rating/auditor/capital/RPT rows re-admitted (one-time migration, drains under the 3h OCR budget). 584 insider-notice rows deliberately not re-admitted (see `readmit_bse_attachlive_failures`). |
| C2 | Pledge and PE were fetched market-wide daily but stored only for the 0-6 companies due a 75-day crawl -> up to 75 days stale; pledge trend had a prior for 4 of 235 names; `pledge_increase` never fired; the sector percentile ranked a company against the day's handful. | **fixed** -- `fundamentals_l2_market_snapshot`, every L1 company every run, with `*_data_available` flags; confluence reads it. TODO C2's pledge half. |
| C3 | A failed pledge fetch stored 0.0 for everyone (the 2026-08-30 guard was never wired in) -> the next good fetch looked like a market-wide rise. | **fixed** -- NULL + flag. |
| C4 | `institutional_pct` NULL when both FII and DII rows are absent (~12% of latest rows) although the table parsed. | **fixed** -- 0 when the Promoters row is present. TODO C2's institutional half. |
| C5 | The company->L2 map covered only today's L1, so 19-22 watchlist names that left L1 scored with every L2 axis None (and passed "no contradicting axis" more easily). | **fixed** -- `build_l1_ticker_by_company_master_id(include_history=True)` for identity lookups. |
| C6 | Open positions on flagged/invalidated names were never re-scored, so no later contradiction could reach the exit. | **fixed** -- confluence scores active watchlist UNION open positions. |
| C7 | 48 of 137 watchlist names can never get a Weinstein stage (systrader's stage universe is NSE; these are mostly BSE-only), so they can never enter. | **fixed 2026-09-24** (operator: systrader computes it) -- systrade 9b15b79 serves every NSE series plus `BSE:<scrip>` keys and prefixes BSE history before an NSE listing; stockey looks up symbol -> rename alias -> BSE key. Active watchlist with a stage: 89 -> 126 of 138 (rest: genuinely short history or suspended). |
| C8 | NSE insider filings matched on the raw L1 slug, which is a BSE code for some NSE-listed names. | **fixed** -- also keyed by `company_master.nse_ticker`. |
| C9 | Watchlist-only names priced only while 'active' -> stale price -> flagged name flipped active for a day. | **fixed** -- all statuses priced; a stale price cannot un-flag. |

## Not stored / overwritten

| # | Finding | Status |
|---|---|---|
| S1 | Dedup on (isin, filing_type, disclosure_date) merged genuinely different same-day filings from ONE exchange (two insiders, standalone vs consolidated, NSE Disclosure1..N); the losers were never stored. | **fixed** -- merge only across exchanges, never across two named insiders. |
| S2 | The 7-day BSE re-crawl DO-UPDATEd every column: NSE insider fields wiped, `sources` reset, `enrichment_status` back to pending (daily rating re-searches), `load_ts` = now. | **fixed** -- insert-only for stored rows; only still-NULL attachment/identity fields are filled. |
| S3 | BSE insider trade fields lived only in extraction JSON (96% of `insider_name` NULL). | **fixed** -- promoted to flat columns (fill-only); 39 rows backfilled. |
| S4 | Positions stored no entry bar date; entries 2026-09-04..15 used the previous session's close, undetectably. | **fixed** -- `entry_price_date`. |
| S5 | Positions stored no confluence `score_version`; the v2 scorer fix closed ASHIANA (a shadow) as "a contradiction appeared". | **fixed** -- `score_version` + `baseline_*`; a version change re-baselines, same-version rise invalidates. ASHIANA's closed record is left as history. |
| S6 | No reporting-period label on L2 -> resolution could grade a forecast against the accounts that produced it (a re-crawl is a newer run_date, not a newer report). First target date ~2026-11-03. | **fixed** -- `balance_sheet_period`/`shareholding_period`; resolution requires a period ending after the forecast, before either path. |
| S7 | A half-year balance-sheet column mixed into the annual debt series. | **fixed** -- `annual_periods_only`. |
| S8 | OCR timeouts re-queued the same heavy documents at the head of every run, with no attempt limit. | **fixed** -- `ocr_timeouts` counter; timed-out rows go to the back; `timeout_exhausted` after 3. |

## Wrong type

| # | Finding | Status |
|---|---|---|
| T1 | `net_debt_rscr`/`net_debt_yoy_delta_rscr` BIGINT: a -0.4 cr delta stored as 0 and failed every "< 0" check. | **fixed** -- DOUBLE PRECISION (migration). |
| T2 | `nseindia_bulk_deals`/`_block_deals`.`date` TEXT. | **fixed** -- TIMESTAMPTZ (migration). |
| T3 | `fundamentals_events.announcement_timestamp` TEXT in four formats; 1,076 BSE values 5h30m off (IST labelled UTC before 2026-08-18). | **fixed** -- recomputed from raw `DissemDT`; column TIMESTAMPTZ. |
| T4 | `disclosure_date` as UTC calendar date (00:00-05:30 IST filings a day early); deal_flow rows as '2026-07-24 00:00:00+00'. | **fixed** -- IST date in both collectors; backfilled; ISO CHECK constraint (stays TEXT because the API serialises it verbatim). |
| T5 | 130 stored L3 alerts held a bare `NaN` in JSON (invalid; Starlette renders strictly). | **fixed** -- `utils/json_safe.py`; rows repaired. |
| T6 | `fundamentals_l3_alerts.l2_run_date` is TEXT. | open -- written, never read; low value. |

## Vocabulary mismatch / used wrong

| # | Finding | Status |
|---|---|---|
| V1 | Confluence trajectory axis dead (the trigger for this audit). | **fixed** -- v2. |
| V2 | L2 `reversal` was a catch-all (48% of rows) that dropped the latest move's sign -- the turnaround case scored "no call". | **fixed** -- `reversal_to_decline/_increase`, `new_decline/_increase`, `stalled`; confluence v3. Old rows keep `reversal` until re-crawled. |
| V3 | One sell alert counted in two confluence axes (ownership AND event). | **fixed** -- v3: alerts count once, in event_corroboration. |
| V4 | 79% of bulk-deal sells had a same-day buy by the same client (intraday churn) -> 90-day contradictions and exits. | **fixed** -- deal_flow nets per client/day; 184 of 234 stored deal alerts marked `superseded_round_trip` (kept, not counted). |
| V5 | Sector phase used at 'low' sample confidence (often one company). | **fixed** -- v3 reads no phase. |
| V6 | Confluence alert window keyed on disclosure date (alerts lag a median ~15 days) and DB clock. | **fixed** -- GREATEST(alert_date, detection date), IST. |
| V7 | NSE insider trades alerted regardless of person or mode (ESOP, gift, inter-se, employees). | **fixed** -- market trades by promoter/director/KMP only. |
| V8 | India Ratings "Affirms" classified 'other'. | **fixed**. |
| V9 | Veto guard compared dates: confluence re-scores daily, so "evidence moved" was always true (SHREEPUSHK re-asked 14x); vetoed and later-accepted names sat in both arms. | **fixed** -- content fingerprint; accept supersedes the vetoed shadow. |
| V10 | Action email listed vetoed shadows' closes as SELLs. | **fixed** -- accepted only. |
| V11 | Exit adjudicator saw no trigger detail, no current axes, no stage, no thesis -> deferred 0 of 10. | **fixed** -- evidence passed. |
| V12 | Decision log: resolutions as phase 'exit'; entry prompt version on stop-losses/resolutions; a default accept during an outage recorded under the model's name. | **fixed** -- 'resolution' phase; truthful model/prompt fields. |
| V13 | Results-calendar baseline took the newest-crawled row of any period. | **fixed** -- nearest entry to the filing, +-60 days. |
| V14 | Signal pointers' "latest" chosen by crawl time, not event time. | **fixed** -- disclosure_date. |
| V15 | Company-name joins by `REPLACE(id,'nse:','')` missed 17 of 142 watchlist names. | **fixed** -- via company_master (142/142). |
| V16 | Technicals preferred a stale EQ series over a fresh BE one, and priced BSE-slug NSE names off the BSE series. | **fixed**. |
| V17 | Unsupported rating agencies were a dead end, even after a plugin shipped; agency detected from headline only. | **fixed** -- retried; agency also read from the extracted filing. |
| V18 | `enrichment_status='pending'` on 1,306 rows that are never enriched. | **fixed** -- `not_applicable` (writer + backfill). |
| V19 | "Stage API may be frozen" logged daily for ~25 suspended tickers while the API was current. | **fixed** -- classified: frozen only when the newest read is stale. |
| V20 | API/run output reported the pre-bucket Rs 1 lakh / 100-name sizing. | **fixed**. |
| V21 | Stop basis text printed sigma, not the stop ("(4.8%) clamped" for a 7.2% stop). | **fixed**. |
| V22 | Scoring counted vetoed rows as open forecasts; breakdowns not split by method though documented as split. | **fixed** -- forecast rows only; `*_and_method` breakdowns added. |
| V23 | Bucket `min_adv_rs` configured, never applied; ADV read from an L1 row of any age. | **fixed**. |
| V24 | Closed positions on /portfolio showed P&L against today's price. | **fixed** (screener repo). |
| V25 | Frontend/digest labels missing for pledge_increase and bulk deals. | **fixed**. |

## DB safety

| # | Finding | Status |
|---|---|---|
| D1 | Unbounded "latest" reads on hypertable views: technicals (per ticker, per run), watchlist `load_price_near` (per row), a whole-view GROUP BY date, and the /portfolio API's per-row `last_price`. The OOM incident's shape. | **fixed** -- all date-bounded. |
| D2 | The test suite reached the live DB: measured with a plugin that makes any real connection raise, 37 tests touched it -- migrations ran for real, and two tests passed only because of what the live DB happened to hold. | **fixed** -- conftest neutralises `apply_schema_migration` at its DB boundary; the rest stubbed at their call sites. 2 remain on purpose: `test_confluence_evaluable_equals_supportive_plus_contradicting` (a live-data invariant) and `test_cron_preflight_validates_generated_crontab`. |

## Incidents during the audit itself

- **Mixed-version pipeline run.** Editing `utils/company_master.py` while the 15:30 UTC
  screener was running failed l3_triggers, confluence_score and llm_triage (TypeError):
  `run_step` re-imports only the step module. Re-run in a fresh process under the
  screener lock at 18:21 UTC; all five steps OK. Recorded as a CLAUDE.md working gotcha.
- **`/api/watchlist` 500 for ~45 min.** A `%` in a SQL *comment* this audit added
  ("~22% of the watchlist") -- the repo's oldest gotcha; tests stub `sql_to_df`, so they
  cannot see it. Fixed, API restarted, and `test_no_sql_literal_has_a_bare_percent` now
  scans every SQL string literal in `fundamentals/` and `utils/` via the AST.

## Price-data platform (checked, not changed)

The completeness gate failed every night 09-19..09-23 (`dhan_daily` at 10-43% of normal;
`intraday_freshness` a day behind on 09-22). Traced, not guessed:

- **09-22:** `all_dhan_auth_ensure.sh` failed at 02:05 UTC, so no Dhan token existed all
  day; the 17:45 UTC reconcile and the intraday sync died on it -> both 09-23 gate
  failures. Fixed by the operator the same day (a490c5f, web-login mobile resubmit);
  `auth_cli status` shows a valid token to 2026-09-24 07:35 IST.
- **09-19..09-21:** Dhan's daily endpoint is unreliable in the 07:40 IST window; fixed by
  the operator the same day (f41ee74: retries, cap 3000, morning pass moved to 12:00 IST).
- **Watch:** the gate (08:00 IST) now runs BEFORE the moved morning pass (12:00 IST), so
  for `dhan_daily` it judges the evening pass alone. If the first nights on f41ee74 still
  fail at 08:00 but pass by noon, move the gate after 12:00 IST (template + `builder.py`
  + go-crond restart, at a quiet moment -- a regenerate under a live go-crond stops all
  scheduling). Current data is whole: `dhan_ohlcv_daily` holds ~2,730 symbols for every
  session through 09-22.
- Collectors were not otherwise modified: the Dhan/auth path is operator-owned, and the
  coverage report is green on every table.

## 2026-09-24: running the data jobs one by one

Morning chain (cron) all exit 0; completeness gate PASSED with two warnings. Every price
table current through 09-23. Findings, each traced to source before fixing:

| # | Finding | Status |
|---|---|---|
| P1 | **NSE symbol renames broke identity platform-wide.** TATAMOTORS->TMPV, LTIM->LTM, INFIBEAM->CCAVENUE, HEG->HEGAM, AKZOINDIA->JSWDULUX, SEQUENT->VIYASH and 44 more: neither Sharpely nor Dhan's master follows a rename (Dhan keeps the old symbol, same ISIN and security_id), so the NEW symbol resolved to no company. Bhavcopy rows for TMPV/LTM/CCAVENUE had no company_master_id since at least June; Dhan daily for Tata Motors PV stopped 2026-07-14; it has NO intraday for all of 2026. | **fixed** -- `company_master_nse_alias` (ISIN -> Dhan id -> existing company id; no id changes), used by `map_company_master_ids` and `load_company_master_records`; 7,285 bhavcopy rows + 11 NSE tables backfilled. Daily gaps refill via the reconcile. Intraday history: see below. |
| P2 | **`dim_security` had no scheduled producer**: last built by hand ~2026-08-10, so listings/renames since had no ISIN, sector or company id (read by event dedupe, NSE PIT, L2 sector percentiles, confluence sector axis). | **fixed** -- rebuilt (7,069 -> 8,032, through 09-23); `security_history` + `security_dimension` + `backfill_company_master_ids` now in the download runner's parser steps (non-critical `identity_dimension`). |
| P3 | Dimension missed sectors Sharpely has under a different symbol. | **fixed** -- ISIN fallback: 482 -> 359 active EQ without sector (the 359 have none at source). |
| P4 | **`master_sharpely_equity` grew by ~6k rows every run**: its unique key treated NULL `bse_ticker` as distinct -- 346,274 rows for 6,396 companies, multiplied through security_dimension's join. | **fixed** -- deduped, `UNIQUE NULLS NOT DISTINCT`; ISIN now stored. `sharpely_id` is no longer returned by the source (ids derive from tickers, unaffected). |
| P5 | **Dhan EQ<->BE ids**: a stock moving between series keeps two active Dhan ids, and Dhan serves data only under the id of the series it trades THAT day (GATECH, BE all year: 374 intraday bars on BE, 0 on EQ; REPL, back to EQ 09-21: the reverse). company_master pinned the EQ id, so 115 of the 178 evening-reconcile failures were BE stocks, and 216 BE stocks were missing 51,234 intraday days over 5 years. (A same-morning revert, based on a 7-day series window that mixed switched stocks, was itself reversed once measured per day.) | **fixed** -- company_master picks the id of the latest bhavcopy day's series; daily sync retries the other id on a 400; daily keeps one bar per company-day (latest id wins) and starts from the company's last bar, not the id's -- 211,200 duplicate daily rows from an id switch removed; missing intraday days backfilled (see P12). |
| P6 | Reconcile logged "2,742 succeeded" for 09-23 while the gate found 1,406 symbols with a 09-23 bar: a successful call can return no bar for the expected day. | **fixed** -- reports `still_stale_after_sync` / `succeeded_without_expected_bar`. |
| P7 | `backfill_company_master_ids` covered 9 of 15 tables and had no alias; 287,886 historical bhavcopy rows plus mcap/CA rows were identifiable by ticker but never backfilled. | **fixed** -- 15 tables, alias fallback, run daily. |
| P8 | Genuinely unavailable at source (left as is): GOLDADD (new ETF, on neither feed); rights entitlements (`*-RE`) and brand-new listings on Dhan's historical API; 359 companies with no Sharpely sector; RBI bank rates (event table, last change 2025-12-05). | n/a |
| P10 | **Intraday history missing for all 50 renamed companies** (the 2026-08-22 5-year backfill could not resolve their new symbols; Tata Motors PV had zero 1-min bars). | **fixed** -- operator chose a full 5-year throttled backfill: 18.55M bars, 48 companies (+2 already complete), 0 failures, 41 min; WAL archive backlog peaked at 15 segments under a 150-segment kill guard; disk 203 -> 190 GB. Short histories are genuine listing dates (KDGREEN 2026-08-17). |
| P11 | Reconcile's staleness check looked renamed symbols up by the bhavcopy symbol while rows are stored under the canonical ticker -- re-fetched ~50 names every run and reported them stale forever. | **fixed** -- alias-aware. Midday 09-24 run: 269 stale -> 257 fetched (13 via the alternate BE id); 23 remain, all unavailable at source: 17 did not trade 09-23, GOLDADD (not on Dhan), CENTEXT-RE (rights entitlement), AMARJOTHI/GUJENERGY/GVPIL/VADILENT (Dhan's historical API refuses their only id; NSE bhavcopy has them). |
| P12 | Intraday gap for BE periods: 51,234 missing company-days across 216 stocks (5 years). | **fixed** -- targeted fill of exactly the missing IST days under the series-correct id (Dhan also serves some EQ days under the BE id, so a plain window refetch would have duplicated rows), DO NOTHING inserts, same WAL/disk kill guard. |
| P13 | 518,182 identical 1-min bars stored twice (under both Dhan ids) for 17 stocks, loaded 2026-04..09. | **fixed** -- kept the current id's bars. Note: on the compressed hypertable a self-referencing `DELETE ... USING` / `IN (subquery)` silently matched 0 rows; literal `timestamp = ANY(array)` batches worked. |
| P9 | A dedupe DELETE (self-join on IS NOT DISTINCT FROM) ran >10 min holding the migrations lock and queued three cron sessions. | cancelled via pg_cancel_backend within ~1 min of detection; rewritten as a one-pass window delete (0.5 s). |

## 2026-09-24 (afternoon): the gap list

Operator decisions taken first (all four "recommended"): mechanical veto horizon; systrader
computes stage for BSE names; only positive alerts add to the watchlist; rework the sector
phase over one company set.

| # | Finding | Status |
|---|---|---|
| G1 | Vetoed rows had no target date -> the accepted-vs-vetoed comparison could only ever hold accepts. | **fixed** -- `VETO_HORIZON_DAYS` gives rejects a mechanical horizon (6 existing rows backfilled); resolution skips them; `price_return_by_entry_decision` compares arms on closed-position price, shown on /portfolio. |
| G2 | Negative-only names (a lone `results_decline`/`bulk_deal_sell`) joined the watchlist and never left. | **fixed** -- only positive alerts add; such names exit as `no_thesis`. |
| G3 | **NSE SME boards (SM/ST) were outside every equity filter**: ~550 listed stocks had no adjusted prices, no Dhan id, no daily or intraday data -- several on the watchlist. | **fixed** -- adjusted view (new migration id), factors, company_master, universe, gate. 542/546 got Dhan ids; daily reconciled; 5-year intraday backfill: 80.9M bars, 546 symbols, 0 failures, 154 min, disk never below 163 GB. Gate after: 5 of 3,454 active symbols without intraday (was 546); 128 of 138 active watchlist names staged. |
| G4 | Sector phase compared SUMMED gross block (one company set) with median QUARTERLY sales growth (another); one large company set a sector (Chemicals -10.25 percent). | **fixed** -- one screener.in query gives gross block, prior gross block and annual `Sales growth` for the same companies; phase = median per-company gap; no phase for Financial Services / IT / Realty; confluence v4. |
| G5 | BSE-only companies never had a sector (dim_security is NSE-only): 16 of 17 sectorless watchlist names. | **fixed** -- `fundamentals_company_sector` view (dim_security, then Sharpely by NSE/BSE ticker) read by all six sector readers; watchlist 121 -> 137 of 138; L1 excluded from sector aggregation 38 -> 2. |
| G6 | "359 companies with no sector" (P3/P8) was mostly not companies. | **measured** -- of 358 active main-board symbols without a sector, 313 are ETFs, 12 Dhan "Other", 1 MF, 25 with no Dhan listing; 7 real equities (new listings / merger successors Sharpely has not classified yet). |
| G7 | Rights entitlements (`-RE`, `-RE1`) in the Dhan universe and the coverage gate. | **fixed** -- excluded from `get_equity_universe`, company_master's bhavcopy branch and `universe_coverage`. |
| G8 | The 584 failed BSE insider PDFs were held back on the premise that NSE's feed covers them. It carries PIT trades only (since 2026-08-17), not SAST. | **fixed** -- 152 SAST disclosures (Reg 29/31/10) re-admitted; ~390 trading-window notices stay out. (The 09-23 re-admission of ~414 other BSE rows was committed 09-24 08:45 UTC and applies in that evening's run.) |
| G9 | Dates stored as TEXT: `l3_alerts.l2_run_date` (`str(Timestamp)`), `sector_reference.as_of_date`. | **fixed** -- DATE by migration. `fundamentals_events.disclosure_date` stays TEXT with the ISO CHECK (09-23 decision). |
| G10 | Extraction enums (`period_type`, `rating_action`, `transaction_type`). | **fixed** -- strict-schema enums, verified live; no SCHEMA_VERSION bump (every stored value already fits). |
| G11 | Frontend: 6 typecheck errors; no label for `no_thesis`. | **fixed** -- 0 errors (screener 3005fc5). |
| G12 | Gate timing watch (gate 08:00 IST vs the 12:00 IST morning Dhan pass). | **watching** -- 09-24 gate PASSED on the evening pass alone, the first clean night since 09-13. The evening reconcile now overlaps the 18:10 UTC intraday sync (36 min on 09-23, longer with SME); both go through the cross-process Dhan gate, so it only queues. |

## Open, needing a decision

- Nothing from the 09-23 list. Watch items: G12 (gate timing).
