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

**Open work lives in `~/code/trading/TODO.md`** (workspace root, cross-repo).
Sections B and C of that file are stockey's: universe expansion, the confluence
score's dead axis, point-in-time snapshots, and the untested fundamental
families.

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
- `advisory_sync_state` is keyed by (source_name, **scope_key**), where scope_key is
  the step's `purpose`. Renaming a step's purpose orphans its old row at whatever
  status it last held, forever -- `data.nseindia.offmarket` moved `market_wide` ->
  `fundamentals_deal_flow` on 2026-08-14 and its stale row was the ONE "failing"
  collector on the Data Health page for three weeks while the module ran green daily.
  `get_collectors()` now demotes such a row to `orphaned`, but ONLY when the module
  also has a row under a current registry scope: 8 scope_key values are not registry
  purposes at all (written by the fundamentals/continuous_watch paths), and demoting
  on "scope not in registry" alone would HIDE a real failure from those.
- Dhan's `/charts/historical` (daily) `toDate` is **exclusive**; `/charts/intraday`
  is not. `DhanHistoricalClient.fetch_daily` compensates by sending `to_date + 1
  day`, so its own parameter is inclusive like every caller assumes. Do not
  "simplify" that away: without it the most recent session's bar is never fetched
  by the run that asks for it, `dhan_ohlcv_daily` sits permanently one trading day
  behind, and `data_completeness`'s `dhan_daily` check fails EVERY night for a
  chronic reason -- a gate that fails nightly is a gate people stop reading, which
  is how the August outage went unnoticed for five days. Confirmed live 2026-09-06
  on BSE: `toDate=09-04` returned sessions through 09-03; `toDate=09-05` returned
  09-04 with a close matching NSE's bhavcopy exactly.
- Do not let an ordinary collector mint a Dhan consent. Only
  `all_dhan_auth_ensure.sh` (auth_cli's `--auto-login` path, which calls
  `claim_consent_owner()`) may log in; everything else fails fast via
  `_require_consent_owner()` and records a fallback event. Each auto-login MINTS A
  CONSENT, and on 2026-09-04 five jobs each minting their own produced 22 attempts,
  `CONSENT_LIMIT_EXCEED`, and a total Dhan outage that then fed itself. Note the
  `_dhan_login_lock` does NOT protect against this -- it serialises callers so they
  do not fight over the browser; serialising N callers still mints N consents. The
  hard cap is `DHAN_MAX_CONSENTS_PER_DAY`, persisted on disk across processes and
  charged before the network call.
