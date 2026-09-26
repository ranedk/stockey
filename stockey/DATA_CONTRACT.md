# Data Contract — stockey ⇄ systrader

*Canonical copy: `systrader/docs/DATA_CONTRACT.md`. A copy lives at
`stockey/DATA_CONTRACT.md`; when they disagree, this one wins.*

## Roles

- **stockey** — the data platform, pure TA scope (see
  `stockey/docs/DATA_INVENTORY.md` for the full collector/table inventory):
  bhavcopy, corporate actions, indices, calendar, Dhan broker data, RBI/FBIL
  rates, identity, historical mcap. Owns acquisition, corporate-action
  adjustment, instrument masters, Dhan browser auth, and data quality. Writes
  to the **cloud postgres** (small instance — see load rule). No research, no
  fundamental analysis, no news/announcement pipelines, no LLM-token
  consumption.
- **systrader** — the research & trading system. Owns all signals, research,
  backtesting, sizing, and execution. Works exclusively against the **local
  postgres `systrade`**, a mirror of the cloud DB. Research discipline
  (LEDGER, holdout burns, story rule) lives here and only here.

## TimescaleDB on the cloud DB

The `timescaledb` extension is installed on the cloud DB, and roughly 100
tables are active hypertables (check `timescaledb_information.hypertables`
for the current list — `dhan_ohlcv_daily`, `dhan_ohlcv_intraday`,
`nseindia_adjustment_factors`, `nseindia_corporate_actions_bc_raw`/
`_normalized`, `nseindia_indices`, `rbi_bank_rates`/`rbi_currency_rates`,
`events_dividend`/`events_capital_change`, `historical_mcap`, and most
`advisory_*` tables among them). Plain tables include `nseindia_ohlcv`,
`nseindia_mcap`/`mto`/`52wk`/`cmvolt`/`circuit_hit`/`var1`, `company_master`,
`master_dhan_instruments`, `dim_security`.

Two hazards to know before touching a hypertable:

- **Size estimation**: `pg_total_relation_size('some_hypertable')` only
  measures the empty parent shell and dramatically undercounts real size —
  use `hypertable_size()` / `timescaledb_information.hypertables` for any
  capacity planning, never the plain postgres size functions.
- **Backup/copy**: plain `\copy hypertable_name TO ...` and
  `pg_dump -t hypertable_name` silently copy **zero rows** — hypertable data
  lives in per-chunk child tables the parent shell doesn't expose to those
  forms, and neither errors, it just produces a small, valid-looking, empty
  result. Always wrap the table in a `SELECT`: `\copy (SELECT * FROM
  hypertable_name) TO ...` — this is why `sync_from_stockey.sh`'s own
  FULL/INCR loops use that form rather than a bare table name. Verify any
  backup by checking its actual row count before trusting it — a `pg_dump`
  file existing, or a `pg_restore -l` table-of-contents entry being present,
  does not prove the data section has any rows.

Plain queries (`SELECT`/`COUNT`/`WHERE`) work transparently against a
hypertable — only size-estimation and the copy forms above need care.

## The load rule (why the mirror exists)

The cloud DB is small and must never see heavy load. Therefore:

1. Backtests, scans, and any read-heavy work run ONLY against local
   `systrade`, never against the cloud DB.
2. `systrader/scripts/sync_from_stockey.sh` pulls cloud → local (atomic
   spooled copies; incremental tables are fingerprint-reconciled because
   historical rewrites happen). It is the only reader that touches the
   cloud DB from this side, once a day.
3. Direction of truth today: cloud → local. Planned evolution: local becomes
   the TA/research workhorse and *publishes* selected outputs local → cloud
   (order sheets, signals) into `systrader_*` tables. One writer per table
   namespace, always.

## Table API

The schemas of these tables ARE the API. Additive changes are safe (sync
detects column drift and recreates); renames/drops must be coordinated on
both sides.

