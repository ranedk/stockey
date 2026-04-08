# Operator Manual

This is the short runbook for daily use.

Use this file when you want the important commands, when to run them, and what each one is for.

## Main operating modes

### 1. Full advisory run

Use when:

- you want the current end-to-end advisory output
- you want the portfolio summary in one command
- you want model training to run first if the event-model horizon is ready

Command:

```sh
./all_full_advisory.sh
```

Useful variants:

```sh
./all_full_advisory.sh --skip-model-training
./all_full_advisory.sh --skip-downloads
./all_full_advisory.sh --date 2026-04-08
```

What it does:

1. runs model prep and training if ready
2. runs the advisory pipeline
3. prints the current portfolio summary

### 2. Advisory only

Use when:

- raw data is already fresh
- you want the batch advisory output without model-training prep

Command:

```sh
./all_advisory.sh
```

### 3. Model prep and training

Use when:

- you are working on the event model
- you want to backfill labels and see if training is justified

Command:

```sh
./all_model_training.sh
```

Useful variants:

```sh
./all_model_training.sh --prep-only
./all_model_training.sh --horizon-days 1
```

What it does:

1. runs `advisory.event_model_data_prep`
2. checks label coverage
3. trains only if the requested horizon is ready
4. scores current events after training unless skipped

### 4. Continuous watch

Use when:

- you want the live watch loop running during market hours
- you want active watchlist names and open positions monitored continuously
- you want the static dashboard refreshed automatically

Command:

```sh
./all_continuous_watch.sh --loop
```

Useful variants:

```sh
./all_continuous_watch.sh --loop --sleep-seconds 300
./all_continuous_watch.sh --loop --ohlcv-interval-seconds 300 --news-interval-seconds 1800 --announcement-interval-seconds 1800
```

What it watches:

- active advisory watchlist names
- open positions from portfolio and lifecycle outputs
- recent intraday OHLCV
- exchange announcements
- ET/news

Important behavior:

- there is no default cap on how many symbols the router may reevaluate
- symbols stop being watched once advisory removes them from the watch path
- open positions remain monitored for exit-related alerts
- alerts and cycle summaries are also published over Redis pub-sub

### 5. Live notifier

Use when:

- you want a human-readable operator feed from the Redis pub-sub stream
- you want a rolling log of live watch activity without reading raw JSON tables

Command:

```sh
./all_live_notifier.sh
```

Useful variants:

```sh
./all_live_notifier.sh --output-dir live_dashboard
./all_live_notifier.sh --duration-seconds 600
```

What it writes:

- `live_dashboard/operator_feed.json`
- `live_dashboard/operator_feed.jsonl`
- `live_dashboard/operator_feed.txt`

The live dashboard page now also shows the latest operator feed entries directly.

## Dashboard

Generate and serve the lightweight dashboard with:

```sh
python -m http.server --directory live_dashboard 8000
```

If the continuous watch loop is already running, it keeps rewriting:

- `live_dashboard/index.html`
- `live_dashboard/dashboard.json`

## Important inspection commands

### Portfolio

```sh
python -m advisory.portfolio_engine --include-planned --format text
```

### One setup trace

```sh
python -m advisory.setup_trace DEFENSIVE_REGIME_POSITION_V1 --format text
```

### One symbol trace

```sh
python -m advisory.symbol_trace LUPIN --format text
```

### Research ledger

```sh
python -m advisory.research_ledger --limit 20
```

### Live routing dry-run

```sh
python -m advisory.event_router --dry-run
```

## Recommended daily order

### Batch mode

1. `./all_full_advisory.sh`
2. inspect portfolio output
3. inspect any setup or symbol traces that look unusual

### Live monitoring mode

1. `./all_continuous_watch.sh --loop`
2. `./all_live_notifier.sh`
3. `python -m http.server --directory live_dashboard 8000`
4. inspect `advisory.event_router --dry-run` if routing volume looks suspicious

## Redis pub-sub channels

The continuous-watch stack publishes lightweight messages on these channels:

- `stockey:continuous_watch:alerts`
- `stockey:continuous_watch:ohlcv`
- `stockey:continuous_watch:news`
- `stockey:continuous_watch:announcements`
- `stockey:continuous_watch:router`
- `stockey:continuous_watch:dashboard`
- `stockey:continuous_watch:summary`

These are for loose coordination and observability. The database remains the source of truth.
