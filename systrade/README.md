# systrader

A systematic trading framework for Indian markets in Go, built strictly on the
architecture of Robert Carver's *Systematic Trading*: continuous
volatility-standardized forecasts → handcrafted combination → volatility
targeting → position sizing → portfolio assembly → costs & speed limits.

**Read `TRADING_BIBLE.md` before touching anything.** It is law here; the
condensed version is installed as the `trading-bible` Claude skill and loads in
every session.

## Layout

```
TRADING_BIBLE.md          The laws (Carver distilled) — non-negotiable
docs/open_questions.md    Questions you must answer; edit inline
docs/instruments_india.md Indian instrument analysis & two-sleeve design
docs/rule_ideas.md        Phase-2 rules (breakout, TimesFM…) + innovations
research/LEDGER.md        Append-only record of every experiment (M-counter)
docs/strategies/          Frozen specs for strategies awaiting paper data
docs/RESEARCH_PROTOCOL.md Two-track process (explore freely, confirm
                          elsewhere) — ADOPTED 2026-09-08, amends Laws 2 and 4
research/preregistrations Design, periods, controls and decision rule of a
                          trial, written BEFORE it runs
research/reports/         Raw output of the runs the ledger rows summarize

internal/core             Series math: EWMA, EWMA-std, price-unit volatility
internal/data             Instrument metadata, cost model, CSV loading
internal/rules            Rule interface + EWMAC + carry (Carver's published
                          scalars) + breakout, acceleration, mean reversion
                          (scalars measured here) — all scaled, capped ±20
internal/combine          Forecast weights + FDM (≤2.5) → combined forecast
internal/sizing           Vol targeting: cash vol target, vol scalar, subsystem
internal/portfolio        Instrument weights, IDM (≤2.5), rounding, inertia,
                          four-block test
internal/backtest         Daily engine (compounding, costs), metrics
                          (SR/skew/DD/turnover/cost-drag), Bonferroni stats
internal/parallel         Generic worker-pool Map
internal/sleeve           Long-only cross-sectional equity backtest + its
                          matched controls (beta, shuffle, stable-shuffle)
internal/bars             Daily OHLCV + the read-postgres-once bar cache
internal/explore          Where and when a rule worked: per-slice edges with
                          each slice as its own control (EXPLORATION only)
internal/futures          Curve cleaning (recycled ids, duplicate streams),
                          roll detection, Panama stitching, splice-free carry
internal/patterns/coi     External "COI" 3-bar reversal pattern — RESEARCH
                          ONLY, rejected (LEDGER rows 11-13), never a rules.Rule

cmd/backtest              Full pipeline on synthetic data (demo/harness check)
cmd/run                   Daily production runner → prints the order sheet
cmd/stage                 CLI: Weinstein stage read per ticker (REPORTING ONLY)
cmd/api                   HTTP API serving screener/'s stage-analysis page
cmd/coi                   COI pattern: cache | scan | study (matched controls)
cmd/slice                 Slice explorer: a rule's edge by liquidity, size,
                          vol, price, sector, trend state, breadth and year
cmd/carver                Pre-registered verification of EWMAC + carry on
                          the two-sleeve universe, with its two controls
cmd/futures               Futures curve report: what cleaning kept, the rolls
                          found, the stitch check, and what is unusable
cmd/rulelab               scalars | corr characterise the library WITHOUT
                          returns; trial runs the pre-registered backtest
                          (that one IS a trial — see cmd/rulelab/trial.go)
```

## Quickstart

```bash
go test ./...        # includes the no-look-ahead property test
go run ./cmd/backtest   # synthetic demo (numbers meaningless by design)
```

## Long-running services (cmd/api)