| Table | Producer (stockey) | Notes |
|---|---|---|
| advisory_adjusted_ohlcv_daily | VIEW over `nseindia_ohlcv` × `nseindia_adjustment_factors` (the latter written by `data/nseindia/price_adjustment.py`) | PRIMARY equity series (2013+, CA-adjusted + total-return-adjusted, incl. delisted). Series EQ/BE, plus NSE SME (SM/ST) since 2026-09-24 -- filter `series = 'EQ'` for backtests. Not a written table — `\copy (SELECT * FROM ...)` works against it exactly like a table |
| nseindia_ohlcv | `data/nseindia/bhavcopy_history.py` | raw OHLC; source of adjusted opens |
| nseindia_mto | `data/nseindia/bhavcopy_parser.py` (`parse_mto`) | security-wise delivery position, 2013+: traded vs deliverable quantity and delivery %, one row per (date, symbol), all series. Retired 2026-08-15, revived 2026-09-11 for systrader's trait library. Raw (unadjusted) quantities — use the ratio, or adjust volumes before comparing across a split |
| nseindia_circuit_hit | `data/nseindia/bhavcopy_parser.py` (`parse_circuit_hit`) | price-band hits, 2013+: one row per (date, symbol, series, H/L) for each stock that hit its upper (H) or lower (L) band that day. The file lists hits only — a stock-day with no row had no hit, not missing data. Retired 2026-08-15, revived 2026-09-11 for systrader's trait library |
| nseindia_indices | `data/nseindia/indices_downloader.py` | mixed-case index names ("Nifty 50"); PE/PB/div yield = carry inputs |
| dhan_ohlcv_daily | `data/dhanlive/ohlcv_pull.py` | fallback only (2021+ coverage) |
| dhan_ohlcv_intraday | `data/dhanlive/ohlcv.py` (via `sync_many_intraday`) | 1-min bars, live in the cloud DB (not a local-only landing zone as originally planned). systrader reads this table live via a `postgres_fdw` foreign table (2026-08-26 — both Postgres instances are colocated on one disk, so a local mirror would duplicate the same bytes for no isolation benefit); NOT in `sync_from_stockey.sh`'s table lists, and shouldn't be added there — that script's own daily-bar sync model is superseded here by the FDW read. See `docs/HF_DATA_PLATFORM_PLAN.md` for the fuller history |
| master_dhan_instruments | `data/dhanlive/scrip_master.py` | security ids, lots, expiries |
| dim_security | `data/nseindia/security_history.py` + `data/nseindia/security_dimension.py` (scheduled in `download_runner`'s parser steps since 2026-09-24 -- before that it had no scheduled producer and was frozen at 2026-08-10) | identity mapping; sector_code falls back to Sharpely by ISIN |
| company_master | `utils/company_master.py` (`data/company_master.py`) | one row per company: nse_ticker, bse_ticker/bse_scrip_code, Dhan ids (the id of the series the stock trades in). Synced (full) since 2026-09-24 for the stage API's NSE<->BSE join |
| company_master_nse_alias | `utils/company_master.py` (`sync_nse_symbol_aliases`) | NSE symbol renames (TMPV -> nse:TATAMOTORS): current bhavcopy symbol -> existing company_master_id, by ISIN. Synced (full) since 2026-09-24 |
| bse_advisory_adjusted_ohlcv_daily | VIEW over `bseindia_ohlcv` x BSE adjustment factors (`data/bseindia/price_adjustment.py`) | BSE adjusted series (2025-08+), keyed by scrip_code. Synced since 2026-09-24 for the stage API ONLY (BSE-only names, and pre-NSE-listing history); not a backtest series |
| nseindia_corporate_actions_bc_raw / nseindia_corporate_actions_normalized | `data/nseindia/bhavcopy_parser.py` / `data/nseindia/adjusted_prices.py` | `_bc_raw` (bhavcopy CA feed) is the comprehensive corporate-actions source; `_normalized` derives from it |
| nseindia_mcap | `data/nseindia/bhavcopy_parser.py`'s `parse_mcap` | point-in-time universe, 2024-02+ |
| historical_mcap | frozen archive, no longer written | pre-2024 market-cap history (2012+, ~970 symbols) `nseindia_mcap` doesn't have |
| nseindia_holidays | `data/nseindia/holidays.py` | |
| dim_trading_days | `data/nseindia/calendar_creator.py` (restored 2026-09-02, parser step after bhavcopy) | trading-day calendar; populated through the end of the current year |
| rbi_bank_rates / fbil_gsec_par | `data/rbi/*` | risk-free (repo/T-bill) + G-sec carry |
| events_dividend | `data/nseindia/corporate_action_events.py` | keyed on `ex_date`; a minority of rows lack `dividend_amount` — TR math must handle nulls. Input for NIFTY TR benchmark + equity carry |
| events_capital_change | `data/nseindia/corporate_action_events.py` | |
| rbi_currency_rates | `data/rbi/download_currency_rates.py` | |

**systrader never writes any table above** — not in the cloud, not in its
local mirror copies (sync truncates them). systrader-owned data lives in
`systrader_*` tables (today: `systrader_ohlcv_daily`).

## Auth

stockey owns the Dhan login (CDP Chrome + TOTP). systrader only reads the
token cache at `DHAN_TOKEN_CACHE`
(`$STOCKEY_DIR/.cache/dhan_access_token.json`). Token expires daily
~17:20 IST; refresh is always `python -m data.dhanlive.web_login` in stockey.

## Research boundary

- Ideas graduate from stockey to systrader by **re-implementation as storied
  rules through `research/LEDGER.md`** — never by copying code.
- Any experiment on data both repos can see must be exportable as a trial
  count into systrader's LEDGER before claiming significance on that data.
- The TimesFM/ML sidecar (Python) lives in the systrader repo: it is
  research, and it must live where the LEDGER lives.
