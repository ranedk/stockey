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

Both DBs are plain PostgreSQL. TimescaleDB was tried and removed
(error-prone); nothing may assume hypertables.

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
| advisory_adjusted_ohlcv_daily | `advisory/price_adjustment.py` → **must move to `data/`** before the advisory strip | PRIMARY equity series (2013+, CA-adjusted, incl. delisted) |
| nseindia_ohlcv | `data/nseindia/bhavcopy_history.py` | raw OHLC; source of adjusted opens |
| nseindia_indices | `data/nseindia/indices_downloader.py` | mixed-case names ("Nifty 50"); PE/PB/div yield = carry inputs |
| dhan_ohlcv_daily | `data/dhanlive/ohlcv_pull.py` | fallback only (2021+ post-reorg) |
| master_dhan_instruments | `data/dhanlive/scrip_master.py` | security ids, lots, expiries |
| dim_security | `data/nseindia/security_history.py` | identity mapping |
| nseindia_corporate_actions | `data/nseindia/corporate_action_events.py` | |
| nseindia_mcap / historical_mcap | bhavcopy parser / sharpely | point-in-time universe |
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

## Open decision — 1-minute ticks (~mid-Aug 2026)

The small cloud DB cannot hold tick volume (~1M+ rows/day for equity+F&O).
Proposal: raw 1-min bars land DIRECTLY in local `systrade` (or parquet files
loaded locally); the cloud keeps only daily bars and small aggregates. This
inverts the acquisition flow for ticks only — acceptable because the load
rule dominates. Format on arrival is unknown; a parser will convert into the
canonical bar shape (instrument id, UTC exchange timestamp, OHLCV, OI
nullable, frequency tag).
