# Trading Workspace

One git repo (`git@github.com:ranedk/stockey.git`, since 2026-09-26) holding three
projects, one purpose, one Claude session (launched from here). Until 2026-09-26 these
were separate repos (stockey: alphabuy/stockey, systrade: atman-care/systrade, screener:
no remote); their full histories were merged in under their folders, and the old `.git`
dirs are backed up at `~/code/trading-git-backup-2026-09-26/`. Commit from anywhere in
the tree; there are no nested repos any more.


- **`stockey/`** — the data platform (Python). Pure-TA scope: collects
  bhavcopy, corporate actions/dividends, indices, calendar, Dhan
  instruments/prices/auth, RBI/FBIL rates, identity, historical mcap; parses
  into the cloud postgres. NO research, NO fundamental analysis (except the
  deliberate `fundamentals/` carve-out, see `stockey/docs/FUNDAMENTAL_
  SCREENER_PRD.md`), NO news/announcement pipelines outside that carve-out,
  NO LLM-token consumption outside it. Authoritative scope:
  `stockey/docs/DATA_INVENTORY.md`.
- **`systrader/`** (checked out on this machine as `~/code/trading/systrade`
  — no trailing "r"; check `ls ~/code/trading/` if unsure which name a given
  machine uses) — the research & trading system (Go, + Python sidecar for
  TimesFM/ML later). Sole home of ALL signals, TA, backtesting, sizing,
  execution. Governed by `systrader/TRADING_BIBLE.md` (law; the
  `trading-bible` skill is its distillation) and the research discipline in
  `systrader/internal/research` (LEDGER audit log — M is a record, not a bar since 2026-09-15 — holdout burns, story
  rule). **`systrader/docs/RESEARCH_PROTOCOL.md` is also law** (adopted by the
  operator 2026-09-08): it amends bible Laws 2 and 4 — FDR within the searched
  family instead of a workspace-wide Bonferroni, purged walk-forward instead of
  one-shot holdouts, slicing on named attributes permitted, and forward paper
  trading as the gate before capital moves.

Boundary contract: `systrader/docs/DATA_CONTRACT.md` (canonical copy;
byte-identical mirror at `stockey/DATA_CONTRACT.md` — keep both in sync on
any edit). Data flows stockey → cloud DB → daily sync → local `systrade`
postgres; heavy reads hit ONLY the local DB. systrader never writes
stockey-owned tables; its outputs live in `systrader_*` tables.

## The open-work index

**`TODO.md` at this workspace root is the index of open work across all three
repos** (written 2026-09-23). Pick items from there; if something is open only
in a repo's README or PRD and not in `TODO.md`, add it. There is also a third
repo beside the two above — **`screener/`** (Nuxt) — the frontend both publish
through: stockey's watchlist and stages pages, systrader's paper page.

## Authority rule for this session

- In `stockey/`: data operations, **and** fundamental analysis + the
  swing/long-term portfolio built from it (boundary revised by the operator
  2026-09-04 — the split is now by METHOD, not "data vs everything else").
  Keep collectors, parsers, adjustment, and sync healthy; the collectors in
  `data/` stay pure data with no signal or LLM logic. The portfolio is fully
  machine-run: ruleset → LLM adjudicator (may only reject) → machine forecast
  resolution. The human forecast register was DELETED 2026-09-04; do not
  restore it. See `stockey/docs/PORTFOLIO_RULESET_PRD.md`.
- In `systrader/`: design code and experiments under the bible. NEVER
  override a forecast, position, or frozen parameter by discretion
  (Law 19) — including during drawdowns. LLM judgment shapes experiments
  and code, never live trading decisions.
- Every experiment on market data gets a `research/LEDGER.md` row (batch
  rows: `trials=N`; exploration rows: `trials=0` and no significance claim).
  Holdouts burn once (`research/holdout_burns.json`); the ensemble-family
  holdout 2020-01→2021-07 is already burned.

## Operational notes

- Local DB `systrade` (postgres, localhost) is the research workhorse;
  sync via `systrader/scripts/sync_from_stockey.sh`. Cloud DB is small —
  never point backtests or scans at it.
- Dhan token: stockey owns login. `web_login` is the INNER step and requires
  `--consent-url`; the operator command is
  `python -m data.dhanlive.auth_cli refresh --clear-cache-first --auto-login`
  (scheduled as `all_dhan_auth_ensure.sh`, weekdays 02:05 UTC = 07:35 IST). Login is fully automatic
  — TOTP via `pyotp`/`DHAN_TOTP_SECRET` — so an OTP-looking timeout in the logs
  does NOT mean a human must type a code. Cache at
  `stockey/.cache/dhan_access_token.json`; check expiry with `auth_cli status`
  rather than assuming a fixed hour.
- **Only `all_dhan_auth_ensure.sh` may mint a Dhan consent.** Each auto-login
  creates one, and on 2026-09-04 five jobs each minting their own produced 22
  attempts, `CONSENT_LIMIT_EXCEED`, and a total Dhan outage that then fed
  itself. Other callers fail fast by design. The `_dhan_login_lock` does NOT
  protect against this — serialising N callers still mints N consents.
- `trading-bible` skill loads for any rule/backtest/sizing work.
- 1-min tick data lands in `dhan_ohlcv_intraday`. systrader reads it live
  through a `postgres_fdw` foreign table — both Postgres instances are
  colocated on one disk, so there is no local mirror and no sync script
  (`systrader/scripts/sync_intraday_from_stockey.sh` is deprecated, kept only
  as a reference if the machines are ever truly split).
- Never scan `dhan_ohlcv_intraday` or `advisory_adjusted_ohlcv_daily` without a
  date bound — both are hypertable-backed, an unbounded aggregate forces full
  decompression, and that is what caused this workspace's OOM incident.

## Before writing code here

Read `stockey/CLAUDE.md`'s **Working Gotchas** section. It lists the mistakes that
have actually cost time in this workspace — `%` escaping in psycopg2, guessing column
names instead of reading `information_schema`, IST vs server clock, deleting code by
text-split instead of AST span, regression guards matching their own comments, `$?`
after a pipeline. They are cheap to avoid and expensive to rediscover.
