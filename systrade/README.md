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
internal/paper            Forward paper record: order sheet, book, NAV vs two
                          benchmarks — recomputed from the start date each run
internal/paperapi         That record, shaped for the screener frontend
internal/futures          Curve cleaning (recycled ids, duplicate streams),
                          roll detection, Panama stitching, splice-free carry
internal/patterns/coi     External "COI" 3-bar reversal pattern — RESEARCH
                          ONLY, rejected (LEDGER rows 11-13), never a rules.Rule

cmd/backtest              Full pipeline on synthetic data (demo/harness check)
cmd/run                   Daily production runner → prints the order sheet
cmd/stage                 CLI: Weinstein stage read per ticker (REPORTING ONLY)
cmd/api                   HTTP API serving screener/'s stage-analysis and
                          paper-trading pages
cmd/coi                   COI pattern: cache | scan | study (matched controls)
cmd/paper                 Daily forward record for a frozen strategy
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
- [x] Paper-trading the frozen configuration forward — the only honest evidence
      left for the trend result, and the gate `docs/RESEARCH_PROTOCOL.md` and
      Law 4 both point at. Spec frozen in
      `docs/strategies/2026-09-08_trend_quintile.md`; `cmd/paper` recomputes the
      order sheet, book and NAV against an equal-weight and a random-ranking
      book every weekday (`scripts/paper_daily.sh`, 21:00 IST), and screener/'s
      Paper page shows it. Record starts 2026-09-08; judgement is pre-committed
      to wait 12 months and 12 rebalances
- [x] Attacked the strategy's known failure mode before the record began
      (LEDGER rows 20-21): volatility scaling, a 200-day regime floor, and six
      per-position stop rules. **All rejected.** Every exposure overlay lost to
      simply holding that much constantly, on both return and drawdown; no stop
      reduced drawdown and tight trailing stops increased it. Exiting at random
      beat exiting the fallers by ~2 points a year — a stop in a momentum book
      sells the names about to mean-revert. `internal/paper` gained variable
      exposure and stop rules to make any of it testable
- [x] Asked the better-posed version of the exit question (LEDGER row 22,
      `cmd/slice drawdown`): at what depth does a fall stop bouncing? Measured
      the whole curve instead of three stop levels — forward returns are flat
      to RISING with depth in volatility units, and never flip sign. No exit
      level exists, which explains why every stop failed rather than merely
      agreeing with it
- [ ] First evaluation of the paper records — trend-quintile not before 12
      months and 12 rebalances from 2026-09-08, trend-speed-blend likewise from
      2026-09-10, and the one comparison between them at 2027-09-10, per the
      specs' binding terms
- [ ] Combination policies beyond the handcrafted one (Hedge | ML), judged
      vs the same matched-control baseline — the handcrafted policy is live as
      momentum-lowvol-combination (LEDGER row 34)
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
- [x] Matched-control harness, statistics half (`internal/evidence`). The
      controls already existed, built by the same code as the books they
      control (`sleeve` C1-C3, `paper`'s equal-weight and random ranking,
      `carver`'s always-long and time-shift); what was missing was the
      arithmetic the amended Laws 2 and 4 require, which no command could
      compute. Now: a stationary block bootstrap (seeded, declared, never
      drawn), the paired edge vs a control with a bootstrap interval and p,
      row 24's matched-risk reading as `ScaleToRisk`, the Bailey-López de
      Prado deflated Sharpe against the family's trial count, BH FDR within
      the family, and `research.PurgedWalkForward`. `backtest.Metrics`'
      "DeflatedSR" was only Law 7's flat ×0.75 and is renamed `HaircutSR`.
      Bootstrap weight estimation follows Carver's appendix C — the
      cross-check on handcrafted weights, never their source (a first
      equal-means version was retired the same day; see the handcrafting
      entry below).
      Tooling only, tested on synthetic data: no market data read, no LEDGER row
- [x] Re-scored row 18's trend family with it (`cmd/slice family`, LEDGER row
      25, trials=0). All 14 configurations survive FDR within the family; only
      five survive the deflated Sharpe, and the frozen paper configuration is
      not one of them (DSR 0.915 against a 0.95 bar). Nothing changes — moving
      to a neighbouring cell that scored better is pick-the-winner — but the
      backtest case for the live paper track is now stated at its honest
      strength. Row 18's committed report turned out to hold only a shell
      error; it is regenerated and reproduces the row's headline numbers
