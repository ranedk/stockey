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
- The old `nseindia_ohlcv_adjusted` (2 symbols) is a dead stub; ignore it.

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

## Dhan futures history is NOT contract-specific

Empirical finding (2026-07-24): requesting history for a specific contract's
security id returns a series far longer than the contract's life (e.g. the
Aug-2026 GOLDM contract returns bars back to 2015). Dhan reuses security-id
"slots" per curve position; the history of a slot is the **unadjusted
concatenation of successive contract generations** at roughly that curve
position. Evidence:

- Overlapping GOLDM slots show a persistent contango ladder on every
  historical date (near < 2nd < 3rd), long before those contracts listed.
- At a prior generation's expiry (2025-08-05) the `GOLDM-2026-08-05` series
  jumps −1.2% on a day spot gold (GOLDBEES) moved +0.4% — an unadjusted roll
  splice. Only the tail since the previous generation's expiry is the genuine
  named contract.
- Dead slots (already-expired contracts) and far slots that haven't traded
  return DH-905 "no data" — the backfill logs these as `skip`.

### Consequences (Bible: never difference across a splice)

- **Carry: usable now.** Basis = (position-2 close − position-1 close) on the
  same date uses no time-differencing, so splices cancel out of the signal.
  Pick the two nearest live slots per underlying per date.
- **EWMAC / P&L on futures: NOT directly usable.** Returns across roll dates
  embed the roll gap. Panama-stitch first; the roll dates are the slot's
  generation expiries (≈ the expiry calendar in `master_dhan_instruments`),
  and each gap is visible as a same-day divergence vs the spot series.
- ETF and index-spot series have no such issue.

## Operations

```bash
go run ./cmd/dhan backfill                 # all groups, incremental (safe to re-run)
go run ./cmd/dhan backfill -group etf      # etf | index | futures | all
go run ./cmd/dhan backfill -from 2015-01-01   # start date when table is empty
```

Incremental: each series resumes from its stored `max(date)+1`; upserts are
idempotent. Rate-limited to ~3 req/s. Run daily after `sync_from_stockey.sh`
(the token must be fresh — see README, Dhan auth).
