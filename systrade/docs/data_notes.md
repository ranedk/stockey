# Data Notes

## advisory_adjusted_ohlcv_daily — primary equity backtest series (2026-07-26)

Stockey's gap-fix landed in a NEW table, `advisory_adjusted_ohlcv_daily`
(5.8M rows, 3,957 symbols, 2013-07 → present): `close` raw, `adj_close`
split/bonus-adjusted via `cum_adj_factor`, `ca_flag` marks adjustment events,
`series` EQ/BE. Facts that matter:

- **Delisted symbols are included** (1,201 symbols whose last bar predates
  2026) → survivorship-honest universe. Never filter to currently-live names.
- **Use `Store.AdjustedCloses` (EQ only) for all return math.** Raw
  `dhan_ohlcv_daily` closes fabricate fake returns at split/bonus dates;
  `cmd/backtest` now prefers adjusted and labels any raw fallback loudly.
- Adjustment is splits/bonuses only — dividends are NOT in the price series
  (total-return needs `events_dividend` separately).
- The old `nseindia_ohlcv_adjusted` dead stub was confirmed empty and dropped
  from stockey entirely on 2026-08-14; it no longer exists to sync or ignore.

## Sync reconciliation (why it exists)

Incremental-on-date sync is blind to rows inserted or rewritten at
HISTORICAL dates — exactly what gap backfills and corporate-action
re-adjustment do (2026-07-26: source had ~273k historical rows in
dhan_ohlcv_daily and 6× more nseindia_ohlcv that local never received).
`sync_from_stockey.sh` therefore fingerprints each incremental table after
syncing (row count + max(load_ts) where present) and does a full re-copy on
any drift. If a table with re-adjustment churn ever lacks load_ts, move it to
FULL_TABLES rather than trusting the count alone.

# systrader_ohlcv_daily (Dhan backfill)

`cmd/dhan backfill` fills `systrader_ohlcv_daily` (a table WE own — never
write into the stockey-mirrored tables; their incremental sync keys on local
`max(date)` and foreign rows would silently break it).

## What's in the table (verified 2026-07-24)

| Group | Tickers | Depth | Semantics |
|---|---|---|---|
| ETFs (NSE_EQ) | NIFTYBEES, JUNIORBEES, BANKBEES, GOLDBEES, SILVERBEES, LTGILTBEES, GILT5YBEES, MON100 | 2015+ (SILVERBEES 2022+, gilt ETFs 2019/2021+) | true per-instrument daily OHLCV |
| Index spot (IDX_I) | NIFTY, BANKNIFTY | 2015+ | true index levels |
| Futures (NSE_FNO / MCX_COMM) | `UNDERLYING-<expiry>`, e.g. `GOLDM-2026-08-05` | mostly 2015+ | **continuous curve-position series — see below** |

## Dhan futures history: what it actually is (re-verified 2026-09-07)

The 2026-07-24 note in this file said the slots were a clean ladder of curve
positions, each an unadjusted concatenation of contract generations. Half
right. Reading the data properly — after fixing the date bug below — three
things are true, and two of them were missing:

1. **Slots are positional, not contract-specific.** A security id's history is
   a continuous series at roughly one curve position, splicing successive
   generations together with no adjustment. This part was right.
2. **Dhan recycles security ids, and the history comes with them.** Six of
   NIFTY's nine stored slots are not NIFTY futures at all: `NIFTY-2026-06-30`
   prints 117.10 on a day the index closed 10458.40 — an option premium series
   living on a recycled id. BANKNIFTY has seven such. Any of these fed to a
   backtest as "NIFTY futures" would produce numbers, and they would be
   nonsense.
3. **Several ids serve one identical stream.** Four of GOLDM's seven slots, and
   four of CRUDEOILM's, are byte-identical to another slot. Only about three
   genuine curve positions exist per underlying.

`internal/futures` therefore proves every series before using it — level within
15% of the rest of the curve (adjacent positions differ only by carry), daily
returns correlating above 0.80 with it, duplicates collapsed — and records what
it rejected and why. `go run ./cmd/futures report` prints the lot;
`research/reports/2026-09-07_futures_curve_report.txt` is that output.

### Roll dates: the hard part

Nothing in the data marks a roll, and the ladder cannot reveal one on its own:
every position shifts at the same moment, so the whole curve simply re-labels
itself. The front's price steps by the basis, which is indistinguishable from
an ordinary market move of the same size. Measured on a synthetic curve with a
basis three times daily volatility — friendlier than any real contract — the
ladder-only detector finds 23 of 35 rolls.

What works:

- **Calendar + ladder (used).** The listed expiries give the month with
  certainty and the ladder picks the day inside a +/-4 day window: 32 of 35 on
  the same fixture. A roll snapped one day off corrupts that single daily
  difference and nothing else.
