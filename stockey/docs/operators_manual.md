# Operator Manual

Short runbook for running stockey day to day. For the full script/registry
inventory see `docs/scripts.md`; for cron scheduling see `README.md` and
`config/stockey.crontab.template`.

## Python interpreter behavior

All top-level shell wrappers resolve Python through `scripts/resolve_python.sh`, in this order:

1. `PYTHON_BIN` if set explicitly
2. the active virtualenv `python` (after `source .xstockey/bin/activate`)
3. the repo-local fallback at `.xstockey/bin/python`
4. system `python3`, then `python`

So interactive use after activating the venv works naturally, and cron can
call the shell wrappers directly without activating the venv first, as long
as `.xstockey` exists. Bootstrap with:

```sh
python builder.py
```

This creates/refreshes the venv, creates `logs/cron`, and installs `go-crond`
in the repo root if missing.

## Chrome CDP session

Dhan's Playwright-based auto-login needs a Chrome remote-debugging session:

```sh
scripts/start_chrome_cdp.sh
```

Keep this in `.env`:

```sh
CDP_ENDPOINT=http://localhost:9222
```

Useful overrides:

```sh
CDP_PORT=9223 CHROME_USER_DATA_DIR=~/.stockey/chrome-cdp-9223 scripts/start_chrome_cdp.sh
CHROME_BINARY="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" scripts/start_chrome_cdp.sh
```

This starts Chrome in the foreground with a separate user-data directory;
stop it with `Ctrl-C`. Dhan login attempts are serialized across processes
(`data/dhanlive/auth.py`'s `_dhan_login_lock`) so concurrent logins don't
trigger Dhan's "too many attempts" block — do not bypass that lock.

## Scheduled runs

```sh
python builder.py
python scripts/cron_preflight.py
./start_cron.sh
```

`start_cron.sh` refuses to start a second go-crond if one is already running,
otherwise always reconciles OHLCV coverage first (so a scheduler that was
down during market hours doesn't start the day on stale bars), then execs
`./go-crond config/stockey.generated.crontab --allow-unprivileged`. Stop it
with `./stop_cron.sh` (SIGTERM, escalates to SIGKILL after
`STOCKEY_CRON_STOP_TIMEOUT_SECONDS`, default 15s; a no-op if not running) --
note `scripts/ensure_go_crond_alive.sh`, if registered in the OS crontab,
will auto-restart it within its own schedule unless you disable that entry
separately. `./restart_cron.sh` chains the two safely (waits for the actual
stop, then reuses `start_cron.sh` unmodified, including its already-running
guard). See `README.md`'s pipeline list for what runs and when. The
generated cron file includes a username column (go-crond/system-crontab
style); a normal per-user
`crontab` needs that column removed first.

To run the full pipeline immediately instead of waiting for the schedule:

```sh
./complete_data.sh && ./all_ohlcv_reconcile.sh && ./all_price_adjustment.sh && ./all_data_readiness.sh
```

## Main operating modes

### Complete data refresh

```sh
./complete_data.sh
```

Runs every downloader module then every parser module from
`data.download_runner`'s registries, in order, and writes a combined status
summary. Use this as the catch-up/backfill path after a missed day.

### Queued mode (normal cadence)

```sh
./all_downloaders_queue.sh
./all_external_workers.sh
```

`all_downloaders_queue.sh` enqueues single-client NSE/Dhan work into Redis;
`all_external_workers.sh` drains those queues serially so parallel NSE/Dhan
browser/API sessions don't collide. Runs 3x/day; `complete_data.sh` alone
only covers the same modules at its 2x/day cadence.

### OHLCV reconcile + price adjustment

```sh
./all_ohlcv_reconcile.sh --dry-run --format json   # inspect first
./all_ohlcv_reconcile.sh
./all_price_adjustment.sh
```

Reconcile backfills any universe symbol whose latest Dhan daily bar predates
the last completed trading day. Price adjustment rebuilds
`advisory_adjusted_ohlcv_daily` from price steps (corroborated by declared
NSE corporate actions) — run reconcile first so the adjustment sees fresh bars.

### Data readiness + coverage

```sh
python -m data.data_readiness --fix
./all_data_coverage_report.sh
```

Readiness checks bhavcopy/Dhan/benchmark freshness and runs bounded repairs;
pass `--require` to gate (exit 1 on remaining hard errors). The coverage
report is non-fatal visibility across every KEEP table — see
`docs/DATA_COVERAGE.md`.

## Database robustness knobs

The shared DB helper (`utils/db.py`) retries transient read/connect/metadata/
upsert failures against the cloud Postgres, disposes the stale SQLAlchemy
pool, reconnects, and writes large upsert payloads through local temp files
before `COPY`.

- `SQL_TO_DF_RETRIES` (default `2`), `SQL_TO_DF_RETRY_SLEEP_SECONDS` (default `1.0`)
- `SQL_TO_DF_STATEMENT_TIMEOUT_MS` (default `0`, leaves server default), `SQL_TO_DF_CHUNK_SIZE` (default `0`, fetch-all)
- `DB_OPERATION_ATTEMPTS` (default `3`), `DB_POOL_RECYCLE_SECONDS` (default `300`)
- `DB_RETRY_TELEMETRY_FILE` — local JSONL spool for Postgres retry/exhaustion events, default `logs/fallback/db_retry_events.jsonl`
- `LOCAL_FALLBACK_TELEMETRY_FILE` — local JSONL spool for fallback events that shouldn't attempt a DB write, default `logs/fallback/local_fallback_events.jsonl`

These are file-backed (not DB-backed) so they still work when Postgres itself
is the failing component. Inspect with
`python scripts/fallback_telemetry_coverage_report.py --format json` and
`python scripts/db_retry_coverage_report.py`.

## Redis robustness knobs

Redis holds lightweight cursors and the download queue; it is not the source
of truth (Postgres upserts are). Ingestion jobs retry Redis operations and,
by default, continue without Redis state if Redis is unavailable.

- `REDIS_OPERATION_ATTEMPTS` (default `3`), `REDIS_RETRY_SLEEP_SECONDS` (default `1.0`)
- `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS` (default `2.0`), `REDIS_SOCKET_TIMEOUT_SECONDS` (default `5.0`)
- `REDIS_FAIL_SOFT` (default `true`) — set `false` only when debugging Redis itself

## Important inspection commands

```sh
python scripts/cron_preflight.py
python scripts/data_coverage_report.py --format json
python scripts/price_data_sanity.py
python -m data.dhanlive.auth_cli status
```