- [x] Handcrafting helper (`internal/handcraft`): Carver's Tables 8 and 12
      transcribed from the book, the grouping tree, complete-linkage
      clustering (moved out of `cmd/rulelab`), and one diversification
      multiplier shared by FDM and IDM. Every worked example the book gives is
      a test — 46/27/27, 42/29/29, the sixteen-asset tree of Table 11, Table
      17's 21/8/21/50 with FDM 1.31, Table 48's IDM 1.89. Building its
      cross-check exposed a flaw in the morning's `evidence` bootstrap: with
      means held equal it corners any member correlated with two others (the
      middle trend speed got 0% in every resample, contradicting Table 8 on
      Carver's own numbers); replaced with his appendix-C method
- [x] **Second paper track: trend-speed-blend** (LEDGER row 26). Law 5 applied
      to the live strategy — the three slow EWMAC speeds blended 40/16/44 with
      FDM 1.10 (`rules.SpeedBlend`) instead of 32/128 picked alone. Weights
      from correlations and costs only, frozen before any return of the blend
      was computed; a pre-registered kill screen it survived (+0.60%/mo vs
      equal-weight, +0.55% vs random ranking, p<0.01, both positive at 2x
      costs); record from 2026-09-10 beside trend-quintile, which it does not
      replace. `cmd/paper -strategy`, specs registered in `paper.Specs`, the
      screener lists it on its own
- [x] Delivery % revived in stockey (`nseindia_mto`, 2013+, 6.0M rows):
      the retired parser restored from git, history reloaded from its S3
      archive, days since by the parser's own backfill; no new cron job
- [x] Trait library, step 1 (LEDGER rows 27-28, `cmd/traits`): traits
      judged WITHOUT forward returns — random gaps, 20-day persistence,
      overlap. Two duplicate groups; beta, 52-week-high distance, delivery %
      and upper/lower price-band hits are new and distinct. Market cap
      REMOVED from the slicer: it covered 8.8% of stock-dates, and no
      full-history source exists in our data (NSE's market-cap file starts
      2024-02-01). Liquidity is the size axis. Rest of the trait set pending
      the operator
- [x] Price-band hits revived in stockey (`nseindia_circuit_hit`, 2013+,
      756k rows) the same way as delivery % — `scripts/restore_retired_table.py`
      now restores either table
- [x] Slicer upgrade, step 2 (LEDGER row 29): the operator's trait set in
      `slice explore` and `slice coi` (`internal/traits` shared with
      `cmd/traits`, rolling forms tested against point forms); churn and
      net-of-cost edge per bucket; exploration stops at 2021-12-31 unless
      `-include-confirmation-years`; and a family-wise 'vs ctl' score —
      each bucket of stocks against random same-size groups from the same
      day, each bucket of days against all other days, block-bootstrapped.
      Two earlier designs (a random-score noise floor; a whole-universe
      comparison) were each caught by a synthetic calibration test and
      replaced; the shipped one flags 3 of 30 uniform-effect datasets
- [x] Momentum across time windows (LEDGER row 30, `slice windows`,
      pre-registered): 6 formation windows from the literature (3/6/9/12
      months, 12-minus-1, 12-to-7) x 5 holding periods (1/2/3/6/12 months,
      staggered books), Rs 10cr, exploration years only. Verdict IN BETWEEN:
      17 of 30 cells beat both controls and survive FDR (20 needed for a
      plateau), spread over 5 of 6 windows and all 5 holds; every cell's edge
      is positive, strongest at 6-12 month lookbacks held 1-3 months; no cell
      reaches deflated Sharpe 0.95. The reversal "known-sign controls" came
      out negative, the opposite of the pre-registered expectation
- [x] **Third paper track: momentum-lookback-blend** (LEDGER row 31). The
      literature's momentum — trailing 6, 9, 12 and 12-minus-1 month returns,
      the lookbacks whose one-month-hold cells survived row 30 — held as a
      blend of their BOOKS (`paper.ModeBookBlend`: each variant's top quintile
      at handcrafted weights 33/33/17/17, from correlations and costs only),
      rebalanced monthly. Survived its pre-registered kill screen; record from
      2026-09-12 beside the other two, replacing neither; no in-sample
      reference curve, so 2022+ stays unread for this family
- [x] Cross-strategy qualification view: every paper run records which stocks
      are in the top fifth of each strategy and of each variant inside a
      blend (`paper.Qualify`, `systrader_paper_qualify`), served as a stock ×
      strategy/variant matrix at `GET /api/paper/qualifiers` and shown on
      screener/'s Paper page — for any stock, every version of momentum it
      currently qualifies for
- [x] Fixed: the stable-shuffle control was not reproducible. Symbol ids came
      from a parallel scan's order and the control keys its per-symbol draw on
      the id, so identical runs drew different random books (8.35%, 10.64%,
      11.10%/yr). Ids are now assigned by name (`stableIDs`)
- [x] **Fourth paper track: low-volatility-blend** (LEDGER row 33): the
      lowest-risk quintiles by 3/6/12-month volatility, beta and residual
      volatility, held 11/11/11/33/34 as a blend of books, monthly. Survived
      its kill screen at matched risk (13.2%/yr at 14.8% volatility, maxDD
      −30.5%); its raw edge is not significant — the defensive profile the
      claim predicts
- [x] **Fifth paper track: momentum-lowvol-combination** (LEDGER row 34) —
      the combination item's handcrafted policy, now that two strategies have
      survived: one book of both blends' nine variants, 48% momentum / 52%
      low risk; the branches' excess returns correlate −0.12. Survived its
      kill screen (16.7%/yr at 19.1% volatility, maxDD −38.6%); its Sharpe
      ratio sits near the better parent's, not above both
- [x] What fills actually cost (LEDGER row 35, `cmd/costs`): statutory 22.3
      bps a round trip plus Rs 14.75 DP per sale; spreads 4-17 bps by
      liquidity, none paid in the opening auction; no drift after the open.
      In the Rs 10 cr+ universe a round trip runs 30-39 bps at Rs 12,500-20,000
      a position and ~48-50 at Rs 6,000 — so the 50 bps every backtest assumed
      was conservative except for the smallest positions. Also: the OS
      crontab now syncs at 15:00 UTC and runs the paper tracks at 15:30 UTC,
      after stockey's price adjustment, so order sheets use the day's prices
- [x] What the paper books become at real capital (LEDGER row 36,
      `paper capacity`, `internal/capacity`): each track's own targets in
      whole shares, Law 12 inertia, Dhan's real per-trade costs. Sized to
      today's books, **Rs 1 crore carries all five tracks faithfully** (cost
      0.6-1.5%/yr, under the 50-bps model; tracking error ≤ 0.3%/yr); Rs 30
      lakh carries only the 160-name trend books — the blends' small slices
      pay the Rs 14.75 DP charge out of Rs 6,000-12,500 positions, and a Rs
      10,000 floor fixes cost only by changing the book
