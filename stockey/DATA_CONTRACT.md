# Data Contract — stockey ⇄ systrader

*Canonical copy: `systrader/docs/DATA_CONTRACT.md`. A copy lives at
`stockey/DATA_CONTRACT.md`; when they disagree, this one wins.*
*Agreed 2026-07-27.*

## Roles

- **stockey** — the data platform, PURE TA scope (operator decision
  2026-07-27, see `stockey/docs/DATA_INVENTORY.md` for the full keep/remove
  inventory): bhavcopy + corporate actions + indices + calendar + Dhan +
  RBI/FBIL + identity + historical mcap. Owns acquisition, corporate-action
  adjustment, instrument masters, Dhan browser auth, and data quality.
  Writes to the **cloud postgres** (small instance — see load rule).
  NO research, NO fundamental analysis, NO news/announcement pipelines,
  NO LLM-token consumption (see `stockey/docs/ADVISORY_SPLIT.md`).
- **systrader** — the research & trading system. Owns ALL signals, research,
  backtesting, sizing, and execution. Works exclusively against the **local
  postgres `systrade`**, a mirror of the cloud DB. Research discipline
  (LEDGER, holdout burns, story rule) lives here and only here.

**Correction (2026-08-05): this was wrong.** The prior claim here — "both DBs
are plain PostgreSQL, TimescaleDB was tried and removed, nothing may assume
hypertables" — does not hold on the cloud DB. The `timescaledb` extension is
installed there and ~100 tables are still active hypertables, including
`advisory_adjusted_ohlcv_daily` (the PRIMARY series), `dhan_ohlcv_daily`,
`dhan_ohlcv_intraday`, `nseindia_corporate_actions_bc_raw`/`_normalized`,
`nseindia_indices`, `rbi_bank_rates`/`rbi_currency_rates`,
`events_dividend`/`events_capital_change`, `historical_mcap`, and dozens of
`advisory_*` tables (confirmed via `pg_extension` +
`timescaledb_information.hypertables`). Plain tables (not hypertables)
include `nseindia_ohlcv`, `nseindia_mcap/mto/52wk/cmvolt/circuit_hit/var1`,
`company_master`, `master_dhan_instruments`, `dim_security`.

Queries against a hypertable work transparently (a plain `SELECT`/`COUNT`
needs no hypertable-aware syntax) — nothing has broken because of this. The
real hazard is **size estimation**: `pg_total_relation_size('some_hypertable')`
only measures the empty parent shell and dramatically undercounts (found via
`dhan_ohlcv_intraday`: 40 kB via `pg_total_relation_size` vs. 11 GB via
TimescaleDB's own `hypertable_size()`, for 22M real rows) — use
`hypertable_size()` / `timescaledb_information.hypertables` for any
capacity planning or size audit on a table in the list above, never the
plain postgres size functions. `systrader/scripts/sync_from_stockey.sh`'s
`\copy`-based sync still works correctly against these tables (transparent
querying), but its stated rationale ("both sides are plain postgres") is
inaccurate.

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
detects column drift and recreates); renames/drops must be coordinated —
2026-07-26 showed a reorg can silently orphan downstream code.

| Table | Producer (stockey) | Notes |
|---|---|---|
| advisory_adjusted_ohlcv_daily | `data/nseindia/price_adjustment.py` (promoted from `advisory/` 2026-07-28) | PRIMARY equity series (2013+, CA-adjusted, incl. delisted) |
| nseindia_ohlcv | `data/nseindia/bhavcopy_history.py` | raw OHLC; source of adjusted opens |
| nseindia_indices | `data/nseindia/indices_downloader.py` | mixed-case names ("Nifty 50"); PE/PB/div yield = carry inputs |
| dhan_ohlcv_daily | `data/dhanlive/ohlcv_pull.py` | fallback only (2021+ post-reorg) |
| dhan_ohlcv_intraday | `data/dhanlive/ohlcv.py` (via `sync_many_intraday`) | 1-min bars, ALREADY LIVE since 2025-09-22 (22M+ rows, 630 tickers as of 2026-08-04) — corrected 2026-08-05, this was previously (wrongly) described below as a not-yet-started future landing zone |
| master_dhan_instruments | `data/dhanlive/scrip_master.py` | security ids, lots, expiries |
| dim_security | `data/nseindia/security_history.py` | identity mapping |
| nseindia_corporate_actions_bc_raw / nseindia_corporate_actions_normalized | `data/nseindia/bhavcopy_parser.py` / `data/nseindia/adjusted_prices.py --only normalize` | corrected 2026-08-05: the plain `nseindia_corporate_actions` table (previously listed here) is no longer written — its collector only ever covered 2 placeholder symbols and was unscheduled; `_bc_raw` (bhavcopy CA feed, 5,728 symbols) is the comprehensive source, `_normalized` derives from it (now scheduled daily, was 1yr+ stale until fixed) |
| nseindia_mcap | `data/nseindia/bhavcopy_parser.py`'s `parse_mcap` | point-in-time universe, 2024-02+ |
| nseindia_holidays | `data/nseindia/holidays.py` | |
| dim_trading_days | producer unidentified — locate before relying on it | |
| rbi_bank_rates / fbil_gsec_par | `data/rbi/*` | risk-free (repo/T-bill) + G-sec carry |
| events_dividend | `data/nseindia/corporate_action_events.py` | restored 2026-07-27: 21k rows, 2,603 symbols, 2013+, keyed on `ex_date`; ~19% rows lack `dividend_amount` — TR math must handle nulls. Input for NIFTY TR benchmark + equity carry |
| events_capital_change | `data/nseindia/corporate_action_events.py` | restored 2026-07-27 (1,152 rows) |
| rbi_currency_rates | `data/rbi/download_currency_rates.py` | restored 2026-07-27 (1,949 rows) |

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
- stockey's historical experiment records (`advisory/research_ledger.py`,
  `multiple_testing.py` outputs) are prior trials: export and fold them into
  systrader's LEDGER M-count before claiming any significance on datasets
  they touched.
- The TimesFM/ML sidecar (Python) lives in the systrader repo: it is
  research, and it must live where the LEDGER lives.

## 1-minute ticks — already live, NOT where this doc originally planned

**Corrected 2026-08-05.** This section previously described 1-min bars as a
future proposal ("~mid-Aug 2026", "lands DIRECTLY in local `systrade`, cloud
keeps only daily bars"). Reality, found during the 2026-08-05 completeness
sweep: `dhan_ohlcv_intraday` has been live in `data/dhanlive/ohlcv.py` since
2025-09-22 and already holds 22M+ rows (630 tickers, 11 GB as a TimescaleDB
hypertable — see the correction note above) — and it landed in the **cloud**
DB, the opposite of the original proposal, which specifically wanted to keep
tick volume OUT of the small cloud instance. `systrader/scripts/sync_from_stockey.sh`
does not currently sync this table at all, so systrader has no access to it
despite it existing for nearly a year.

Canonical bar shape delivered: `company_master_id, exchange, ticker,
security_id, exchange_segment, instrument, interval_minutes, timestamp,
open, high, low, close, volume, open_interest, load_ts, asset_type` — matches
the originally-proposed shape.

Open items this leaves unresolved (operator decision needed, not made here):
whether to (a) leave it in the cloud DB as-is and add it to systrader's sync
list, accepting the load-rule violation since it appears to be within the
instance's real capacity (11 GB against a 98 GB database), (b) migrate it to
a local-only landing zone per the original plan, or (c) something else. This
doc will not decide that unilaterally — flag for the next dhan_ohlcv_intraday
work.
