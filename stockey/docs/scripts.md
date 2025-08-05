## 1. Purpose

* Describe how each crawler script ingests macro-economic data.
* Define the metadata every script must expose so incremental loads can resume safely.


## 2. Common metadata fields

* `script` - import path (relative to project root) of the crawler, e.g. `data/rbi/download_fbil_gsec.py`.
* `db` - destination table / collection / topic name, e.g. `fbil_gsec_quote`.
* `frequency` - expected arrival cadence at the **source**, not the cron interval.

  * Allowed values: `daily`, `weekly`, `monthly`, `quarterly`, `annual`, `unknown`.
* `handle_date` - strategy for turning the source's date(s) into our time index.
* `redis_key` - Redis key that stores the latest successfully ingested cursor so the next run can continue; use `null` if the script is idempotent or if the there is a database based cursor.

> **Cron vs. source frequency:** All crawlers are triggered **daily**. If `frequency` isn't `daily`, the script must check whether new data exists and exit early if nothing changed.

## 3. `handle_date` strategies

* `forward_fill` - use for "level" series where the value remains valid until a new one arrives (e.g. policy rate, index levels).

  * Rule: store value on `as_on_date` and automatically extend it forward until a new record arrives.

* `on_this_date` - use for one-off events or daily flows (e.g. FPI net flow, dividend declaration date).

  * Rule: insert exactly one row on `as_on_date`; custom logic to model

* `future_data` - use for schedules or targets published ahead of time (e.g. auction calendar).

  * Rule: generate rows for future dates based on offsets such as `offset: {months: 1, day: 1}` or `offset: {months: 0, day: 'last'}`.

* `revision_overwrite` - use for datasets that republish history (e.g. GDP revisions).

  * Rule: upsert on `(date, version)` or delete the affected date range and re-insert latest values.

* `cumulative_reset` - use for YTD metrics that reset at FY or quarter boundaries.

  * Rule: detect boundary dates; reset running total; derive daily/period deltas if needed.

* `snapshot_panel` - use for cross-section snapshots with extra dimensions (e.g. full yield curve).

  * Rule: treat `as_on_date` as the index; keep other dimensions in columns or a separate child table.


## 3. Script inventory

```
script: data/eaindustry/wpi.py
db: eaindustry_wpi
frequency: monthly
handle_date: forward_fill | offset : {months: 1, days: 14}   # 14 of the next month
redis_key: wpi:downloaded
```

```
script: data/rbi/download_fbil_gsec.py
db: fbil_gsec_quote
frequency: daily
handle_date: forward_fill | offset: { days: 7 }              # declared next week
redis_key: fbilgec:downloaded
```

```
script: data/rbi/download_fbil_gsec.py
db: fbil_gsec_par
frequency: daily
handle_date: forward_fill | offset: { days: 7 }              # declared next week
redis_key: fbilgec:downloaded
```

```
script: data/fred/us_macro.py
db: macro_usa
frequency: unknown
handle_date: forward_fill | offset: { days: 10}
redis_key: null
```

```
script: data/fred/us_macro.py
db: macro_india_gdp
frequency: unknown
handle_date: forward_fill | offset: { days: 10}
redis_key: null
```

```
script: data/fred/us_macro.py
db: macro_usa_ism
frequency: daily
handle_date: forward_fill | offset: { days: 2}
redis_key: null
```

```
script: data/nsdl/fpi.py
db: fii_investments
frequency: daily
handle_date: on_this_date | offset: { days: 1}
redis_key: nsdl:fpi:downloaded          # any future date is summary for end of month and end of year, getting updated daily
```

```
script: data/nsdl/fpi.py
db:fii_derivatives
frequency: daily
handle_date: on_this_date | offset: { days: 1}
redis_key: nsdl:fpi:downloaded          # any future date is summary for end of month and end of year, getting updated daily
```

```
script: data/sharpelydata/sharpely_data.py
db:shareholding_category
frequency: daily
handle_date: on_this_date | offset: { days: 1}
redis_key: null
```


```
script: data/sharpelydata/sharpely_data.py
db:shareholding_top_holders
frequency:
handle_date: on_this_date | offset: { days: 1}
redis_key: null
```

```
script: data/sharpelydata/sharpely_data.py
db:historical_mcap
frequency:
handle_date:
redis_key:
```

```
script: data/sharpelydata/scrip_master.py
db:master_sharpely_funds
frequency: daily
handle_date: null
redis_key: null
```

```
script: data/sharpelydata/scrip_master.py
db:master_sharpely_equity
frequency: daily
handle_date: null
redis_key: null
```

```
script: data/dhanlive/scrip_master.py
db: master_dhan_instruments
frequency: daily
handle_date: null
redis_key: null
```