- [x] A Rs 1 crore paper account on every forward track (book `account`,
      operator 2026-09-13): the same targets in whole shares, Law 12 inertia
      and Dhan's charges per trade (`capacity.AccountPolicy`). The order
      sheet shows shares, rupees and charges beside the weights; the forward
      record shows the account's NAV and realised cost beside the weight books
- [ ] **Stability gate** (`docs/RESEARCH_PROTOCOL.md`, amendment 2026-09-13):
      20 consecutive clean trading days of the scheduled runs, fresh and
      complete order sheets, one rebalance reconciled end to end, charges
      under the kill level, and the Dhan execution dry run — then capital may
      move. Replaces waiting for the 12-month reading; kill rules stay
- [ ] Multi-strategy live portfolio (operator 2026-09-13): one account holding
      several tracks, weighted by risk with Law 6's handcrafting (correlations
      and costs — not backtested returns), a stock held by several tracks
      netted into one position (one order, one DP charge). A corpus topped up
      over time, new money added on rebalance days; every track's slice at or
      above its row-36 capacity floor (Rs 1 crore for the blends, Rs 30 lakh
      for the trend books)
- [x] No cumulative testing bar (operator, 2026-09-15; TRADING_BIBLE Law 2
      second amendment, RESEARCH_PROTOCOL): the workspace-wide Bonferroni
      no longer prints or gates anything (slice, carver, backtest's report).
      Ledger rows stay (audit log); families are still judged with FDR +
      deflated Sharpe over their own trials
- [x] **Weinstein's stage analysis backtested** (LEDGER rows 37-38,
      `slice stage`, pre-registered). The stages do sort returns — Stage 2
      beat a same-size random group by +0.41%/month (p = 0.048) — but in the
      order 2 > 3 > 4 > 1, with basing (Stage 1) names the WORST. Of the
      three trading versions none survived: holding Stage 2 (17.5%/yr, maxDD
      −46% vs the universe's −58%) was suggestive alone (p ≈ 0.09) but not a
      discovery within its family, correlates 0.71 with momentum and turns
      over 1,259%/yr; the book's signature breakouts lost 6.4%/yr (−83% max
      drawdown), with or without the market-stage filter. No new track
