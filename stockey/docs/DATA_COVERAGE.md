# Data Coverage & Completeness

See `docs/DATA_INVENTORY.md` for the authoritative table-ownership list this
document assumes.

## Live tool

`scripts/data_coverage_report.py` is the source of truth for current
coverage — row counts, symbol/entity coverage, date range, and a staleness
verdict for tables expected to advance every trading day. It writes one row
per table per day to `data_coverage_report` (so trends are queryable over
time) and prints a text/JSON summary for cron logs.

```sh
python -m scripts.data_coverage_report                 # text, persists to the DB
python -m scripts.data_coverage_report --format json    # for programmatic use
python -m scripts.data_coverage_report --no-persist     # dry run
python -m scripts.data_coverage_report --require        # exit 1 if any table is in "error"
```

Scheduled nightly (`all_data_coverage_report.sh`, right after
`all_data_readiness.sh`) — see `config/stockey.crontab.template`. This
document explains what each table category is and its known, permanent
quirks; run the tool for current numbers.

## Table categories and what's inside them

### NSE bhavcopy (daily price/turnover data)
`nseindia_ohlcv` (OHLC + volume, the base equity series, 2013+), `nseindia_mcap`.

`nseindia_mcap` starts 2024-02-01, well short of the 2013 bhavcopy baseline;
`historical_mcap` (a frozen archive) covers pre-2024 market cap where
`nseindia_mcap` doesn't.

`nseindia_mto` (delivery volume), `nseindia_52wk` (52-week hi/lo),
`nseindia_cmvolt` (realized volatility), `nseindia_circuit_hit`,
`nseindia_cat_turnover`, `nseindia_catg`, `nseindia_var1` (VaR margin), and
the offmarket-sourced `nseindia_short_selling`/`nseindia_block_deals`/
`nseindia_bulk_deals` were all retired 2026-08-15 (write-only, zero readers
anywhere, confirmed live) — archived to S3 and dropped. See
`docs/DATA_INVENTORY.md`'s "Retired 2026-08-15" section for the full list
and rationale.

### NSE corporate actions
`nseindia_corporate_actions_bc_raw` (the comprehensive source — parsed from
the bhavcopy CA feed) and `nseindia_corporate_actions_normalized` (derived
from it via `data/nseindia/adjusted_prices.py`, detects split/bonus/etc. and
feeds `data/dhanlive/ohlcv.py`'s recent-adjustment check). `events_dividend`
/ `events_capital_change` cover dividends and capital changes separately — a
minority of `events_dividend` rows lack `dividend_amount`, handle nulls in
any TR math.

### Price adjustment (systrader's PRIMARY series)
`advisory_adjusted_ohlcv_daily` is a view over raw `nseindia_ohlcv` joined
with `nseindia_adjustment_factors` (`data/nseindia/price_adjustment.py`) —
split/bonus factor derived purely from price steps, corroborated by declared
NSE corporate actions where available; total-return factor derived from
`events_dividend`. Always in sync with `nseindia_ohlcv` since nothing is
separately written for the adjusted series itself.

### NSE indices / calendar
`nseindia_indices`. `nseindia_holidays` / `dim_trading_days` are
forward-looking reference data by design — a "stale" max date here is
normal, not a bug (they cover the calendar year ahead).

### Dhan broker
`master_dhan_instruments` (the full instrument master — includes every F&O
strike/expiry combination, not just equities, so its distinct-symbol count is
much larger than the equity universe), `dhan_ohlcv_daily` (fallback series),
`dhan_ohlcv_intraday` (1-min bars — lives in the cloud DB; systrader reads it
live via a `postgres_fdw` foreign table, not a sync, see `DATA_CONTRACT.md`).

`dhan_ohlcv_daily` is neither purely raw nor continuously adjusted — Dhan
applies its own split/bonus adjustment to a symbol's history whenever it
re-syncs that symbol (triggered here by `has_recent_adjustment()`), but the
timing can differ from NSE's official ex-date by a few days either way, and
older history only reflects whatever was known as of Dhan's last re-fetch (a
symbol with two splits may show only the more recent one applied to its
oldest rows). `scripts/price_data_sanity.py`'s Dhan cross-check accounts for
this with a multi-day window rather than a strict same-day comparison.

