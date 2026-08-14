# Security Identity

`symbol` is a trading identifier, not a permanent company identity. The canonical identity layer now lives in:

- `dim_security_history`
- `dim_security_review_events`
- `dim_security_overrides`
- `dim_security`

## Purpose

Use `security_id` whenever you need continuity across:

- symbol renames
- ISIN changes
- mergers / amalgamations
- demergers / scheme events

## Heuristics

The automatic builder is conservative:

- same `ISIN`, different `symbol` or `series` -> `rename_candidate`
- same `symbol + series`, different `ISIN` -> `identity_break_candidate`
- merger / amalgamation language in corporate-action text -> `corporate_action_identity_event`

These go into `dim_security_review_events` for manual review.

## Manual overrides

Curate exceptions in `dim_security_overrides`.

Recommended usage:

- stitch true renames onto one `security_id`
- keep mergers and demergers as explicit predecessor/successor mappings
- do not silently stitch histories on `symbol` alone

## Downstream usage

- `nseindia_corporate_actions_normalized` now carries `security_id`

This keeps the identity-aware NSE adjusted-price pipeline stable when the
market identifier changes. (`nseindia_ohlcv_adjusted` and `features_price_daily`
were an earlier design, both since removed -- confirmed live 2026-08-14 that
neither table exists; `data/nseindia/price_adjustment.py`'s factor table +
`advisory_adjusted_ohlcv_daily` view is the current adjusted-price pipeline and
does not key on `security_id`.)

systrader consumes `dhan_ohlcv_daily` (raw) and `advisory_adjusted_ohlcv_daily`
(systrader's PRIMARY series, built by `data/nseindia/price_adjustment.py` from
price steps) — see `DATA_CONTRACT.md`. The identity-aware NSE pipeline below
remains useful for reconciliation, audit, and corporate-action corroboration
(`nseindia_corporate_actions_normalized`), not as systrader's primary feed.

## Run order

Run these sequentially — they touch the same derived identity tables, do not run in parallel:

```sh
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices --only all
```

For the current operator flow, use `./complete_data.sh` for the broad refresh
(it runs `adjusted_prices --only normalize` daily) or run the individual
Python modules directly when debugging identity issues.
