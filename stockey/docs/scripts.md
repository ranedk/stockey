```
script: data/eaindustry/wpi.py
db:eaindustry_wpi
frequency: daily
date: first_of_month
redis_key: wpi:downloaded

```


```
script: data/rbi/download_fbil_gsec.py
db:fbil_gsec_quote
frequency: daily
date: everyday
redis_key: fbilgec:downloaded
```


```
script: data/rbi/download_fbil_gsec.py
db:fbil_gsec_par
frequency: daily
date: everyday
redis_key: fbilgec:downloaded
```


```
script: data/fred/us_macro.py
db:macro_usa
date: everyday
```


```
script: data/fred/us_macro.py
db:macro_india_gdp
```


```
script: data/fred/us_macro.py
db:macro_usa_ism
```


```
script: data/nsdl/fpi.py
db:fii_investments
```


```
script: data/nsdl/fpi.py
db:fii_derivatives
```


```
script: data/sharpelydata/sharpely_data.py
db:events_capital_change
```


```
script: data/sharpelydata/sharpely_data.py
db:events_dividend
```


```
script: data/sharpelydata/sharpely_data.py
db:events_earnings
```


```
script: data/sharpelydata/sharpely_data.py
db:ticker
```


```
script: data/sharpelydata/sharpely_data.py
db:report_date
```


```
script: data/sharpelydata/sharpely_data.py
db:shareholding_category
```


```
script: data/sharpelydata/sharpely_data.py
db:shareholding_top_holders
```


```
script: data/sharpelydata/sharpely_data.py
db:trades_bulk
```


```
script: data/sharpelydata/sharpely_data.py
db:stock": ticker,
```


```
script: data/sharpelydata/sharpely_data.py
db:historical_mcap
```

```
script: data/sharpelydata/scrip_master.py
db:master_sharpely_funds
```


```
script: data/sharpelydata/scrip_master.py
db:master_sharpely_equity
```