- Do not mutate broker/auth behavior (Dhan login, CDP) unless the user explicitly
  asks for that task — a botched change here can lock out the account (see
  `data/dhanlive/auth.py`'s login lock and timezone-aware expiry check).
- systrader owns all PRICE/TA-derived signals and the Carver execution stack.
  Fundamental scoring and the fundamental portfolio live here (2026-09-04
  revision). A task needing a price-derived rule, a vol-target, or futures
  belongs in systrader; a task reasoning over fundamentals belongs here.
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

The core pure-TA pipeline is 10 cron jobs (`config/stockey.crontab.template`).
Times below are the intended IST wall-clock schedule; go-crond
(webdevops/go-crond) has no `CRON_TZ`/`TZ` support, so the crontab file itself
is written in UTC (IST − 5:30) — see the per-job comments in the template for
each line's UTC/IST pair:

**Times below were re-derived from `config/stockey.generated.crontab` on 2026-09-06.**
The earlier list here was pre-retiming and wrong by hours on nearly every job. If you
change the schedule, re-derive rather than hand-editing — the template is the source of
truth and this list is a convenience copy that has drifted before.

Most data jobs run **twice**: an evening pass (best effort, as early as the data plausibly
exists) and a **morning catch-up (Mon–Sat)** that is the actual guarantee.

| IST | days | job |
|---|---|---|
| 06:50 | daily | `scripts/rotate_logs.sh` |
| 07:10 | Mon–Sat | `complete_data.sh` — catch-up `download_runner --phase all` |
| 07:30 | Mon–Sat | `all_data_readiness.sh` (`--fix`, bounded repairs) |
| 07:35 | Mon–Fri | `all_dhan_auth_ensure.sh` — **the only job allowed to mint a Dhan consent** |
| 07:40 | Mon–Sat | `all_ohlcv_reconcile.sh` |
| 07:45 | Mon–Sat | `all_price_adjustment.sh` |
| 08:00 | Mon–Sat | `all_data_completeness.sh` — read-only GATE, exits non-zero on a hard error |
| 08:10 | Mon–Sat | `all_data_coverage_report.sh` (monitoring) |
| 08:15 | Mon–Sat | `all_issue_digest.sh` (monitoring) |
| 08:30/12:30/16:30/20:30 | Mon–Fri | `all_downloaders_queue.sh` + `all_external_workers.sh` (+5 min) |
| 19:15 | Mon–Fri | `complete_data.sh` — evening EOD (NSE publishes ~18:00–19:00) |
| 20:00 | Mon–Fri | `all_price_adjustment.sh` — depends on bhavcopy, not on the Dhan reconcile |
| 21:00 | Mon–Fri | `all_fundamentals_screener.sh` |
| 22:00 | Mon–Fri | `all_portfolio_ruleset.sh` — exits → entries → forecast resolution → **action email** |
| 23:15 | Mon–Fri | `all_ohlcv_reconcile.sh` — Dhan publishes staggered through the evening |
| 23:40 | Mon–Fri | `all_dhan_intraday_sync.sh` |
| every 5 min | daily | `all_fundamentals_api.sh` (respawn-under-lock; no-ops when healthy) |

Ordering is load-bearing: the gate and both monitoring jobs sit AFTER the morning
catch-up so they judge final data. The morning chain runs **Mon–Sat** because a
Friday-evening miss otherwise had no catch-up until Monday — that is exactly what happened
on 2026-09-04, when NSE had not published when the 19:15 run asked and Friday's bhavcopy
then sat missing all weekend. The evening chain stays Mon–Fri: Saturday is not a trading
day, so there is no session to collect.

What each job does, and the reasoning behind the times, is in the per-job comments in
`config/stockey.crontab.template` — including why the EOD run moved off 17:30 (NSE had not
published) and why the morning catch-up exists at all.

`data/nseindia/earnings_events.py` and `data/nseindia/recent_events.py` are
BORDERLINE (LLM-free, useful for FnO event-vol research later per
`docs/DATA_INVENTORY.md`) — the files stay but are deliberately **not** scheduled
(frozen, not deleted).

Plus two more jobs for the fundamentals screener, a deliberate, separate
carve-out from the pure-TA boundary above (long-term fundamental screening —
screener/watchlist/narrative/portfolio — not technicals/trading;
`docs/FUNDAMENTAL_SCREENER_PRD.md`):

11. `all_fundamentals_screener.sh` (21:00 IST, weekdays) — runs
   `fundamentals.run_pipeline`: sector reference, L1/L2 refresh, event
   collectors, OCR + structured extraction, sector capital-cycle, L3 alerts
   (rule + LLM triage), descriptive technicals, and the watchlist/narrative/
   email pipeline, in dependency order. One step failing does not abort the
   run.
12. `all_fundamentals_api.sh` (every 5 min, no weekday restriction) — long-running
   FastAPI service (`fundamentals/api/app.py`) serving the `screener/` Nuxt
   frontend. Cron retries every 5 minutes; `with_lock.sh` no-ops while a real
   instance holds the lock, and the script's own `/api/health` check no-ops
   again if something is already serving. **It does NOT hot-reload**: because
   both guards no-op on a healthy instance, `./all_fundamentals_api.sh` after a
   code change silently leaves the OLD code serving (bit us 2026-08-31 adding
   `/api/data-health`). After changing anything under `fundamentals/api/`, kill
   the listener (`ss -tlnp | grep :8000`) and relaunch, or wait for cron.

   It also serves the data-platform health surface behind the screener's Data
   Health page (all read-only, all monitoring of stockey's own data rather than
   research, so inside the pure-TA boundary):
   - `GET /api/data-health` — the same `run_all_checks()` the nightly
     completeness gate uses, so page and gate can never disagree about what
     "complete" means. Cached 5 min (~20s cold against a 528M-row compressed
     hypertable); `?refresh=true` forces a recompute.
   - `GET /api/collectors` — per-collector status from `advisory_sync_state`,
     **reconciled against `download_runner.py`'s live registry**. That table is
     never pruned, so deleted/renamed modules keep an error row forever; without
     the reconcile the page shows ~23 permanently-red phantoms beside the real
     failures and teaches you to ignore it.
   - `GET /api/scheduler-health` — shells out to
     `scripts/is_cron_running.sh --json`. Deliberately NOT reimplemented in
     Python: the script is the source of truth and a second copy would drift.
   - `GET /api/platform-issues` — open identity issues + a fallback-telemetry
     rollup. Read-only on purpose: `issue_digest` reaches a similar report via
     `resolve_open_identity_issues(apply=True)`, which CLOSES rows, and a GET
     must never mutate pipeline state.
   - `GET /api/coverage-report` — nightly per-table coverage plus a staleness
     trend.

13. `all_portfolio_ruleset.sh` (22:00 IST, weekdays) — the fundamental portfolio
   (`docs/PORTFOLIO_RULESET_PRD.md`): mechanical entry ruleset → entry adjudicator
   (LLM, may only REJECT) and mechanical exit evaluator → exit adjudicator (LLM, may
   only DEFER, never a stop). **Exits run before entries** inside the script: a name
   can satisfy both on the same day, and entering first would leave a one-day round
   trip in the record that never happened. It reads that run's
   `fundamentals_confluence_score`, plus systrader's stage API for the price read --
   and the screener runs 65-109 minutes, so "an hour later" was NOT enough: on 5 of 6
   days before 2026-09-23 this job decided on half-refreshed data. The script now waits
   on the screener's cron lock (`scripts/wait_for_locks.sh`, 3h timeout -> no run), and
   the ruleset refuses a confluence score older than `PORTFOLIO_CONFLUENCE_MAX_AGE_DAYS`.
   Exits compare contradicting axes WITHIN one scorer version: a `SCORE_VERSION` bump
   re-baselines open positions instead of closing them. **Record-only** — the crontab deliberately does not
   pass `--live`, so no real money is committed (rollout phase 1).
   The script then runs `portfolio_resolution.py`, which grades every forecast whose
   target date has passed. That step is not optional: with the human register gone
   there is no other resolver, and if it stops running the scoring reports
   "0 resolved" forever without anything failing.
   Sizing is a flat Rs 1,00,000 per position, capped at 100 names (Rs 1 crore max
   deployed) -- a CAPITAL constraint, so a full book stops entering and names what it
   turned away rather than truncating the candidate list.

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
./all_dhan_intraday_sync.sh
./all_data_readiness.sh
./all_data_completeness.sh   # read-only gate; exits non-zero on a hard error
./all_data_coverage_report.sh
./all_issue_digest.sh
./all_fundamentals_screener.sh
./all_fundamentals_api.sh   # long-running -- serves the screener/ Nuxt frontend
./all_portfolio_ruleset.sh  # exits then entries; record-only unless --live is passed
```

Cron:

```sh
scripts/is_cron_running.sh   # "do I need to restart cron?" -- read-only, exits 1 if anything needs a human
python builder.py
python scripts/cron_preflight.py
./start_cron.sh          # start go-crond (refuses to double-start); runs OHLCV reconcile first
./stop_cron.sh           # stop go-crond; SIGTERM then SIGKILL after a grace period; no-op if not running
./restart_cron.sh        # stop then start, safely (waits for the real stop before starting)
```

`is_cron_running.sh` deliberately never reports healthy on the strength of a PID.
go-crond has twice been found alive while scheduling **nothing** (2026-08-19, dead
5 days; and after a `builder.py` rewrite under a live instance, 2.5 hours), so it
proves execution by checking that a job log was actually written inside two cycles
of the crontab's shortest interval. It also flags a go-crond whose parent is not
init (started from a shell, so it dies when that shell exits — this bit us
2026-08-31), locks held long enough that a job is being silently skipped, template
drift, a missing OS-crontab watchdog, and a down API/Chrome-CDP. Start it detached:
`setsid --fork ./start_cron.sh >> logs/cron/manual_start.log 2>&1`.

Dhan/Screener browser automation:

```sh
scripts/start_chrome_cdp.sh
```

Keep `CDP_ENDPOINT=http://localhost:9222`. Chrome is host state: it does not
survive a reboot and **nothing restarts it automatically** — that is deliberate,
the operator starts it by hand. Every Playwright consumer therefore routes through
`utils/cdp.py`'s `connect_over_cdp(playwright, CDP_ENDPOINT, caller=...)`, which
preflights the endpoint over plain HTTP with a 2s budget and, when Chrome is down,
prints a full-width banner naming the caller and exits **3** without touching the
database. Do not call `playwright.chromium.connect_over_cdp` directly: it does not
fail fast, it blocks for the full 30s Playwright timeout and then raises a
traceback naming `_browser_type.py` rather than Chrome — which is exactly what
buried the 2026-08-26..31 collection outage (`ohlcv_reconcile`,
`dhan_intraday_sync`, `data_readiness` and the EOD collectors all died on it,
silently, for five days). Dhan auto-login should fail hard if Chrome/CDP is
unavailable; do not add a hidden manual-consent fallback unless explicitly
requested. Login attempts are serialized across processes
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

## Working Gotchas

Mistakes that have cost real time in this repo, more than once each. Every one below was
made, diagnosed, and fixed live -- they are cheap to avoid and expensive to rediscover.

**SQL and the database**

- **`%` in any SQL string passed through `sql_to_db`/`psycopg2` must be `%%`** -- including
  inside a `LIKE`/`ILIKE` pattern AND inside a SQL comment. Otherwise psycopg2 tries to
  interpolate it and raises `IndexError: tuple index out of range`, which names nothing
  useful. Hit three separate times in one session (`ilike '%dhan%'`, `like
  'fundamentals_%'`, a `%` in a comment).
- **Never guess a column or table name -- read `information_schema` first.** Guessing cost
  four failed queries in one session: `dim_trading_days.is_trading_day` (does not exist),
  `ingestion_file_state.file_name`, `advisory_fallback_events.created_at` (it is
  `observed_at`), `advisory_sync_state.last_error_class` (it is `error_text`), and the
  table `dhan_instruments` (it is `master_dhan_instruments`).
- **`security_id` is `bigint`.** Passing a list of strings gives `operator does not exist:
  bigint = text`.
- **Every read of `advisory_adjusted_ohlcv_daily` or `dhan_ohlcv_intraday` needs a date
  bound.** Both are views/hypertables; an unbounded "latest per symbol" visited 689 chunks
  and took 2.4s for 100 symbols against 0.2s bounded, and the cost grows with history.

**Time**

- **Anchor on IST, never the server clock.** Between 18:30 UTC and midnight the two are
  different calendar days. This made two unrelated tests fail every evening and pass every
  morning (`fbil_gsec`, `bse_bhavcopy`), and it silently shifts any date-based rule. Use
  the repo's `_ist_today()`/`_ist_now()` helpers; a test asserting on dates must use the
  same clock the code does.
- **pandas will not compare tz-naive to tz-aware.** Postgres returns tz-aware timestamps,
  so a comparison against `pd.Timestamp("...")` raises. Normalise one side explicitly.

**Editing and searching code**

- **Delete code by AST span, not by splitting text.** Splitting `tests/test_data_platform.py`
  on `\ndef test_` to remove tests silently took a module-level fixture that followed one
  of them, breaking six unrelated tests. `ast.parse` + `node.lineno/end_lineno` is exact.
- **A regression guard that greps source will match its own explanatory comment.** This
  happened four times (`expect_download`, `flock`, `tr -d ' '`, the hypertable date bound).
  Strip comments first, or -- better -- walk the AST and inspect only the string literals
  or calls you actually mean. A guard that trips on correct code gets deleted rather than
  fixed, so it is worse than no guard.
- **A self-referencing DELETE on a compressed hypertable can silently delete nothing.**
  On `dhan_ohlcv_intraday` (TimescaleDB 2.21), `DELETE ... USING dhan_ohlcv_intraday` and
  `WHERE ts IN (SELECT ts FROM dhan_ohlcv_intraday ...)` both returned rowcount 0 while
  the equivalent SELECT matched 69,682 rows (2026-09-24). Select the keys first, then
  delete with a literal `= ANY(%s)` array, and always check `rowcount`.
- **`sql_to_df` retries a query after `pg_cancel_backend`.** Cancelling a runaway read
  server-side just restarts it under a new pid (2026-09-24). Kill the CLIENT process
  first, then cancel the backend -- and bound exploratory reads with
  `SET LOCAL statement_timeout` so there is nothing to cancel.
- **`pgrep -f` / `pkill -f` match the invoking command's own argv.** Use `pgrep -x`, an
  exact pattern, or kill by PID.

**Running things**

- **`$?` after a pipeline is the LAST command's status.** `cmd | tail -3; echo $?` reports
  `tail`'s success and will happily print `0` for a failed audit. Capture the status
  separately or use `PIPESTATUS`.
- **Do not pipe a long job's output through `tail`** when you may need to diagnose it: the
  detail is gone. Redirect to a file and tail the file.
- **`builder.py` (no flags) rewrites the crontab and silently stops a LIVE go-crond from
  scheduling anything.** Use `--check-crontab` to test for drift; if you do regenerate,
  restart go-crond afterwards regardless of whether the diff was empty.
- **Pipeline steps run in their own process** (`run_pipeline.run_step`, since 2026-09-24).
  They used to be re-imported in-process, which left their DEPENDENCIES as loaded at
  15:30 UTC: a mid-run signature change in `utils/company_master.py` failed l3_triggers,
  confluence_score and llm_triage on 2026-09-23 while every test passed. Each step now
  loads what is on disk when it starts -- so an edit mid-run applies from the NEXT step,
  and a half-finished edit can still break one. Prefer editing outside 15:30-17:30 UTC.
- **Unit tests cannot reach the live DB.** conftest refuses any real connection unless a
  test is marked `@pytest.mark.live_db`; stub `sql_to_df` / `db_session` / `upsert_to_db`
  at the module under test. Before this, 37 tests touched production (some ran real
  migrations; two passed only because of what production happened to hold).
- **The fundamentals API does not hot-reload.** After changing anything under
  `fundamentals/api/`, kill the listener (`ss -tlnp | grep :8000`) and relaunch.

**Judgement**

- **When a monitor is noisy, fix the classification -- do not widen a filter.** Two
  attempts this session to quiet monitoring nearly created blind spots: a scope-based
  demotion would have hidden genuine failures from 8 collectors whose `scope_key` is not a
  registry purpose. Narrow the rule to the exact signature of the noise, and add a test
  proving a real failure still surfaces.
- **Establish whether a persistent check failure is chronic or incidental BEFORE blaming
  the current incident.** `dhan_daily` was failing nightly for a chronic reason (an
  exclusive `toDate`) that was read for days as damage from an unrelated outage.
- **Verify claims about the environment.** The session metadata said "Is a git repository:
  false"; there is a `.git`, and it was the only reason lost test code was recoverable.

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
- Do not add TA/price-derived signal logic here — that belongs in systrader,
  which owns every price-based signal (2026-09-04 boundary revision, see
  "Boundary with systrader"). Fundamental scoring, portfolio rulesets and the
  LLM adjudication layer now DO live here, under
  `docs/PORTFOLIO_RULESET_PRD.md`; the collectors in `data/` remain pure data
  with no signal or LLM logic.
- Do not reintroduce a human forecast register. `fundamentals/screens/l4_thesis.py`
  and the `fundamentals_l4_thesis` table were DELETED 2026-09-04 on the operator's
  instruction ("remove the human forecast completely... only machine created
  portfolio and forecast"), along with the `POST /api/portfolio` create/resolve
  endpoints and their frontend forms. Restoring any of them would re-add a manual
  gate that nothing in the nightly run can satisfy, which freezes the portfolio
  silently rather than failing. Forecasts live on
  `fundamentals_portfolio_position` and are resolved by `portfolio_resolution.py`.
- Do not resolve a forecast against L2 state that does not POSTDATE it. L2 refreshes
  on the screener's cadence and is routinely older than the forecast; grading a
  prediction against the data that produced it is not merely circular, it marks
  change-predictions wrong before the company has reported (review finding,
  2026-09-04). `portfolio_resolution.l2_is_newer_than_forecast()` guards both paths.
  A newer run_date is still not enough: a 75-day re-crawl re-reads the SAME annual
  accounts. `reported_period_postdates_forecast()` requires the metric's reporting
  period (`balance_sheet_period` / `shareholding_period`, stored on L2 since
  2026-09-23) to END after the forecast, and it runs before BOTH paths -- if it sat
  only in the mechanical check, a None there would fall through to the LLM judge.
- Do not let a veto become permanent, and do not let it be re-asked on unchanged
  evidence. These are a MATCHED PAIR and a change to either must preserve both: a
  vetoed shadow never closes, so reading all open positions bans the name forever;
  but re-adjudicating nightly lets a sampled model eventually accept anything
  (observed: veto->accept in one hour on an identical scorecard). A veto stands
  until the EVIDENCE CONTENT moves -- `portfolio_runner.evidence_fingerprint` (scorer
  version, five axes, three counts, stage). It used to be "until the confluence
  `run_date` moves", which is true every night because confluence re-scores daily, so
  the guard did nothing: SHREEPUSHK was re-asked 14 times in 14 sessions (fixed
  2026-09-23). A later accept closes the vetoed shadow (`superseded_by_accept`) so one
  company never sits in both arms.
- The ONLY outbound email is `fundamentals/screens/portfolio_notify.py` (entries and
  exits), sent at the END of the portfolio chain. It cannot move into the 21:00
  screener pipeline: entry/exit decisions are not made until the 22:00 portfolio job,
  so mail sent from there reports YESTERDAY's actions while looking correct. The
  watchlist digest was retired 2026-09-07 (`send_daily_digest` is kept and tested but
  no longer called) -- do not re-add the call, or the operator gets two emails.
- The action email sends EVERY run, including "no action today". An email that only
  arrives when something happened makes a quiet day and a broken pipeline identical
  from the inbox -- the exact failure that hid two five-day outages here.
- **Verify email changes with `--dry-run`.** `./all_portfolio_ruleset.sh` sends for
  real to a four-person list; the module takes `--dry-run` for exactly this.
- Do not trust systrader's stage read without checking its `as_of`. The endpoint
  serves last week's stages indefinitely and without error if systrader's own cron
  dies, which silently turns a daily price read into a constant. Stale reads are
  discarded per row (`PORTFOLIO_STAGE_MAX_AGE_DAYS`) and the discard is logged.
- Do not read `advisory_adjusted_ohlcv_daily` without a date bound. It is a VIEW over
  a hypertable; "latest close per symbol" unbounded visited 689 chunks / 2.4s for 100
  symbols. Use `PORTFOLIO_PRICE_LOOKBACK_DAYS`.
- Do not blend the two resolution paths into one hit rate. A forecast with a
  structured metric resolves mechanically (data decides); one without is judged by
  a model. Reporting a single blended number lets a model that grades its own prose
  flatter the mechanical result -- the gap between the two IS the self-grading bias
  estimate, so every breakdown stays split by `resolution_method`.
- Do not key the accepted-vs-vetoed comparison off `kind`. `kind` means "is real
  money at risk"; `entry_decision` means "what did the adjudicator say". In
  record-only mode every row is `kind='shadow'` regardless of the verdict.
- Do not point backtests, scans, or any read-heavy work at the cloud DB — it's
  small by design; heavy reads happen against systrader's local `systrade`
  mirror (see `DATA_CONTRACT.md`'s load rule).
- Do not add a static symbol/company registry file for a collector to fall
  back to — see Project Principles above.
- Do not widen a per-symbol scraper's scope casually without checking the
  cost — a slow per-symbol scraper run against a wide universe can mean
  thousands of daily calls.

## Boundary with systrader

**REVISED 2026-09-04 by the operator.** The split is now by METHOD, not by
"data vs everything else":

- **`systrader`** — technical analysis. Price/volume-derived signals, Carver
  vol-targeting and sizing, futures, execution. Governed by
  `TRADING_BIBLE.md` and the `research/LEDGER.md` M-accounting.
- **`stockey`** — fundamental analysis, and the swing/long-term portfolio
  built from it. The data platform stays exactly as it is; the fundamentals
  carve-out grows to include portfolio construction from fundamental signals
  (see `docs/PORTFOLIO_RULESET_PRD.md`).

What this reversed: this file previously said "all research/TA/selection
authority lives in systrader" and "do not add research/signal/scoring/LLM
logic here". That was right while stockey was pure-TA-data-only. It is no
longer the operator's intent, and a future session that "helpfully" restores
the old rule would delete working code -- which is why this note is explicit
rather than a silent edit.

What did NOT change:
- The COLLECTORS stay pure data. No signal or LLM logic in `data/`.
- systrader still owns every price/TA-derived signal. stockey must not grow
  its own TA. It reads systrader's stage API for a price read; it does not
  recompute one.
- The pure-TA table inventory (`docs/DATA_INVENTORY.md`) is unchanged.

Read before any structural work:

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
