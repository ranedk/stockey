# High-Frequency Data Platform Plan

Written 2026-08-21. Answer open questions inline under each one (same
convention as `docs/open_questions.md`). This is the single planner for: (1)
moving Dhan market-data collection from stockey (Python) to systrader (Go),
(2) standing up a high-frequency (1-min, eventually tick) data platform for
strategy research, (3) the screener frontend serving both backends, and (4)
the backlog of pending items already accumulated in both repos.

## End state

- **systrader (Go)** owns all Dhan market-data collection: live websocket
  ticks + 1-min historical candles, for the full tradeable universe (not just
  a watchlist) — the raw material for strategy research.
- **stockey (Python)** narrows to EOD bhavcopy/reference/corporate-actions/
  calendar/RBI-FBIL/identity, plus the fundamentals screener carve-out. Dhan
  leaves its scope entirely once the cutover is proven.
- **Screener/Nuxt frontend** becomes the shared UI for both backends:
  stockey's fundamentals (FastAPI, existing) + systrader's technicals/signals
  (new Go API, doesn't exist yet).
- **Storage**: stockey's own data stays Postgres, unchanged. systrader's
  existing daily/weekly synced panel stays Postgres, unchanged (small,
  already works). systrader's NEW high-frequency data (vendor 10-year 1-min
  history + live-derived 1-min bars) lives primarily in DuckDB; the live
  landing zone specifically is an open question below.

## Guiding principles (non-negotiable, carried over from existing docs)

- The stockey/systrader boundary established earlier does not change: ALL
  signals/TA/execution stay in systrader; stockey stays pure-data + the
  fundamentals carve-out. Moving HF *collection* to Go is a velocity/latency
  split (batch EOD vs. streaming), not a reopening of that boundary.
- Law 16 (no discretionary override once live) applies to the live path same
  as everything else — websocket data feeds the forecast/sizing pipeline, it
  never becomes a side channel that bypasses it.
- Every new signal built on this data still goes through LEDGER + holdout
  discipline (Law 1, 2, 4) — more data does not relax that bar.
- stockey keeps running exactly as today throughout the transition. Nothing
  about its 9 scheduled jobs changes until systrader's replacement path is
  proven end-to-end for a meaningful period.
- Single active Dhan login owner at all times. Never run stockey's and
  systrader's login flows concurrently once systrader has its own — stockey's
  own CLAUDE.md already documents concurrent-login account-lockout risk.

## Already in place — don't rebuild these

- **`internal/broker/dhan`** (systrader) — REST client that reads stockey's
  token cache directly (`DHAN_TOKEN_CACHE` env var) and already has
  `FundLimit`/`Holdings`/`Positions`/`HistoricalDaily`/`LTP`/`PlaceOrder`
  (the last gated behind `LIVE_ORDERS=yes`, Law 16). This **is** the "Python
  owns login, Go consumes the token" bridge this plan needs — no new design
  work, only extension (websocket feed + intraday/1-min historical endpoint).
- **stockey's Dhan auth** (`data/dhanlive/auth.py`) — the consent-token
  exchange itself is plain REST (`generate_consent_app_id`/
  `consume_consent_token`), trivially portable to any language. The only
  piece needing browser automation is Dhan's hosted login *page*
  (mobile/PIN/TOTP entry) — done today via CDP + Playwright. Go's `chromedp`
  is a mature equivalent, so the eventual full migration (Phase 6) is
  feasible, not a research risk — just sequenced last, deliberately.
