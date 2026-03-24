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
- `nseindia_ohlcv_adjusted` now carries `security_id`
- `features_price_daily` groups on `security_id`

This keeps the legacy/reference NSE adjusted-price pipeline stable when the market identifier changes.

For the advisory runtime path, OHLCV now comes from `dhan_ohlcv_daily`; the identity-aware NSE pipeline remains useful for reconciliation, audit, and other non-advisory derived features.

## Run order

Run these sequentially:

```sh
python -m data.nseindia.security_history
python -m data.nseindia.security_dimension
python -m data.nseindia.adjusted_prices --only all
python -m features.price_daily
```

Do not run the first three in parallel. They touch the same derived identity tables.

For the legacy NSE-derived daily watchlist/reference processing, the same sequence is wrapped by:

```sh
./all_daily_derivations.sh
```
