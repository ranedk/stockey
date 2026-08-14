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

### NSE bhavcopy (daily price/turnover/circuit data)
`nseindia_ohlcv` (OHLC + volume, the base equity series, 2013+), `nseindia_mcap`,
`nseindia_mto` (delivery volume), `nseindia_52wk` (52-week hi/lo),
`nseindia_cmvolt` (realized volatility), `nseindia_circuit_hit`,
`nseindia_cat_turnover` (FII/DII category turnover, parsed from a separate NSE
Excel workbook), `nseindia_catg` (impact cost by category), `nseindia_var1`
(VaR margin — spans equity/bonds/SME/govt-securities/MF, not equity-only,
hence its much larger symbol count than the equity tables), `nseindia_short_selling`,
`nseindia_block_deals`, `nseindia_bulk_deals`.

`nseindia_mcap` starts 2024-02-01 and `nseindia_52wk` starts 2019-10-17 —
both well short of the 2013 bhavcopy baseline; `historical_mcap` (a frozen
archive) covers pre-2024 market cap where `nseindia_mcap` doesn't.

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
`dhan_ohlcv_intraday` (1-min bars — lives in the cloud DB, not currently
synced to systrader; see `DATA_CONTRACT.md`'s open item).

### RBI/FBIL
`rbi_bank_rates` (a rate-*change* log back to 1935, not a daily series —
sparse gaps between entries are correct, RBI just hasn't moved the repo/bank
rate), `rbi_currency_rates`, `fbil_gsec_par`, `fbil_gsec_quote`.

### Identity
`company_master`, `dim_security` (identity mapping).

## Known, understood, not-a-bug gaps

- `rbi_bank_rates` "staleness" — it's a change log, see above.
- `nseindia_holidays`/`dim_trading_days` "staleness" — forward-looking by
  design.
- `nseindia_short_selling` only starting mid-2025 — reflects when NSE began
  publishing that disclosure.
- `nseindia_var1`'s large symbol count — legitimate breadth (equity + bonds +
  SME + govt securities + MF units).
- `nseindia_cat_turnover` gaps — NSE doesn't include
  `cat_turnover_*.xls`/`Margintrdg_*.zip` in the bhavcopy archive every
  trading day; coverage genuinely varies by period, not a parser bug.
  `data_coverage_report.py` classifies it "informational" rather than
  "daily" for this reason.

## Still open

- `nseindia_mcap` / `nseindia_52wk` history gaps (2024/2019 starts vs. 2013
  baseline) — decide whether backfilling is worth it.
- `dhan_ohlcv_intraday` living in the cloud DB rather than locally, and not
  synced to systrader at all — see `DATA_CONTRACT.md`'s open item.
