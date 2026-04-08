# Advisory Change Cookbook

This is the short practical guide for common changes.

Use [`docs/advisory_manual.md`](advisory_manual.md) for the full operator manual. Use this file when you want a concrete recipe.

## 1. Add a new screener and connect it to a setup

### Register the Screener.in URL

```sh
python -m data.screenerin.screener_registry add "https://www.screener.in/screens/1234567/my-new-screen/"
```

### Sync the raw screener snapshot

```sh
python -m data.screenerin.screener_parser
```

### Normalize to the advisory universe

```sh
python -m advisory.screener_parser
```

### Add a setup in [config/advisory_setups.yaml](../config/advisory_setups.yaml)

Example:

```yaml
- setup_id: MY_SETUP_V1
  setup_name: My setup
  screener_slug: my-new-screen
  allowed_regimes:
    - STABLE
    - BULL_NARROW
  market_cap_min: 5000
  min_avg_traded_value_20d: 100000000
  max_breakout_extension_pct: 8
  min_dist_52w_high: -15
  technical_rules:
    - column: pass_above_dma_50
      operator: eq
      value: true
    - column: rs_vs_benchmark
      operator: gte
      value: 0.0
  watch_reasons:
    - earnings
    - guidance
```

### Test the setup

```sh
python -m advisory.rule_engine --dry-run
```

### If it looks right, persist it

```sh
python -m advisory.rule_engine
python -m advisory.watchlist_builder
```

## 2. Loosen or tighten one setup’s thresholds

Edit [config/advisory_setups.yaml](../config/advisory_setups.yaml).

Common changes:

- loosen liquidity: reduce `min_avg_traded_value_20d`
- allow more extended names: increase `max_breakout_extension_pct`
- require stronger leaders: raise `rs_vs_benchmark` or `rs_vs_sector`
- make setup more defensive: add `debt_to_equity_vs_sector` cap

After changing the file:

```sh
python -m advisory.rule_engine --dry-run
python -m advisory.symbol_trace HDFCBANK --format text
```

If correct:

```sh
python -m advisory.rule_engine
```

## 3. Trace one symbol through the pipeline

Use the symbol trace CLI:

```sh
python -m advisory.symbol_trace HDFCBANK --format text
```

JSON form:

```sh
python -m advisory.symbol_trace HDFCBANK
```

Setup-specific trace:

```sh
python -m advisory.symbol_trace HDFCBANK --setup LARGECAP_BREAKOUT_V1 --format text
```

What it shows:

- latest screener membership
- latest technical and fundamental snapshots
- latest candidate row
- latest rejection reasons
- watchlist state
- announcement and ET RSS events
- latest LLM event verdict
- latest allocation, portfolio, lifecycle, and execution states

## 3b. Trace one setup through the full funnel

Use the setup trace CLI:

```sh
python -m advisory.setup_trace LARGECAP_BREAKOUT_V1 --format text
```

JSON form:

```sh
python -m advisory.setup_trace LARGECAP_BREAKOUT_V1
```

What it shows:

- screener universe size
- candidate count
- rejection count and top rejection reasons
- watchlist and event funnel size
- latest evaluation, allocation, portfolio, lifecycle, and execution counts

## 3c. Show all setups in one command

Use the dashboard CLI:

```sh
python -m advisory.dashboard --format text
```

JSON form:

```sh
python -m advisory.dashboard --format json
```

This is the fastest way to see:

- regime per setup date
- screener universe count
- candidate count
- rejection count
- watch and evaluation funnel counts
- top rejection reason

## 4. Refresh only event processing

For official announcements plus ET RSS:

```sh
python -m advisory.announcement_watch
python -m data.economictimes.rss
python -m advisory.news_watch
python -m advisory.llm_event_evaluator
```

## 5. Rebuild the full advisory flow after a strategy change

```sh
python -m advisory.pipeline --include-watch --include-news --dry-run
python -m advisory.pipeline --include-watch --include-news
```

## 6. Check why the rule engine produced zero candidates

Run:

```sh
python scripts/sql_query_runner.py --read-only "select * from advisory_candidate_rejections order by asof_date desc, setup_id, symbol limit 100"
```

Then trace one rejected symbol:

```sh
python -m advisory.symbol_trace IGL --format text
```

## 7. Validate after any change

```sh
python -m pytest tests/test_advisory_regression.py
```
