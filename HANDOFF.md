# Workspace Handoff

Written 2026-08-07 ahead of moving this workspace to a new machine/environment
where the owner will not do hands-on development — Claude is expected to
operate the data platform and shape systrader's research with less direct
supervision. This file is the "read this first, before anything else" doc for
a fresh Claude session on the new side. It is a **plain file, not in git**
(same as the sibling `CLAUDE.md` in this directory) — copy both by hand.

## What this workspace is

Two sibling git repos, one purpose: `CLAUDE.md` in this directory has the full
authority/boundary rules — read it first, always. Short version:

- **`stockey/`** — pure data platform (Python). Collects, adjusts, and
  identity-maps NSE bhavcopy/corporate-actions/indices/calendar, Dhan broker
  data, RBI/FBIL rates. Writes to the cloud Postgres. No research, no signals,
  no LLM-token consumption in its collectors.
- **`systrader/`** — the research & trading system (Go, Carver framework).
  Sole home of ALL signals, TA, backtesting, sizing, execution. Governed by
  `systrader/TRADING_BIBLE.md` (law — Law 1 story rule, Law 19 no discretionary
  override of live decisions).
- **Boundary contract**: `systrader/docs/DATA_CONTRACT.md` (canonical; copy at
  `stockey/DATA_CONTRACT.md`). stockey → cloud DB → daily sync →
  local `systrade` postgres; heavy reads/backtests hit ONLY the local mirror,
  never the small cloud DB.

## Repo state as of this handoff (both clean, both fully pushed)

- `stockey` — `git@github.com:alphabuy/stockey.git`, `main` @ `3205bca`+
  (pure-TA migration Phases 0-4 complete; docs rewritten to match; TimesFM
  code removed; `.env.example` audited to 0 missing/0 unused).