- **Reference divergence (implemented, finds nothing here).** In principle a
  splice shows up as the futures moving when spot did not. In practice the
  NIFTY monthly ladder step is only ~40 bps while futures-vs-spot tracking
  noise runs ~12 bps a day, so the biggest splice reaches 11.5 robust sigmas
  and nothing clears a 20-sigma bar. This is worth stating plainly: the
  detector returning nothing is NOT evidence that the series is unspliced.

The stitch is then checked on its own terms — a roll day's move, in units of a
typical day's move, before and after adjustment. NIFTY goes 2.08x -> 1.28x
against an ordinary 1.37x; GOLDM 3.56x -> 1.62x against 1.99x. A series where
the splice survives is refused rather than shipped.

### What is usable today

| Underlying | Verdict | Note |
|---|---|---|
| NIFTY | usable | 3 positions, monthly, ladder step ~40 bps |
| BANKNIFTY | usable | 3 positions, monthly, ~60 bps |
| GOLDM | usable | 3 positions, monthly, ~190 bps |
| SILVERM | usable, flagged | ladder is QUARTERLY (Aug/Nov/Feb), and one 188-day gap between rolls says history is missing |
| CRUDEOILM | **refused** | roll spacing reaches 1202 days against a 31-day cycle, and no reference series exists to check it against |

### Consequences (Bible: never difference across a splice)

- **Carry: usable now, and splice-free by construction.** Basis =
  (second position - front) on the same date, annualized by the gap between
  their expiries — read that gap off the listed expiries, never off the roll
  cadence: SILVERM's positions are three months apart while its rolls look
  monthly, and assuming monthly overstates its carry threefold.
- **EWMAC / P&L: use the Panama series, not the raw slot.** The adjustment is
  additive, so price DIFFERENCES are exact everywhere and percentage returns
  far back in history are not. That is the standard Panama trade-off and it
  suits Carver's framework, which sizes on price-unit volatility.
- ETF and index-spot series have no such issue.

## The date shift that made all of this unreadable (fixed 2026-09-07)

Every bar in `systrader_ohlcv_daily` was stamped one calendar day early, from
2015 to 2026, because `cmd/dhan backfill` derived dates with
`time.Unix(...).In(time.Local)` and this host runs UTC. Dhan stamps a daily bar
at the start of the IST trading day, so 2026-08-07 00:00 IST became 2026-08-06
18:30 UTC and the bar was filed under the 6th. One row in five landed on a
Sunday — Monday's bar. NIFTY's COVID low sat on Sunday 2020-03-22.

Fixed at the source (`dhan.BarDate`, with `IST` as a fixed +05:30 zone so it
cannot depend on the host or on tzdata being installed) and repaired in place
by `db/2026-09-07_fix_backfill_date_shift.sql`. The repair is exact, not
approximate: of the 6,165 rows overlapping stockey's authoritative
`dhan_ohlcv_daily`, 6,165 match its closes when shifted a day forward and 18
match as stored.

Nothing downstream had read the table yet, so no published result was affected
— the equity research runs off `advisory_adjusted_ohlcv_daily`. Had the ETF
sleeve been backtested first, every fill would have been a day out.

## Operations

```bash
go run ./cmd/dhan backfill                 # all groups, incremental (safe to re-run)
go run ./cmd/dhan backfill -group etf      # etf | index | futures | all
go run ./cmd/dhan backfill -from 2015-01-01   # start date when table is empty
```

Incremental: each series resumes from its stored `max(date)+1`; upserts are
idempotent. Rate-limited to ~3 req/s. Run daily after `sync_from_stockey.sh`
(the token must be fresh — see README, Dhan auth).

## What the NSE equity universe does to a forecast (measured 2026-09-07)

From `rulelab scalars` over 2.68M liquid symbol-days, 2013-07→2026-06
(LEDGER row 14, no returns involved):

- **Trend forecasts are not centred on zero here.** Mean RAW EWMAC is +0.43 /
  +0.80 / +1.41 for fast 16 / 32 / 64 against a mean absolute value of 2.26 /
  3.37 / 4.97 — the slower the rule, the more of its average forecast is a
  standing long. On a 13-year equity bull market that is exactly what one
  should expect, and it is the same beta that LEDGER row 4 found masquerading
  as selection alpha. Any equity backtest of a trend rule that does not carry
  a matched control is measuring that drift, not the rule.
- The mirror shows up in mean reversion (mean raw −0.14 / −0.18): price sits
  above its multi-year average most of the time, so the rule is structurally
  short this universe.
- **Trend per unit of vol is weaker on single stocks than on Carver's futures
  portfolio**: our measured EWMAC scalars are 7-18% above his published ones
  (4.43/2.97/2.01 vs 3.75/2.65/1.87), i.e. a given trend produces a smaller
  raw forecast here. Idiosyncratic stock vol in the denominator is the obvious
  candidate; nothing here tests that.
