# Data Coverage & Completeness

Point-in-time snapshot from the 2026-08-05 completeness sweep, plus the
mechanism that keeps it current going forward. See `docs/DATA_INVENTORY.md`
for the authoritative table-ownership list this document assumes.

## Live tool

`scripts/data_coverage_report.py` runs the same checks below automatically —
row counts, symbol/entity coverage, date range, and a staleness verdict for
tables expected to advance every trading day. It writes one row per table per
day to `data_coverage_report` (so trends are queryable over time) and prints a
text/JSON summary for cron logs.

```sh
python -m scripts.data_coverage_report                 # text, persists to the DB
python -m scripts.data_coverage_report --format json    # for programmatic use
python -m scripts.data_coverage_report --no-persist     # dry run
python -m scripts.data_coverage_report --require        # exit 1 if any table is in "error"
```

Scheduled nightly at 22:40 (`all_data_coverage_report.sh`, right after
`all_data_readiness.sh`) — see `config/stockey.crontab.template`.

**This document is a snapshot, not a substitute for running the tool.** Treat
every number below as "true as of 2026-08-05" — check `data_coverage_report`
or re-run the script for current state.

## Table categories and what's inside them

### NSE bhavcopy (daily price/turnover/circuit data)
`nseindia_ohlcv` (OHLC + volume, the base equity series, 2013+, ~7,150
symbols), `nseindia_mcap`, `nseindia_mto` (delivery volume), `nseindia_52wk`
(52-week hi/lo), `nseindia_cmvolt` (realized volatility), `nseindia_circuit_hit`,
`nseindia_cat_turnover` (FII/DII category turnover, parsed from a separate NSE
Excel workbook), `nseindia_catg` (impact cost by category), `nseindia_var1`
(VaR margin — spans equity/bonds/SME/govt-securities/MF, not equity-only,
hence its much larger symbol count), `nseindia_short_selling` (NSE only
started publishing this in mid-2025).

**Gap:** `nseindia_mcap` only starts 2024-02-01 and `nseindia_52wk` only
starts 2019-10-17 — both well short of the 2013 bhavcopy baseline. Not fixed
here; flagged for a decision on whether backfill is worth pursuing.

### NSE corporate actions
`nseindia_corporate_actions_bc_raw` (the real, comprehensive source — parsed
from the bhavcopy CA feed, ~5,700 symbols) and `nseindia_corporate_actions_normalized`
(derived from it via `data/nseindia/adjusted_prices.py --only normalize`,
detects split/bonus/etc. and feeds `data/dhanlive/ohlcv.py`'s
recent-adjustment check). `events_dividend` / `events_capital_change` cover
dividends and capital changes separately (~19% of `events_dividend` rows lack
`dividend_amount` — handle nulls in any TR math).

**Fixed 2026-08-05:** `nseindia_corporate_actions_normalized` was silently
1+ year stale because nothing ever scheduled its writer — only ever run
manually. Now runs daily in `download_runner.PARSER_STEPS`.

**Also removed:** the plain `nseindia_corporate_actions` table/collector was
dropped from the KEEP set — it ran full NSE browser automation daily but only
ever covered 2 placeholder symbols in `config/tracked_symbols.txt`, entirely
superseded by `_bc_raw`.

### Price adjustment (systrader's PRIMARY series)
`advisory_adjusted_ohlcv_daily` — derived purely from price steps
(`data/nseindia/price_adjustment.py`), corroborated by declared NSE corporate
actions where available. 2013+, ~3,970 symbols, current.

### NSE indices / calendar
`nseindia_indices` (2014+, 226 index names, current). `nseindia_holidays` /
`dim_trading_days` are forward-looking reference data by design — a "stale"
max date here is normal, not a bug (they cover the calendar year ahead).

### Dhan broker
`master_dhan_instruments` (the full instrument master — 757K rows, ~400K
distinct `symbol_name` because it includes every F&O strike/expiry
combination, not just equities), `dhan_ohlcv_daily` (2015+, fallback series),
`dhan_ohlcv_intraday` (1-min bars, live since 2025-09-22, 22M+ rows — see
`DATA_CONTRACT.md`'s correction: this landed in the cloud DB, not the local-only
landing zone originally proposed, and systrader doesn't currently sync it).

### RBI/FBIL
`rbi_bank_rates` (a rate-*change* log back to 1935, not a daily series — sparse
gaps between entries are correct, RBI just hasn't moved the repo/bank rate),
`rbi_currency_rates`, `fbil_gsec_par`, `fbil_gsec_quote`.

**Fixed 2026-08-05:** `rbi_currency_rates` was 12 days stale because
`data/rbi/download_currency_rates.py` had **never** been registered in
`download_runner.DOWNLOADER_STEPS` in this file's entire git history — only
ever run manually. Now scheduled daily.

### Identity
`company_master`, `dim_security` (identity mapping; `effective_from` tops out
2026-02-09 — worth checking whether anything listed since then is missing an
identity record).

### Sharpely (mcap slice only)
`historical_mcap` — 2012+, but only 973 symbols vs. ~7,150 in the full equity
universe (narrower than `nseindia_mcap`'s 3,309).

**Fixed 2026-08-05:** the sync silently failed every day *after* its first
successful run, because Sharpely's API 400s on a same-day (`start_date ==
end_date`) window — exactly what a normal incremental day produces once
yesterday is already captured. Now always requests a >=1-day window.

## Known, understood, not-a-bug gaps

- `rbi_bank_rates` "staleness" — see above, it's a change log.
- `nseindia_holidays`/`dim_trading_days` "staleness" — forward-looking by
  design.
- `nseindia_short_selling` starting mid-2025 — reflects when NSE began
  publishing the disclosure.
- `nseindia_var1`'s large symbol count — legitimate breadth (equity + bonds +
  SME + govt securities + MF units), verified via series-code breakdown.
- `nseindia_cat_turnover` occasionally lagging — its source is a separate NSE
  Excel workbook the parser already treats as "occasionally corrupt at
  source" (see `data/nseindia/bhavcopy_parser.py`'s `soft_error_substrings`
  handling); a multi-day gap is more likely a run of bad source files than a
  code bug, but worth watching via the daily report if it doesn't self-heal.

## Still open (not resolved by this pass)

- `nseindia_mcap` / `nseindia_52wk` history gaps (2024/2019 starts vs. 2013
  baseline) — decide whether backfilling is worth it.
- `dhan_ohlcv_intraday` living in the cloud DB rather than locally, and not
  synced to systrader at all — see `DATA_CONTRACT.md`'s open item.
- `dim_security` identity-mapping freshness for anything listed since
  2026-02-09.
- Broader `docs/` cleanup — several files (`advisory_manual.md`,
  `llm_decision_authority.md`, `hypothesis_*.md`, `operator_*.md`, and
  similar) still describe the pre-2026-07-27 advisory system.
