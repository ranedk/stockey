# Setup

## Python environment

Create the project environment and VS Code settings:

```sh
python builder.py
source .xstockey/bin/activate
```

`builder.py` is safe to import and only runs when executed directly. It also
creates `logs/cron` and installs `go-crond` if missing, resolving the binary
in this order:

1. use `GO_CROND_INSTALL_DIR` if that env var is set
2. otherwise install into the repo root as `./go-crond`
3. if `go-crond` already exists elsewhere on `PATH`, copy that binary into the repo root instead of downloading again

## PostgreSQL

```sh
sudo -u postgres psql
```

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

TimescaleDB is still in active use on the cloud DB — several KEEP tables,
including the primary adjusted price series, are hypertables. See
`DATA_CONTRACT.md` for the `hypertable_size()` note (plain
`pg_total_relation_size()` dramatically undercounts hypertables).

For provisioning a fresh remote Postgres/Redis server from scratch (ZeroTier
networking, UFW, moving data directories to an external volume), see
`docs/db_setup.md`.

## Redis backup

```sh
python utils/redis_bkp_restore.py --host localhost --port 6379 --db 0 --file backups/redis_global_backup.json backup
```

## S3 backup layout

```sh
aws s3 ls s3://stockeydata/
```

Expected prefixes:

- `bhavcopy/`
- `nsedeals/`
- `pgdump/`
- `rdbdump/`

## Data loaders

Symbol-specific loaders take `--symbols` or the `STOCKEY_SYMBOLS` env var
explicitly -- no static registry-file fallback. A collector that needs "the
current tradeable NSE universe" calls `utils/universe.py`'s
`get_equity_universe()`, which derives it live from the daily bhavcopy -- not
a file anyone has to remember to keep in sync.

The full daily pipeline is `./complete_data.sh` — see `README.md` for the
cron list and `docs/scripts.md` for the exact `data.download_runner` step
registries. Individual source commands, for debugging one collector:

### Dhan

```sh
python -m data.dhanlive.scrip_master
python -m data.dhanlive.auth_cli status
python -m data.dhanlive.ohlcv --symbols SHAKTIPUMP
python -m data.dhanlive.ohlcv --symbols NIFTY --asset-type benchmark --exchange NSE
python -m data.dhanlive.ohlcv_pull RELIANCE
```

By default the loader syncs 5 years of daily candles plus the last 1 day of
1-minute intraday candles. Supported asset types: `stock`, `index`, `benchmark`.
See `README.md` for Chrome CDP / auto-login setup.

### Sharpely (identity/sector mapping only)

```sh
python -m data.sharpelydata.scrip_master
```

`scrip_master.py` feeds `company_master`'s `sharpely_id` identity fallback and
`fundamentals/collectors/sector_data.py`'s sector mapping — see
`docs/DATA_INVENTORY.md` for the full collector/table inventory.

### RBI / FBIL

```sh
python -m data.rbi.download_bank_rates
python -m data.rbi.download_fbil_gsec
python -m data.rbi.download_currency_rates
```

### NSE bhavcopy, indices, identity

```sh
python -m data.nseindia.bhavcopy_downloader
python -m data.nseindia.indices_downloader
python -m data.nseindia.offmarket
python -m data.nseindia.bhavcopy_parser
python -m data.nseindia.indices_parser
python -m data.nseindia.offmarket_parser
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices
```

Chrome remote debugging is required for the Playwright/browser-driven flows:

```sh
scripts/start_chrome_cdp.sh
```

## Schema docs

```sh
python -m utils.db_schema_dump --schemas public
```

`docs/DATA_INVENTORY.md` is the table inventory; run `db_schema_dump` directly
for live column-level detail — there's no separate static schema dump doc to
keep in sync.

## Agent-facing tool surface

The curated agent-safe commands live in:

- [`docs/tool_registry.json`](tool_registry.json)
- `scripts/agent_tool_runner.py`

Quick checks:

```sh
python scripts/agent_tool_runner.py list
python scripts/agent_tool_runner.py list --category storage
```

The runner uses the invoking interpreter for downstream Python commands, so
starting it from the project venv keeps the whole tool chain in the same
environment. See `docs/agents.md` for the recommended agent-access split.

## Notes

1. `ISIN` is not unique. A single underlying can trade in multiple series.
2. `symbol + ISIN` is not unique across series.
3. Company renames usually change symbol, but not `ISIN`.
4. `dim_security_history` is the canonical source for rename continuity and review flags — see `docs/identity.md`.
5. `dim_security_overrides` is where manual merger / demerger / scheme mappings should be curated.
6. Large runtime artifacts such as `base_chromed_data/` and `http_cache/` should stay out of git.
7. Run `security_history`, `security_dimension`, and `adjusted_prices` sequentially, not in parallel — they touch the same derived identity tables.