`cmd/api` is kept alive via [mage](https://magefile.org/) targets rather than
a bare `go run` — the server does NOT hot-reload, so running code and
committed code can silently diverge until something restarts it (bit us once
already, on stockey's side, when a fundamentals API process served 15-day-old
code — see `mage:api Restart`'s doc comment in `magefile.go`).

```bash
go install github.com/magefile/mage@latest   # once
mage -l                # list targets
mage api:ensure         # start if not already healthy; no-op otherwise
mage api:restart        # force-restart -- run this after any code change
mage api:stop
```

`scripts/ensure_api_alive.sh` wraps `mage api:ensure` with the PATH cron
needs; it's registered in the OS-level user crontab (`crontab -e`, every 5
min, flock-guarded) the same way `scripts/sync_from_stockey.sh` already is —
see that crontab entry for the exact line. Logs: `logs/api.log` (server),
`logs/api_cron.log` (cron wrapper). Pidfile: `run/api.pid`.

When real data arrives: drop CSVs (date,open,high,low,close,volume) in
`data/`, write `config.json` (schema at top of `cmd/run/main.go`), then:

```bash
go run ./cmd/run -config config.json -data ./data
```

## Design decisions already taken (challenge via open_questions.md)

- **Zero external dependencies in v1.** EWMA/vol/EWMAC/carry are ~150 lines we
  must control and test (look-ahead safety beats library convenience).
  `cinar/indicator/v2` joins when Phase-2 technicals do.
- Reference rule set: EWMAC 16/64 (21%), 32/128 (8%), 64/256 (21%), carry
  (50%); FDM 1.31 — Carver's Ch-15 configuration, inherited not fitted.
- Engine enforces the laws mechanically: vol target ≤ 50%, FDM/IDM ≤ 2.5,
  forecasts capped ±20, inertia 10%, cost drag printed against the 0.13 SR
  speed limit, t-stat judged against the ledger's Bonferroni bar.

## Data & broker layer

**Boundary with stockey: `docs/DATA_CONTRACT.md`** — stockey = data platform
(cloud postgres, small, never load it); systrader = ALL research/TA/trading,
reading only the local mirror.

- **Local DB**: postgres `systrade` (user/pass `systrade`), tables mirrored
  1:1 from stockey. Sync: `./scripts/sync_from_stockey.sh` (remote
  172.26.39.7, falls back to local; atomic spooled copies; incremental
  tables fingerprint-reconciled against source).
- **Primary equity history**: `advisory_adjusted_ohlcv_daily` — CA-adjusted
  closes, 2013+, ~3,950 symbols incl. delisted (survivorship-honest). Use
  `Store.AdjustedCloses`; raw `dhan_ohlcv_daily` (2015+) is fallback only.
  `nseindia_indices` has index OHLCV + PE/PB/div yield (carry inputs).
  `master_dhan_instruments` has lot sizes/security ids.
- **Backfilled series**: `systrader_ohlcv_daily` (our table, never stockey's)
  holds ETFs, index spot, and futures fetched via `cmd/dhan backfill`.
  CAUTION: Dhan futures "history" is an unadjusted continuous splice, not
  per-contract data — read `docs/data_notes.md` before using it.
- **Dhan auth**: stockey owns the browser login (Chrome CDP + Playwright +
  TOTP); systrader reuses its token cache (`DHAN_TOKEN_CACHE`). If expired:
  run stockey's `python -m data.dhanlive.web_login`.
- **Order safety**: `PlaceOrder` is a dry run unless `LIVE_ORDERS=yes`.
- Config: copy `.env.example` → `.env` (never commit `.env`).

```bash
./scripts/sync_from_stockey.sh                 # refresh local data
go run ./cmd/backtest -tickers RELIANCE,TCS    # real-data backtest
go run ./cmd/dhan funds|holdings|positions     # broker smoke tests
go run ./cmd/dhan backfill                     # fill systrader_ohlcv_daily (incremental)
go run ./cmd/dhan hist -sec 14428 -seg NSE_EQ -inst EQUITY -from 2016-01-01
```

## Status

- [x] Framework core + tests + synthetic demo
- [x] Postgres data layer (systrade mirror of stockey + sync script)
- [x] Real-data backtests from `dhan_ohlcv_daily` (`-tickers` flag)
- [x] Dhan client: token reuse, funds/holdings/positions/historical, guarded orders
- [x] Backfill ETF/index/futures history via Dhan API (`cmd/dhan backfill` →
      `systrader_ohlcv_daily`; futures caveat in `docs/data_notes.md`)
- [x] Corporate-action price adjustment (`advisory_adjusted_ohlcv_daily` →
      `Store.AdjustedCloses`/`AdjustedOHLC`; backtests prefer adjusted)
- [x] Engine hardening (2026-07-26): equity-based metrics (old ones inflated
      vol/DD under compounding), gross-leverage cap for cash sleeves,
      correlation-derived point-in-time IDM, decide-at-close→fill-at-next-open,
      daily/weekly schedules, Law-1 story enforcement (`rules.Validate`),
      holdout-burn registry + walk-forward + ledger M-accounting
      (`internal/research`), property tests for all of the above
- [x] Evaluated an externally-supplied 3-bar reversal pattern ("COI",
      `rahul_ta_1.py`) — Go port at parity with the original, matched-control
      harness, REJECTED (no edge vs control; the apparent edge was the
      lower-Bollinger-band oversold condition, not the candle pattern).
      Four operator-requested follow-ups and one pattern-free re-test of the
      only surviving direction (relative strength on washout days) closed the
      family: LEDGER rows 11-13. Re-examined a fourth time with the slicing
      method in 2026-09 (`cmd/slice coi`, LEDGER row 19) against a control the
      earlier rounds lacked — every indicator computed for every liquid name,
      not just the ones that fired — and 75 of 91 buckets came back negative.
      Left behind: `internal/bars` research cache, `cmd/coi`
- [x] Indicator library implemented and characterised WITHOUT returns
      (LEDGER row 14, `cmd/rulelab`, report in `research/reports/`):
      `rules.Breakout`/`Acceleration`/`MeanReversion` with stories written
      before the code, forecast scalars measured off the forecast distribution
      (EWMAC control lands within 7-18% of Carver's published table), pairwise
      correlations pooled and within-symbol, complete-linkage grouping.
      Result: breakout duplicates EWMAC (ρ 0.84–0.94), meanrev512 is minus
      ewmac64_256 (ρ −0.95), acceleration is the only distinct family
- [x] Backtested the surviving families (acceleration ×3, meanrev1280) against
      matched controls on the construction window — pre-registered in
      `research/preregistrations/`, harness `internal/sleeve`, LEDGER row 15.
      **All four REJECTED**: every one loses to a same-universe equal-weight
      book before costs as well as after, and is indistinguishable from a
      turnover-matched random ranking. The 2022-01→2026-06 holdout was never
      read and is NOT burned. Also recorded there: the pre-registered shuffle
      control was a straw man (160%/day turnover) that made all four dead rules
      look brilliant — a control must be matched on turnover too
- [x] Verified EWMAC + carry on the two-sleeve Indian universe (`cmd/carver`,
      LEDGER row 16, pre-registered): neither sleeve beats owning it. Futures
      SR −0.02 against an always-long control at 0.50; ETFs SR 1.09 against
      1.28, with time-shifted forecasts scoring within 0.03%/month of the real
      system. Carry alone is −6.2%/yr — its story does not hold on equity index
      futures, where the basis is a financing cost rather than hedgers paying
      to shed risk. Construction gate failed, holdout NOT burned
- [x] Slice explorer (`cmd/slice`, `internal/explore`, LEDGER row 17): the
      first tool here that asks WHERE a rule worked. Found that cross-sectional
      trend — never previously tested on this universe — is monotone, not
      explained by taking more risk, and **inverts in washout regimes**, where
      the same signal's bottom fifth gains what its top fifth loses. A
      hypothesis, not a result: 62 buckets were examined and the sample is
      already mined
- [x] Cost-screened the trend hypothesis (LEDGER row 18, `cmd/slice cost`).
      It survives: 26.7%/yr at SR 1.18 vs an equal-weight book at 15%/0.75
      (+0.77%/mo, t=4.36) and vs a turnover-matched random ranking (+0.80%,
      t=4.47), holds at 2x costs, holds in both halves, and sits on a flat
      9-point parameter plateau. Almost certainly the documented Indian
      cross-sectional momentum premium, re-found. NOT promoted: a hypothesis
      mined from this sample scoring well on that same sample is what mining
      produces. Row 17's regime conditioning did NOT survive — washouts are 3%
      of days and move nothing
- [ ] Paper-trade one frozen configuration forward — the only honest evidence
      left for the trend result, and the gate `docs/RESEARCH_PROTOCOL.md` and
      Law 4 both point at. Spec written and frozen:
      `docs/strategies/2026-09-08_trend_quintile.md` (Rs 10cr floor, ewmac32,
      top quintile, equal weight, 20-day rebalance, with its kill criteria)
- [ ] Combination policies (handcrafted | Hedge | ML) judged vs the same
      matched-control baseline — on hold: every rule tested so far has failed
      its controls, so there is nothing yet worth combining
- [x] Futures stitching (`internal/futures`, `cmd/futures report`): curve
      cleaning that rejects recycled security ids (six of NIFTY's nine slots
      are option series) and collapses duplicate streams, roll detection by
      expiry calendar snapped to the ladder, Panama back-adjustment measured
      from the same-day basis, and splice-free carry annualized by the listed
      expiry gap (SILVERM's ladder is quarterly, not monthly). NIFTY,
      BANKNIFTY, GOLDM and SILVERM produce usable series; CRUDEOILM is refused
      — irregular rolls with no reference to check them against. Details and
      the limits of each detector: `docs/data_notes.md`
- [x] Fixed a UTC/IST bug that stamped every `systrader_ohlcv_daily` bar one
      day early since 2015 (one row in five on a Sunday). Source fixed in
      `dhan.BarDate`, data repaired by `db/2026-09-07_fix_backfill_date_shift.sql`,
      verified 6,165/6,165 against stockey's own table
- [ ] Matched-control baseline harness; bootstrap weight estimation
- [ ] Handcrafting helper (correlation grouping → weights + FDM/IDM)
- [ ] Instrument universe finalization (blocked on capital, A1)
- [ ] Live execution via Dhan orders API (after, and only after, paper data)
