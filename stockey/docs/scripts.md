```
script: data/eaindustry/wpi.py
db:eaindustry_wpi
frequency: monthly
handle_date: month
redis_key: wpi:downloaded
```


```
script: data/rbi/download_fbil_gsec.py
db:fbil_gsec_quote
frequency: daily
handle_date: as_on_date
redis_key: fbilgec:downloaded
```


```
script: data/rbi/download_fbil_gsec.py
db:fbil_gsec_par
frequency: daily
handle_date: as_on_date
redis_key: fbilgec:downloaded
```


```
script: data/fred/us_macro.py
db:macro_usa
frequency: unknown
handle_date: as_on_date
redis_key: null
```


```
script: data/fred/us_macro.py
db:macro_india_gdp
frequency: unknown
handle_date: as_on_date
redis_key: null
```


```
script: data/fred/us_macro.py
db:macro_usa_ism
frequency: daily
handle_date: as_on_date
redis_key: null
```


```
script: data/nsdl/fpi.py
db:fii_investments
frequency:
handle_date:
redis_key:
```


```
script: data/nsdl/fpi.py
db:fii_derivatives
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:events_capital_change
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:events_dividend
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:events_earnings
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:ticker
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:report_date
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:shareholding_category
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:shareholding_top_holders
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:trades_bulk
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/sharpely_data.py
db:stock": ticker,
frequency:
handle_date:
redis_key:
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
frequency:
handle_date:
redis_key:
```


```
script: data/sharpelydata/scrip_master.py
db:master_sharpely_equity
frequency:
handle_date:
redis_key:
```
