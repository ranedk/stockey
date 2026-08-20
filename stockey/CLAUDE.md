# CLAUDE.md

This file is the operating guide for AI coding agents working on Stockey.

Stockey is a **pure data platform** for Indian-equity price/reference data. It
collects, adjusts, and identity-maps bhavcopy, corporate actions, indices,
calendar, Dhan broker, and RBI/FBIL rate data, then writes it to the cloud
Postgres for `systrader` to consume. It does **no** research, signal
generation, backtesting, sizing, execution, fundamental analysis, news/
announcement processing, or LLM-token consumption — all of that lives in
`systrader` (Go; checked out locally as `~/code/trading/systrader` or
`~/code/trading/systrade` depending on the machine — check `ls ~/code/trading/`
if unsure). See `docs/DATA_INVENTORY.md` for the authoritative collector/table
inventory.

Treat this as data-collection infrastructure, not a research app: correctness,
point-in-time discipline, and visible failure matter more than features. Do not
reintroduce research/signal/LLM logic here — it belongs in systrader.

## Project Principles

- Every fallback, degradation, skipped source, stale input, and data-quality issue
  should be visible through fallback telemetry, sync state, or logs. Avoid silent
  fallback.
- Preserve point-in-time behavior. Do not use future data, backdated assumptions,
  stale latest rows, or non-causal labels.
