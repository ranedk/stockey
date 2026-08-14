# Stockey

Stockey is a **pure data platform** for Indian-equity price/reference data. It
collects, adjusts, and identity-maps bhavcopy, corporate actions, indices,
calendar, Dhan broker, and RBI/FBIL rate data, then writes it to the cloud
Postgres for `systrader` (the sibling research/trading repo) to consume. It
does no research, signal generation, backtesting, sizing, execution,
fundamental analysis, news/announcement processing, or LLM-token consumption
— all of that lives in `systrader`.

See `CLAUDE.md` for the full operating guide, `docs/DATA_INVENTORY.md` for the
collector/table inventory, and `docs/DATA_COVERAGE.md` for what's inside each
table and its coverage. `DATA_CONTRACT.md` is the boundary contract with
systrader (table API, cloud-DB load rule, Dhan auth handoff).

## Pipeline

The whole pipeline is cron jobs defined in `config/stockey.crontab.template`:

1. `complete_data.sh` (07:10 + 17:30) — `data.download_runner --phase all`:
   downloads + parses NSE bhavcopy/indices/corporate-actions/holidays, Dhan
   scrip master + OHLCV, RBI/FBIL rates, and normalizes
   corporate actions.
2. `all_downloaders_queue.sh` + `all_external_workers.sh` (08:30/12:30/16:30
   and +5 min) — queue single-client NSE/Dhan work and drain it, so parallel
   NSE/Dhan sessions don't collide.
3. `all_ohlcv_reconcile.sh` (18:45) — backfills any universe symbol whose
   latest Dhan daily bar predates the last completed trading day.
4. `all_price_adjustment.sh` (18:50) — rebuilds `nseindia_adjustment_factors`,
   the factor table behind `advisory_adjusted_ohlcv_daily` (a view), systrader's
   PRIMARY equity series.
5. `all_data_readiness.sh` (22:30) — checks bhavcopy/Dhan/benchmark freshness
   and runs bounded repairs.
6. `all_data_coverage_report.sh` — non-fatal daily coverage/health report
   across every KEEP table (see `docs/DATA_COVERAGE.md`).
7. Log rotation (06:50, `scripts/rotate_logs.sh`).

`data/nseindia/earnings_events.py` and `data/nseindia/recent_events.py` are
BORDERLINE (LLM-free, kept for possible future FnO event-vol research) — the
files stay but are deliberately not scheduled.

## Key commands

Daily/operator (matches the crontab):

```sh
./complete_data.sh
./all_downloaders_queue.sh
./all_external_workers.sh
./all_ohlcv_reconcile.sh
./all_price_adjustment.sh
./all_data_readiness.sh
./all_data_coverage_report.sh
```

Cron:

```sh
python builder.py
python scripts/cron_preflight.py
./start_cron.sh          # supported way to (re)start go-crond; reconciles OHLCV first
```

Dhan/NSE browser-backed automation needs a Chrome remote-debugging session:

```sh
scripts/start_chrome_cdp.sh
```

Keep `CDP_ENDPOINT=http://localhost:9222` configured. Dhan auto-login fails
hard when Chrome/CDP is unavailable. Login attempts are serialized across
processes (`data/dhanlive/auth.py`'s `_dhan_login_lock`) so concurrent
processes don't trigger Dhan's "too many attempts" block.

## Setup

```sh
python builder.py
source .xstockey/bin/activate
```

`builder.py` is safe to import and only runs when executed directly; it also
creates `logs/cron` and installs `go-crond` if missing.

```sql
CREATE DATABASE stockey;
CREATE USER stockey WITH ENCRYPTED PASSWORD 'stockey';
GRANT ALL PRIVILEGES ON DATABASE stockey TO stockey;
ALTER DATABASE stockey OWNER TO stockey;
\c stockey
GRANT ALL ON SCHEMA public TO stockey;
GRANT USAGE ON SCHEMA public TO stockey;
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

See `docs/index.md` for Redis/S3 backup layout and per-source run commands,
and `docs/db_setup.md` for provisioning a fresh Postgres/Redis server.

## Other docs

- `docs/scripts.md` — script inventory.
- `docs/operators_manual.md` — daily runbook and cron behavior.
- `docs/db_setup.md` — provisioning a fresh Postgres/Redis server.
- `docs/utils.md` — one-off utility commands (table dump/restore, OCR, identity review).
- `docs/identity.md` — the `dim_security*` identity layer.
- `docs/security_series.md` — NSE security series tag reference.
- `docs/agents.md` — notes for exposing this repo to agent/LLM tool access.