- [x] Weinstein sliced (LEDGER row 39, `slice stage -part explore`,
      EXPLORATION): Stage 2 beats the rest of its slice by +0.61% per 20
      days, 8/8 years — strongest in smaller, more volatile, cheaper and
      price-band-hitting names, weakest (the only ◆) in the most extended ones
      (steepest 30-week MA, furthest above it). Breakouts too rare to slice
      (129 days in eight years) — our Stage 2 needs a rising MA a fresh
      breakout has not built yet. Fixed on the way: unscored thin buckets
      (they had scored ±1e14) and unstable ids in `slice explore`'s builder
- [x] The two follow-ups from row 39 (LEDGER row 40, `slice stage -part
      followups`, pre-registered): early Stage 2 (dropping the most extended
      names) did WORSE than plain Stage 2 (14.5% vs 17.1%/yr, −0.20%/month,
      p = 0.99); a faithful Weinstein breakout (close above the 30-week high
      out of a flat base, ≥ 2× volume, RS > 0) fires ~45 times a year but its
      +0.37%/month is its extra volatility — +0.02 to +0.10 at matched risk.
      Neither survives. Weinstein closed on the exploration years: Stage 2 is
      the momentum premium, held more cheaply by the momentum tracks
- [ ] Two-trait grids, only for pairs named in advance (the last piece of
      step 2, not yet built)
- [x] **The low-volatility anomaly across time windows** (LEDGER row 32,
      `slice lowvol`, pre-registered): the lowest-risk fifth by 1/3/6/12-month
      volatility, 1-year beta and 1-year residual volatility, held 1/3/6
      months, judged at matched risk. Verdict PLATEAU: 17 of 18 cells beat
      both controls and survive FDR, across every measure and hold. Every
      book ran 14-16% volatility and −28% to −40% drawdowns against the
      universe's 22% and −58%, while keeping up on raw return. A candidate
      second strategy and momentum partner — confirmation is forward only
- [ ] Instrument universe finalization (blocked on capital, A1)
- [x] Dhan order builder (`dhan orders`, `internal/execution`): a track's
      sheet as delivery market orders for the next pre-open auction (AMO
      PRE_OPEN), sells first, deterministic correlation ids so a re-run never
      places twice. Security ids matched on the EQ series by ticker OR
      bhavcopy ISIN (renames keep the ISIN; stockey's master never closes a
      vanished row). A sheet it cannot reproduce exactly — unmapped or
      ambiguous id, freeze quantity, a quantity × price far from the sheet's
      rupees, a stale sheet — is refused whole. Runs nightly as a DRY RUN
      after the paper job and logs to `systrader_exec_batch/_order`
      (stability-gate S2/S5 evidence); needs no token
- [ ] Live execution: `dhan orders -strategy <one> -live` with
      `LIVE_ORDERS=yes` is built but has never sent an order. Before it can:
      whitelist this server's static IP with Dhan (order APIs require it), a
      valid token at run time, and the gate. It refuses unless the batch is
      clean and due, Dhan's last price agrees with the sheet for every
      security id (±15%), and the buys fit the available balance (sale
      proceeds are not counted)