- `systrader` — `git@github.com:atman-care/systrade.git`, `main` @ `7396478`
  (engine metrics-inflation bug fixed, decide-at-close/fill-at-open, Law-1
  story enforcement, `internal/research` LEDGER/holdout-burn scaffold —
  this was 4 commits + uncommitted WIP until just now; it's pushed and clean).

Both repos are a plain `git clone` away from full recovery. Nothing in either
working tree is uncommitted as of this handoff.

## What does NOT travel via git clone

1. **This directory's plain files** — `CLAUDE.md` and this `HANDOFF.md`.
   Neither `stockey/` nor `systrader/` nor `/Users/rane/code/trading/` itself
   (the parent) is a single repo containing them; copy by hand (scp/rsync/
   whatever moves the rest of the machine).
2. **`dumps/systrade_20260727/`** (520MB local Postgres dump of the research
   DB). Regenerable on the new side via `systrader/scripts/sync_from_stockey.sh`
   — don't bother moving it unless you want continuity before the first sync
   completes.
3. **Claude's memory** — see below, this is the one that needs care.
4. **The task list from any given session** — not meant to persist; the
   durable record of "what's done" lives in the committed docs
   (`docs/DATA_INVENTORY.md`, `docs/PURE_TA_MIGRATION_PLAN.md`,
   `docs/DATA_COVERAGE.md`, `research/LEDGER.md`), not in tasks or memory.

## Moving Claude's memory

Memory lives at `~/.claude/projects/<encoded-cwd>/memory/`, where
`<encoded-cwd>` is the absolute working directory path with every `/` replaced
by `-`. On this machine that's:

```
/Users/rane/code/trading  →  -Users-rane-code-trading
```

Claude Code auto-discovers memory by matching the *current* working
directory's encoding against that folder name — it is **not** portable by
default across a different username, mount path, or directory layout.

To move it:

```sh
# On the OLD machine
tar -czf stockey-memory.tar.gz -C ~/.claude/projects/-Users-rane-code-trading memory

# Figure out the NEW absolute working directory (call it $NEW_PATH), then
# compute its encoded form the same way:
NEW_ENCODED=$(echo "$NEW_PATH" | sed 's/\//-/g')
mkdir -p ~/.claude/projects/"$NEW_ENCODED"

# On the NEW machine, after copying the tarball over:
tar -xzf stockey-memory.tar.gz -C ~/.claude/projects/"$NEW_ENCODED"
```

Verify by checking `~/.claude/projects/<NEW_ENCODED>/memory/MEMORY.md` exists
and looks right, then start a session with cwd exactly `$NEW_PATH`.

If the new working directory path is genuinely different from
`/Users/rane/code/trading` (different user, different mount), the safest bet
is to keep the two-repo layout name (`trading/{stockey,systrader}`) even if
the parent path differs — nothing inside either repo hardcodes the absolute
path, only Claude's memory-folder-name lookup cares.

## Where a fresh session should start reading, in order

1. `CLAUDE.md` (this directory) — authority rules, which repo owns what.
2. `stockey/CLAUDE.md` — pure-TA operating guide, current 7-job cron pipeline.
3. `stockey/docs/DATA_INVENTORY.md` — authoritative keep/remove table inventory.
4. `systrader/TRADING_BIBLE.md` — the laws; the `trading-bible` Claude skill is
   its distillation and should load automatically for rule/backtest/sizing work.
5. `systrader/docs/DATA_CONTRACT.md` — the table API + load rule between the
   two repos (cloud DB is small, never point backtests at it).
6. `systrader/research/LEDGER.md` + `systrader/research/holdout_burns.json` —
   every experiment run against market data and which holdouts are burned.
   The ensemble-family holdout (2020-01 to 2021-07) is burned; don't re-verify
   inside that window.
7. `systrader/docs/open_questions.md` — answered operator questions (capital
   30L, vol target 20%/40%-drawdown-discomfort, Dhan F&O approved, confirmed
   Dhan fee schedule) that size/gate everything downstream.

## Operational notes that matter day one

- **Cron**: stockey's pipeline starts via `./start_cron.sh` (see
  `stockey/README.md`) — 7 jobs, matches `config/stockey.crontab.template`.
  Confirm `go-crond` is actually running on the new machine before assuming
  data is flowing; nothing auto-starts it on boot. This bit for real
  2026-08-14 to 2026-08-19: go-crond died with zero alerting for 5 days
  (every check that would have caught it is itself a go-crond job). Fixed
  with `stockey/scripts/ensure_go_crond_alive.sh`, a watchdog that
  auto-restarts go-crond if it's found dead — but it only works if it's
  actually installed in the **OS-level user crontab** on this machine
  (`crontab -e`, not go-crond's own crontab), which is host state and does
  NOT travel via git clone. Re-install it on the new machine:
  `(crontab -l; echo "*/15 * * * *
  /path/to/stockey/scripts/ensure_go_crond_alive.sh >/dev/null 2>&1") |
  crontab -`, then `crontab -l` to confirm.
- **Chrome CDP is host state and nothing restarts it.** It does not survive a
  reboot; the operator starts it by hand
  (`stockey/scripts/start_chrome_cdp.sh`, then check
  `curl -s http://localhost:9222/json/version`). Confirmed the hard way
  2026-08-26..31: Chrome went down and `connect_over_cdp` does NOT fail fast —
  it blocks the full 30s Playwright timeout and raises a traceback naming
  `_browser_type.py`, not Chrome. That killed `ohlcv_reconcile`,
  `dhan_intraday_sync`, `data_readiness` and the EOD collectors for five days
  with every cron job still exiting 0. Dhan daily collection decayed
  1350 -> 375 -> 373 -> 3 tickers/day and intraday collapsed from ~2,590
  tickers/day to 1. Since 2026-08-31 every Playwright consumer goes through
  `stockey/utils/cdp.py`, which preflights the endpoint in 2s and exits 3 on a
  loud banner instead.
- **Freshness is not completeness.** The reason nobody noticed for five days:
  every check asked "how recent is the newest row?", and the Dhan connectivity
  smoke test (`download_runner._dhan_precheck_symbols`) kept writing ONE ticker
  a day, so `max(timestamp)` said "today" while the universe was dead.
  `stockey/scripts/data_completeness.py` (new, 22:50 IST) checks breadth against
  the recent median and exits non-zero; it deliberately imports nothing
  browser- or broker-related, because `data_readiness` — the job that should
  have shouted — was crashing on the very same CDP timeout as its subjects.
- **Never query `dhan_ohlcv_intraday` unbounded.** ~525M rows across 263
  COMPRESSED chunks: any whole-table aggregate (`count(*)`,
  `select distinct ticker`, `min(timestamp) group by security_id`) makes
  TimescaleDB decompress every chunk at once and took this 30 GB box to OOM in
  ~15 seconds on 2026-08-31, killing postgres with it. Postgres does NOT restart
  itself after an OOM kill (`sudo systemctl reset-failed postgresql@16-main &&
  sudo systemctl start postgresql@16-main`). Bound every scan by time, or read
  catalog metadata (`approximate_row_count()`, `timescaledb_information.chunks`).
  `stockey/scripts/incident_audit.sh` is safe by default; its whole-history
  questions live behind `--deep`, which walks one chunk at a time.
- **Dhan auth**: stockey owns login; token cache at
  `stockey/.cache/dhan_access_token.json`, expires daily ~17:20-23:00 IST
  (varies). Refresh with `python -m data.dhanlive.auth_cli ensure
  --auto-login` (2026-08-22 correction — `python -m data.dhanlive.web_login`
  is NOT a standalone entrypoint despite older docs saying so; it requires
  `--consent-url` and is only ever invoked as a subprocess by the real auth
  flow). Needs a Chrome CDP session (`scripts/start_chrome_cdp.sh`) for
  auto-login on a machine that doesn't have one running yet.
- **Local DB `systrade`**: the research workhorse. Sync via
  `systrader/scripts/sync_from_stockey.sh` — run this early on the new
  machine so `internal/research`/backtests have data to read. On THIS
  machine it was provisioned as database/role `systrader` (not `systrade` —
  cosmetic naming drift, see `systrader/.env`'s own note; rename when
  convenient).
- **Cloud DB**: small by design. Never point backtests or heavy reads at it.
- **1-min tick data**: was in `dhan_ohlcv_intraday`, cloud DB — see
  `stockey/docs/DATA_CONTRACT.md`. Decision made 2026-08-21: systrader is
  taking over ALL Dhan HF collection (websocket + 1-min) long-term, plus a
  vendor 10-year 1-min history and a new DuckDB layer for it; full phased
  plan at `systrader/docs/HF_DATA_PLATFORM_PLAN.md`. 2026-08-22: also now
  backfilling 5 years of 1-min history directly from Dhan (their API
  genuinely serves 5 years, confirmed against official docs) for the full
  active NSE universe, ahead of the vendor delivery — see the plan doc.
- **OS-level user crontab** (`crontab -l`; host state, does NOT travel via
  git clone — re-install by hand on a new machine, `crontab -e`):
  - `stockey/scripts/ensure_go_crond_alive.sh` every 15 min (see above).
  - `systrade/scripts/sync_from_stockey.sh` daily 15:00 UTC (20:30 IST, after stockey's price_adjustment; was 14:15 UTC until 2026-09-12),
    weekdays.
  - `systrade/scripts/sync_intraday_from_stockey.sh` daily 15:00 UTC
    (20:30 IST), weekdays.
  Both systrader entries added 2026-08-22 — systrader has no go-crond of its
  own, OS crontab is its only scheduling mechanism.

## Known open items (not urgent, but don't assume they're resolved)

- Phase 5 of the pure-TA migration (dropping `advisory_*` tables from the
  cloud DB) is deferred, not done — see `stockey/docs/PURE_TA_MIGRATION_PLAN.md`.
- `nseindia_mcap`/`52wk` history backfill decision — not made.
- `dim_security` freshness — not recently re-checked.
- `nseindia_earnings_events`/`recent_events` collectors are frozen (not
  deleted) pending a possible future FnO event-vol use — see
  `stockey/docs/DATA_INVENTORY.md`'s BORDERLINE section.

## If the owner truly won't be reviewing regularly

Re-read `CLAUDE.md`'s Authority rule section before assuming more autonomy
than it grants — Law 19 (no discretionary override of forecasts/positions,
including during drawdowns) is not relaxed by the owner being less involved;
if anything it matters more. LLM judgment shapes experiments and code, never
live trading decisions, regardless of how this workspace gets migrated.