- **`dhan_ohlcv_intraday`** — 1-min bars stockey ALREADY collects today
  (630+ symbols, 22M+ rows and growing), sitting in the cloud DB, not yet
  synced to systrader (`DATA_CONTRACT.md`'s existing open item). Free
  head-start data, distinct from the new vendor 10-year history.

## Phases

### Phase 0 — Unblock the local systrade DB — DONE 2026-08-21

Provisioned, but as database/role **`systrader`** (matches the Go module
name), not **`systrade`** (the name every other doc/script in this workspace
uses) — cosmetic naming drift, `.env` updated to match what actually exists,
rename to `systrade` later when postgres superuser access is convenient. See
`.env`'s note.

`scripts/sync_from_stockey.sh` run successfully — but found and fixed a real
bug along the way: `src_has_table()` only checked `pg_tables`, which excludes
views. `advisory_adjusted_ohlcv_daily` (systrader's PRIMARY equity series)
became a plain VIEW on 2026-08-14's pure-TA cutover (verified live via
`pg_class.relkind` — genuinely a plain view, not materialized, so it's
recomputed on every query rather than stored) and had been silently skipped
by every sync since, with zero error. Fixed with `to_regclass`, which
resolves any relation kind. All tables now sync cleanly, including 5,846,408
rows of `advisory_adjusted_ohlcv_daily`.

### Phase 1 — Weinstein Stage Analysis — DONE 2026-08-21

`internal/stage` + `cmd/stage`, LEDGER row 10. Verified against real synced
data (`stage -tickers RELIANCE,TCS,INFY`) — all three read Stage 4
(Declining) as of week 2026-08-19, numbers internally consistent (close below
a falling 30-week MA, below-average volume).

### Phase 2 — Sync existing `dhan_ohlcv_intraday` into systrader

Cheapest possible next step and a good forcing function: extend
`sync_from_stockey.sh` (or a dedicated intraday sync — 22M+ rows and growing
daily doesn't fit the existing FULL_TABLES/INCR_TABLES pattern cleanly at
this volume, worth designing separately) to pull what stockey already
collects. This validates "systrader can consume and query HF data" using
real data, before any new collection infrastructure gets built, and is a
cheap real-world test of the Postgres-vs-DuckDB question below.

### Phase 2.5 — 5-year Dhan-sourced backfill, ahead of the vendor — IN PROGRESS 2026-08-22

Decision: rather than block thesis validation on the vendor's delivery
(unknown timeline), pull Dhan's own history directly — confirmed against
[Dhan's official docs](https://dhanhq.co/docs/v2/historical-data/) that 5
years of 1-min OHLCV is genuinely available (not just 90 days), 90 days max
per request (matches `INTRADAY_MAX_WINDOW_DAYS` in `data/dhanlive/ohlcv.py`,
already empirically tuned to it), [5 req/sec, 100,000/day
cap](https://dhan.freshdesk.com/support/solutions/articles/82000891163-what-are-the-api-limits-per-second-in-dhan-).
The vendor's data becomes a one-off normalize-into-our-format job later, not
a blocking dependency — decouples format/storage design from the vendor
entirely.

- **Universe**: full active NSE equity universe (`get_equity_universe()`,
  2,876 symbols as of 2026-08-22), not just the 635 already curated.
- **Storage**: stayed on Postgres/Timescale (no reason to introduce DuckDB
  given Phase 2's clean performance) — but enabled **native compression**
  (`compress_segmentby = 'exchange, security_id, interval_minutes'`,
  auto-compress chunks older than 7 days via `add_compression_policy`).
  Measured, not estimated: **15.7x** on the existing 22M-row dataset
  (7,515MB → 477MB). At that ratio the full 5-year/full-universe pull
  (~545M rows projected) fits in ~17GB — comfortably inside the ~205GB free,
  where the uncompressed 271GB projection would not have.
- **Script**: `stockey/scripts/backfill_intraday_5yr.py` (one-time, not a
  cron job) — resumable (skips a symbol whose earliest stored bar already
  reaches the target start date), batches multiple symbols per
  `upsert_to_db` call to amortize hypertable chunk-touch overhead.
- **Real timing, not estimated**: pure rate-limit math suggested ~3.4 hours
  for the full universe; actual per-symbol time is dominated by real API
  request latency (~1.3s/request empirically, not the 0.2s the rate-limit
  ceiling alone implies), so the real run is **~20-38 hours** depending on
  pacing safety margin. Running unattended in the background; not blocking
  other work.
- **Bug found and fixed same day**: `data/dhanlive/client.py`'s `_request`
  had zero pacing between calls and no retry on HTTP 429 (only
  401/expired-token was ever retried) — the backfill hit real rate-limit
  failures on symbols 4 and 5 of the full run. Fixed at the shared client
  level (in-process pacing ~4.5 req/sec, exponential backoff retry on 429,
  `DHAN_RATE_LIMIT_MAX_RETRIES=5`) so every Dhan consumer benefits, not just
  this one script — explicitly NOT a cross-process rate limiter like
  `utils/nse_rate_limiter.py` has for NSE; fine for today's single-process
  usage, would need that treatment if concurrent multi-process Dhan usage
  ever happens.
- **Daily keep-current job built**: `data/dhanlive/intraday_daily_sync.py` +
  `all_dhan_intraday_sync.sh`, scheduled 18:55 IST (right after
  `all_price_adjustment.sh`) in stockey's crontab — full active universe,
  incremental (each symbol's own last-stored-bar forward, not a fixed
  window), so a normal day's run is small, not a bulk re-pull. Chained to
  systrader's own daily sync via two new **OS-level crontab** entries (no
  go-crond equivalent in systrader) — see `HANDOFF.md`'s OS-crontab section
  for the exact schedule; that state does not travel via git clone.

### Phase 3 — Vendor 10-year 1-min OHLCV bulk load

Now secondary to Phase 2.5 above rather than blocking — becomes a one-off
job to normalize the vendor's data into whatever schema Phase 2.5 already
established, once it arrives. One-time (or periodic) ingestion of the
vendor's historical dataset.

> **OPEN QUESTION**: what format/delivery does the vendor use (CSV/Parquet
> dump, S3 bucket, direct API)? This determines the ingestion tool's shape —
> answer here before this phase gets scoped in detail.
> >

Target store: DuckDB (or Parquet files DuckDB queries directly) — this is
squarely DuckDB's use case: large, ~static once loaded, read-heavy,
analytical. Should also replicate stockey's adjustment pattern here: a VIEW
composing raw price + adjustment factor, never a separately maintained
adjusted series.

### Phase 4 — Live websocket ingestion + 1-min bar aggregation in Go

Extend `internal/broker/dhan` with a websocket client and aggregate ticks
into 1-min bars in-process.

**RESOLVED 2026-08-22** (was an open question): checked [Dhan's official
websocket docs](https://dhanhq.co/docs/v2/live-market-feed/) directly.
5,000 instruments per connection, up to 5 connections per user (25,000
total) — the full 2,876-symbol universe fits in ONE connection with huge
headroom, no multi-connection sharding needed.

Protocol summary for implementation:
- Endpoint: `wss://api-feed.dhan.co?version=2&token=<access_token>&clientId=<id>&authType=2`
  — auth via query params, no separate handshake message. Reuses the same
  token cache `internal/broker/dhan` already reads from stockey.
- Subscribe: JSON `{"RequestCode": 15, "InstrumentCount": N, "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "..."}]}`
  — max 100 instruments per message (so ~29 messages to subscribe the full
  universe), up to 5000 total per connection.
- Responses are **binary, little-endian**, an 8-byte header (response code,
  payload length, exchange segment, security ID) on every packet. Relevant
  packet types: Ticker (code 2: LTP + trade time — minimal for 1-min bar
  construction) and Quote (code 4: LTP + cumulative day volume + day OHLC —
  needed to derive per-tick volume deltas via consecutive-packet diffing,
  since Dhan reports cumulative not incremental volume). **Decide Ticker vs.
  Quote before implementing** — Quote gives volume for free but is a bigger
  packet; Ticker is minimal but needs a separate volume source if per-bar
  volume matters for the thesis.
- Server pings every 10s; must respond (most Go websocket libraries handle
  pong automatically) or the server closes the connection after 40s of
  silence. Disconnect packet is response code 50.

> **OPEN QUESTION / DECISION NEEDED**: where do live-aggregated 1-min bars
> land?
> - **(a) Postgres/Timescale (recommended starting point)** — zero new
>   infrastructure, systrader already depends on `pgx`, proven under
>   concurrent write+read (stockey already runs this pattern successfully).
>   DuckDB can attach read-only later via its postgres scanner, or via
>   periodic Parquet export, once there's something worth querying fast.
> - **(b) DuckDB directly (Parquet part-files, periodically compacted)** —
>   one fewer storage engine overall, but DuckDB's single-writer model needs
>   a real compaction design (continuous appends during market hours,
>   compact/attach after close or on an interval) since concurrent
>   write+read against one `.duckdb` file isn't its strong suit. Also: this
>   repo has zero cgo dependencies today (pure-Go `pgx`/`godotenv`); DuckDB's
>   Go driver is cgo-based — first cgo dependency, real build/cross-compile
>   cost, worth going in with eyes open.
>
> Recommendation: start with (a). It de-risks the live trading path (which
> matters more here than query speed) and costs nothing new to build. Move to
> (b) later once Phase 3 has given the team real operating experience with
> DuckDB on the bulk-historical tier.
> >

Regardless of (a)/(b): the live trading decision loop reads from in-process
Go state updated directly by the ingestion goroutine, never round-trips
through whichever DB is chosen for the hot path — that's a durability/
research concern, not a decision-latency one.

### Phase 5 — Fundamentals summary sync + systrader HTTP API (frontend-facing)

Supersedes the earlier "blended digest" idea from this conversation: instead
of a nightly digest email, expose a systrader HTTP API (new — `cmd/run` is
currently an offline batch tool, not a server) that the Nuxt frontend calls
directly for stage/signal data, alongside stockey's existing FastAPI for
fundamentals. Needs the fundamentals-summary sync (a narrow, structured
summary table — thesis stage / L3-L4 verdict / watchlist status — not raw
narrative text) only if blending happens server-side; the frontend can also
call both APIs and blend client-side, which is simpler and worth trying
first.

### Phase 6 — Full Dhan ownership transfer to Go (LAST, gated)

Reimplement the CDP login flow in Go (`chromedp`) once Phase 4 has run
stable for a meaningful period.

> **OPEN QUESTION**: how long is "stable enough" before cutting over login
> ownership? The bible's paper-trade convention is 3 months for a new
> trading rule; login-flow risk is different in kind (operational, not
> statistical) so a shorter bar may be right — suggest deciding this
> explicitly rather than defaulting either way.
> >

Cut over atomically: stockey's Dhan collectors get disabled the same day
Go's login goes live, never dual-active (account-lockout risk). Update
stockey's `CLAUDE.md` to reflect Dhan leaving its scope once this actually
happens — not before.

## Consolidated backlog (pending items already accumulated, both repos)

**stockey:**
- `nseindia_mcap` pre-2024 history gap — backfill decision not made.
- `dim_trading_days` has no producer in the current codebase — populated
  only through 2026-12-31; find/rebuild the producer before then.
- BSE-only adjustment is weaker than NSE (price-step-derived only, no
  corroborating corporate-actions feed).
- Pure-TA migration Phase 5 (dropping `advisory_*` tables from the cloud DB)
  deferred, not done.
- `dim_security` freshness not recently rechecked.

**systrader:**
- `docs/rule_ideas.md` Tier 1 candidates untouched since EWMAC/carry shipped
  — breakout (normalized Donchian) is explicitly flagged as "first rule to
  add" and hasn't been.
- TimesFM Python-sidecar integration designed (`docs/rule_ideas.md`) but not
  built.
- `docs/open_questions.md` items 8, 9, 11, 13–20 unanswered (execution mode,
  roll policy, instrument exclusions, tax entity, ops time budget,
  paper-trade period length, repo privacy, library approvals) — several of
  these (execution mode, paper-trade period) directly gate Phase 6 above and
  are worth answering alongside this plan rather than deferring further.

**systrader — code-review findings (2026-08-21):**
- ~~`internal/backtest/engine.go:209` — `ExecuteAtOpen` silently falls back to
  same-day close when an instrument's `Opens` series is missing/NaN,
  reintroducing the look-ahead bias the flag exists to prevent.~~ **FIXED
  2026-08-22**: `Run()` now fails fast if any instrument lacks `Opens` when
  `ExecuteAtOpen` is set; a remaining per-day gap inside an existing series
  is counted via `InstrumentResult.DegradedOpenFills`, not silent.
  `cmd/backtest` updated to drop (and print) instruments that fell back past
  adjusted-OHLC instead of passing them through with nil `Opens`.
- ~~`internal/backtest/metrics.go:42` — after a compounding bust floors capital
  to 0, subsequent daily returns silently compute as 0% instead of invalid,
  dragging Sharpe/vol/skew toward "flat and safe" instead of showing the
  blow-up.~~ **FIXED 2026-08-22**: `ComputeMetrics` now detects the bust,
  stops computing return-day statistics there, and `Metrics.Busted`/
  `BustedDay` make `Report()` print a loud warning instead of a misleadingly
  clean number. `MaxDDPct` still uses the full equity series (correctly
  shows the bust as a real drawdown).
- `internal/backtest/engine.go:362` (still open) — `res.EndCapital` (raw summed P&L) and
  `res.Equity` (floored at 0 on bust) diverge after a compounding bust; any
  report printing both shows numbers that don't reconcile.
- `internal/research/research.go:28` — `Split.InTrain`/`InHoldout` are both
  inclusive at their shared boundary date, so `HoldoutStart == TrainEnd` (a
  natural config) puts one date on both sides of the train/holdout firewall.
- `internal/backtest/engine.go:356` — per-instrument turnover annualizes
  using the full portfolio calendar length, not the instrument's own trading
  span — understates turnover for any instrument with shorter history than
  the backtest window.
- `internal/rules/rules.go:36` — `Validate` checks story length only, never
  exercises `Scalar()` — a bad `EWMAC.Fast` value passes validation and
  panics later, mid-run, instead of failing at config time.
- Lower-confidence/efficiency, cut from the original report for the cap but
  worth a look: `engine.go:276` silent-zero on an unmatched
  `InstrumentWeights` key; `internal/broker/dhan/dhan.go:110` swallowed
  `io.ReadAll` error; `scripts/sync_from_stockey.sh`'s reconciliation
  fingerprint possibly degrading to count-only for the now-view
  `advisory_adjusted_ohlcv_daily` (worth confirming against the live source
  schema); N+1 sequential DB loads in `cmd/backtest/main.go` and row-by-row
  upsert in `internal/store/backfill.go`.
- Also flagged, not yet audited: a `Law 20` reference in
  `internal/portfolio/portfolio.go:39` that doesn't exist in
  `TRADING_BIBLE.md` (17 laws total) — same ghost-reference class as the
  "Law 19" issue already found in the workspace `CLAUDE.md`/`HANDOFF.md`.
  Worth a full audit of every `Law N` comment against the actual bible file.
- `Metrics.Report()` doesn't print two things Engineering Corollary 4
  mandates: annual turnover (computed but only printed separately by
  `cmd/backtest`, not part of the standard report) and cost-doubled SR
  sensitivity, which isn't computed anywhere in the codebase at all.

## Explicit non-goals

- Does not change the fundamental/technical (stockey/systrader) boundary —
  already settled this conversation.
- Does not implement any new trading rule — Stage Analysis and anything
  downstream of this data still needs its own LEDGER-gated admission.
- Does not move stockey's own storage off Postgres.