### RBI/FBIL
`rbi_bank_rates` (a rate-*change* log back to 1935, not a daily series —
sparse gaps between entries are correct, RBI just hasn't moved the repo/bank
rate), `rbi_currency_rates`, `fbil_gsec_par`. (`fbil_gsec_quote` retired
2026-08-15 — write-only, zero readers.)

### Identity
`company_master`, `dim_security` (identity mapping).

## Known, understood, not-a-bug gaps

- `rbi_bank_rates` "staleness" — it's a change log, see above.
- `nseindia_holidays`/`dim_trading_days` "staleness" — forward-looking by
  design.

## Still open

- `nseindia_mcap` history gap (2024 start vs. 2013 baseline) — decide
  whether backfilling is worth it.
- `dim_trading_days` has no producer in the current codebase (confirmed live
  2026-08-14) — populated through 2026-12-31 today, not stale, but a live
  cron job (`ohlcv_reconcile.py`) depends on it; find/rebuild the producer
  before end of 2026. See `DATA_CONTRACT.md`'s table.
- `dhan_ohlcv_intraday` has a confirmed historical gap for 2026-08-14 through
  the day this was found (2026-08-30): 4,211 ticker-day rows fall short of a
  full session's 375 one-minute bars (as few as 89), caused by an IST/UTC bug
  in `choose_intraday_refresh_end`'s live-sync fallback (fixed same day, see
  `data/dhanlive/ohlcv.py`'s `_ist_now()`) — the fix stops new gaps, it does
  NOT repair the ones already stored. A targeted re-fetch for the affected
  ticker/day pairs (`SELECT ticker, date_trunc('day', timestamp) FROM
  dhan_ohlcv_intraday GROUP BY 1, 2 HAVING count(*) < 370`) is still needed.
### Resolved 2026-08-21

- `advisory_identity_issues` / `advisory_fallback_events` were write-only —
  active recording (Dhan identity-issue tracking, fallback telemetry) but no
  reader/resolver was ever scheduled (confirmed live 2026-08-14), so issues
  accumulated with no review. `scripts/issue_digest.py` (`all_issue_digest.sh`,
  scheduled nightly right after `all_data_coverage_report.sh`) is now that
  reader: rechecks and auto-closes open identity issues that have since
  resolved themselves, then reports what's left plus a 24h fallback-telemetry
  summary. See `CLAUDE.md`'s Current Architecture job list.

### Resolved 2026-08-15

The write-only tables previously listed here (`master_sharpely_funds`,
`nseindia_mto`/`_52wk`/`_cmvolt`/`_circuit_hit`/`_cat_turnover`/`_catg`/
`_var1`, `nseindia_short_selling`/`_block_deals`/`_bulk_deals`,
`fbil_gsec_quote`, plus 3 `fundamentals_*` tables found in a follow-up sweep)
were retired rather than given a consumer — no near-term use case for any of
them, so the collectors were stopped and the tables archived+dropped instead
of continuing to run and monitor code nobody used. See
`docs/DATA_INVENTORY.md`'s "Retired 2026-08-15" section.

Also fixed the same day: a live `NameError` crash in `data/dhanlive/client.py`
(introduced by an earlier same-day commit, broke every Dhan historical-data
pull including the 18:45 UTC `ohlcv_reconcile.py` cron job) and a frozen-price
bug in `fundamentals/screens/watchlist_exit.py` (see git history for both).

Three external data sources were separately diagnosed as currently broken
(not stockey code bugs — see the diagnostic notes attached to the 2026-08-15
commit): NSE offmarket downloads and RBI bank-rate downloads both show
signs of the source site changing/blocking rather than a stockey regression
(offmarket's un-gated post-navigation clicks likely hit NSE's WAF; RBI's
DBIE portal shows an interstitial modal `download_bank_rates.py` has no
dismiss logic for). `fundamentals.screens.l2_state`'s screener.in valuation
query (added 2026-08-13) was isolated with its own try/except so a query
failure no longer takes down the whole L2 step; the underlying screener.in
response issue itself needs live investigation.