- Corporate-action adjustment is derived from price steps first
  (`data/nseindia/price_adjustment.py`), corroborated by declared NSE corporate
  actions (`nseindia_corporate_actions_bc_raw`/`_normalized`) where available —
  never guess an adjustment factor. Only the compact per-(symbol,date) factor is
  written (`nseindia_adjustment_factors`); `advisory_adjusted_ohlcv_daily`
  (systrader's PRIMARY series) is a VIEW over that table joined with raw
  `nseindia_ohlcv`, not a separately-maintained price series.
- Do not mutate broker/auth behavior (Dhan login, CDP) unless the user explicitly
  asks for that task — a botched change here can lock out the account (see
  `data/dhanlive/auth.py`'s login lock and timezone-aware expiry check).
- systrader owns all research, signals, and trading decisions. If a task looks
  like it needs a trading rule, a backtest, a scoring model, or an LLM call over
  market data, it belongs in systrader, not here.
- No static symbol/company registry file for a collector to fall back to. A
  collector that needs "the current tradeable NSE universe" calls
  `utils/universe.py`'s `get_equity_universe()` (derives it live from the daily
  bhavcopy). A collector that deliberately needs only a small sample (e.g. a
  connectivity/auth smoke test) derives it dynamically too — see
  `data/download_runner.py`'s `_dhan_precheck_symbols()`. No hardcoded ticker
  names anywhere in this pipeline outside tests.
- Do not call `page.goto()` (or `requests.get()`) against nseindia.com /
  nsearchives.nseindia.com directly from a new collector — NSE's Akamai WAF
  blocks fast on request rate and effectively requires a real browser. Route
  every navigation through `utils/nse_rate_limiter.py`'s `nse_goto(page, url)`
  (or `nse_request_gate()` for plain `requests` calls); it enforces a
  cross-process floor and serializes so no two processes ever have an NSE
  request in flight at once — bypassing it for "just this one call" defeats the
  point, since the WAF scores the domain's total request rate, not per-script.

## Current Architecture

The core pure-TA pipeline is 7 cron jobs (`config/stockey.crontab.template`).
Times below are the intended IST wall-clock schedule; go-crond
(webdevops/go-crond) has no `CRON_TZ`/`TZ` support, so the crontab file itself
is written in UTC (IST − 5:30) — see the per-job comments in the template for
each line's UTC/IST pair:

1. `complete_data.sh` (07:10 + 17:30) — runs `data.download_runner --phase all`:
   downloads + parses NSE bhavcopy/indices/corporate-actions/holidays, BSE
   bhavcopy (BSE-only companies, `data/bseindia/bhavcopy.py` — a fundamentals-
   screener gap fix, not part of systrader's PRIMARY series), Dhan scrip
   master + OHLCV, RBI/FBIL rates, and normalizes corporate actions. See
   `data/download_runner.py`'s `DOWNLOADER_STEPS` / `PARSER_STEPS` for the
   exact registry.
2. `all_downloaders_queue.sh` + `all_external_workers.sh` (08:30/12:30/16:30 and
   +5 min) — queues single-client NSE/Dhan work (`data/download_queue.py`) and
   drains it (`utils/external_task_queue.py`) so parallel NSE/Dhan sessions don't
   collide.
3. `all_ohlcv_reconcile.sh` (18:45) — backfills any universe symbol whose latest
   Dhan daily bar predates the last completed trading day
   (`data/dhanlive/ohlcv_reconcile.py`).
4. `all_price_adjustment.sh` (18:50, right after the reconcile) — rebuilds
   `nseindia_adjustment_factors`, the compact factor table behind
   `advisory_adjusted_ohlcv_daily` (a view, systrader's PRIMARY equity series),
   then its BSE-only-company twin (`bseindia_adjustment_factors` /
   `bse_advisory_adjusted_ohlcv_daily`, `data/bseindia/price_adjustment.py`).
5. `all_data_readiness.sh` (22:30) — `data/data_readiness.py --fix`: checks
   bhavcopy/Dhan/benchmark freshness and runs bounded repairs.
6. `all_data_coverage_report.sh` (17:10 UTC) — `scripts/data_coverage_report.py`:
   non-fatal per-table coverage/staleness report (`docs/DATA_COVERAGE.md`),
   monitoring only, not a data producer.
7. Log rotation (06:50, `scripts/rotate_logs.sh`).

`data/nseindia/earnings_events.py` and `data/nseindia/recent_events.py` are
BORDERLINE (LLM-free, useful for FnO event-vol research later per
`docs/DATA_INVENTORY.md`) — the files stay but are deliberately **not** scheduled
(frozen, not deleted).

Plus two more jobs for the fundamentals screener, a deliberate, separate
carve-out from the pure-TA boundary above (long-term fundamental screening —
screener/watchlist/narrative/portfolio — not technicals/trading;
`docs/FUNDAMENTAL_SCREENER_PRD.md`):

8. `all_fundamentals_screener.sh` (19:15, weekdays) — runs
   `fundamentals.run_pipeline`: sector reference, L1/L2 refresh, event
   collectors, OCR + structured extraction, sector capital-cycle, L3 alerts
   (rule + LLM triage), descriptive technicals, and the watchlist/narrative/
   email pipeline, in dependency order. One step failing does not abort the
   run.
9. `all_fundamentals_api.sh` (every 5 min, no weekday restriction) — long-running
   FastAPI service (`fundamentals/api/app.py`) serving the `screener/` Nuxt
   frontend. Cron retries every 5 minutes; `with_lock.sh` no-ops while a real
   instance holds the lock, and the script's own `/api/health` check no-ops
   again if something is already serving.

go-crond itself has no supervisor and no `@reboot` support, and every check
above that would catch it dying (`cron_preflight.py`, `data_readiness.py`,
`data_coverage_report.py`) is itself a go-crond job — confirmed live
2026-08-19: go-crond died silently for 5 days with zero alerting until a
human noticed a missing email. `scripts/ensure_go_crond_alive.sh` is the
out-of-band fix: a cheap liveness check + auto-restart via `start_cron.sh`,
registered in the **OS-level user crontab** (`crontab -e`, every 15 min) —
deliberately NOT in go-crond's own generated crontab, since go-crond being
down is exactly the failure mode it exists to catch. This OS crontab entry
is host state, not tracked by git or `builder.py` — re-install it by hand
on a new machine (`(crontab -l; echo "*/15 * * * *
/path/to/stockey/scripts/ensure_go_crond_alive.sh >/dev/null 2>&1") |
crontab -`) and note it in `HANDOFF.md` alongside the existing "confirm
go-crond is running" caveat.

**A second, sharper gotcha confirmed live the same day**: `builder.py`'s
plain (no-flag) invocation always rewrites `config/stockey.generated.crontab`,
even when the content is byte-identical to what's already there. Running it
while go-crond is already live silently breaks the running instance's
scheduling (it keeps running as a process, but stops firing ANY job —
confirmed live: 2.5 hours, zero jobs fired, including the 5-minute
`fundamentals_api` health check) without crashing or logging anything —
go-crond gives no indication it stopped working. If you run plain
`builder.py` for any reason while go-crond might already be running, restart
go-crond afterward regardless of whether the diff was empty. **Use
`python builder.py --check-crontab` instead when you only want to know
whether a rewrite is needed** (re-audit, 2026-08-20) — read-only, does not
write `config/stockey.generated.crontab` or touch go-crond, exits 1 on
drift/a template error and 0 when already up to date; also rejects (raises,
does not write) a template with an unresolved `{{...}}` placeholder that has
no matching entry in `render_crontab()`'s replacements map.

## Key Commands

Daily/operator (matches the crontab exactly):

```sh
./complete_data.sh
./all_downloaders_queue.sh
./all_external_workers.sh
./all_ohlcv_reconcile.sh
./all_price_adjustment.sh
./all_data_readiness.sh
./all_fundamentals_screener.sh
./all_fundamentals_api.sh   # long-running -- serves the screener/ Nuxt frontend
```

Cron:

```sh
python builder.py
python scripts/cron_preflight.py
./start_cron.sh          # supported way to (re)start go-crond; runs OHLCV reconcile first
```

Dhan/Screener browser automation:

```sh
scripts/start_chrome_cdp.sh
```

Keep `CDP_ENDPOINT=http://localhost:9222`. Dhan auto-login should fail hard if
Chrome/CDP is unavailable; do not add a hidden manual-consent fallback unless
explicitly requested. Login attempts are serialized across processes
(`data/dhanlive/auth.py`'s `_dhan_login_lock`) — do not remove that lock, it
exists because concurrent logins triggered Dhan's "too many attempts" block.

## Files To Inspect First

- `docs/DATA_INVENTORY.md` — the authoritative collector/table inventory and
  cron list; check here before assuming a table or collector is in scope.
- `DATA_CONTRACT.md` (repo root; canonical copy in systrader) — the table API
  systrader depends on, the cloud-DB load rule, Dhan auth handoff, and the
  TimescaleDB-hypertable notes (several tables are hypertables — use
  `hypertable_size()` for capacity work, plain `pg_total_relation_size()`
  dramatically undercounts them; `pg_dump -t`/`\copy tablename` silently copy
  zero rows for a hypertable — the `\copy (SELECT * FROM ...)` form is
  required).
- `data/download_runner.py` for the downloader/parser registry (what actually
  runs and in what order).
- `data/data_readiness.py` for the freshness checks and bounded repairs.
- `docs/operators_manual.md` — day-to-day runbook: interpreter resolution,
  Chrome CDP setup, scheduled runs, main operating modes, DB/Redis robustness
  knobs, inspection commands.

## Coding Rules

- Use `rg` / `rg --files` for search.
- Preserve unrelated dirty worktree changes.
- Do not run destructive git commands unless explicitly asked.
- Add narrow tests for every behavioral change (`tests/test_data_platform.py`).
- Prefer bounded, focused fixes over large refactors.
- Keep new comments rare and useful.
- Default to ASCII in new files.
- When touching a collector, check `docs/DATA_INVENTORY.md` first to confirm
  the table it writes is actually in scope.

## Validation Checklist

Choose the smallest meaningful set for the change:

```sh
python -m py_compile path/to/module.py
pytest -q tests/test_data_platform.py::specific_test_name
python scripts/docs_state_audit.py --strict
python scripts/env_example_audit.py --strict
python scripts/price_data_sanity.py   # data-health: CA-splits/EQ-BE/cross-source/benchmark gaps (informational)
git diff --check
```

For cron/script changes:

```sh
python scripts/cron_preflight.py
python scripts/docs_state_audit.py --strict
```

## What Not To Do

- Do not hide Dhan/NSE source failures as empty outputs — use fallback telemetry
  and sync-state classifications (`auth_unavailable`, `source_unavailable`,
  `reference_mapping_missing`, `parse_failed`, ...).
- Do not assume latest rows are current; use trading-day-aware checks
  (`data/data_readiness.py`, `data/dhanlive/ohlcv_reconcile.py`).
- Do not add research/signal/scoring/backtesting/LLM logic here — it belongs in
  systrader.
- Do not point backtests, scans, or any read-heavy work at the cloud DB — it's
  small by design; heavy reads happen against systrader's local `systrade`
  mirror (see `DATA_CONTRACT.md`'s load rule).
- Do not add a static symbol/company registry file for a collector to fall
  back to — see Project Principles above.
- Do not widen a per-symbol scraper's scope casually without checking the
  cost — a slow per-symbol scraper run against a wide universe can mean
  thousands of daily calls.

## Boundary with systrader

Stockey is a pure DATA PLATFORM; all research/TA/selection authority lives in
`systrader` (Go, Carver framework). Read before any structural work:

- `DATA_CONTRACT.md` (repo root; canonical copy in systrader) — table API,
  load rule (cloud DB is small: never point heavy reads at it), auth, and the
  TimescaleDB notes.
- `docs/DATA_INVENTORY.md` — the full collector/table inventory and cron list.

Both projects can share one Claude Code session and one memory directory —
decisions affecting the other project still belong in these repo docs (the
inter-session API), not only in session memory, since memory doesn't survive
a fresh conversation the way a committed doc does.

Research boundary: ideas graduate from stockey's experiments to systrader by
**re-implementation as storied rules through `research/LEDGER.md`** — never by
copying code. Any research findings here that touch shared NSE data must be
exportable as trial counts (systrader's multiple-testing bar depends on
knowing every experiment the data has been asked).
