## 1 Price & Microstructure

| Dataset                         | Horizon                                                | Symbols                                        | History to Pull                                           | Mandatory Fields                              | Typical Sources (India)                                                                                     |
| ------------------------------- | ------------------------------------------------------ | ---------------------------------------------- | --------------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **Intraday OHLCV**              | 5-min bars (or 1-min if that’s what your vendor sells) | Top-300 liquidity universe                     | **2015-01-01 → present** (gives ≥8 y, plenty for regimes) | ts, open, high, low, close, volume, vwap      | 👉  NSE “Bhavcopy (Historical 1-min)” via GDFL *TrueData*, **nSē data**, Symphonny Fintech, GlobalDataFeeds |
| **Daily OHLCV**                 | EoD                                                    | Same                                           | 2010-01-01 → present (for factor look-backs)              | date, o/h/l/c, adj\_close, volume             | NSE EoD “CM Bhavcopy”, BSE EoD, Quandl “NSE”                                                                |
| **Corporate Actions**           | Events                                                 | Same                                           | Full span                                                 | split\_ratio, bonus\_ratio, dividend, ex-date | NSE “N-corporate actions” CSV; Refinitiv; TickerPlant                                                       |
| **INDIAVIX  &  Sector Indices** | 1-min **or** daily                                     | INDIAVIX, NIFTY50, NIFTYBANK, sectoral indices | 2015-01-01 → present                                      | ts/date, open/high/low/close                  | NSE index data feed (indexes/Bhavcopy “index”)                                                              |

**Notes**

* Pull raw (unadjusted) prices + a separate corporate-action table.
* We will adjust for splits/bonuses in the feature store to keep survivorship audits easy.

---

## 2 Fundamental & Event Data

| Dataset                            | Frequency           | History              | Fields (minimum)                                                               | Sources                                                                                   |
| ---------------------------------- | ------------------- | -------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| **Quarterly financials**           | Quarterly           | FY 2010 → present    | PAT, EBITDA, Revenue, Gross Margin, OCF, book\_value, share\_count             | CapitalIQ, Refinitiv, TickerPlant “Fundamental”, **Screener.in** export                   |
| **Ratios**                         | Calculated          | same                 | PE, PB, ROE, Debt/Equity, EV/EBITDA, etc.                                      | (can compute ourselves once raw statements in)                                            |
| **Earnings announcement calendar** | Event (timestamped) | 2010-01-01 → present | announce\_date\_time, period\_ending, eps\_actual, eps\_consensus if available | NSE corporate filings API, Refinitiv, Bloomberg                                           |
| **Street sentiment (optional)**    | At earnings         | same                 | surprise\_flag (+/−), magnitude                                                | You’ll likely need a data vendor; otherwise derive sign from ΔEPS vs. trailing 4Q average |

**Lag rule** – every fundamental datapoint is available **T + 1 trading day at 09:15 IST** in our simulations (to avoid look-ahead).

---

## 3 Macro & Regime Drivers

| Dataset                                  | Frequency                | History              | Source                            |
| ---------------------------------------- | ------------------------ | -------------------- | --------------------------------- |
| **INR USD Spot**                         | Daily                    | 2010-01-01 → present | RBI reference rate CSV            |
| **10-Year GoI Yield**                    | Daily                    | same                 | FRED “IRLTLT01INM156N”; CCIL data |
| **RBI Repo / Reverse-Repo**              | Event + daily last-known | 2005-01-01 → present | RBI Monetary Policy bulletins     |
| **CPI / WPI Inflation**                  | Monthly                  | 2010 → present       | MOSPI                             |
| **GDP (YoY, QoQ)**                       | Quarterly                | 2005 → present       | MOSPI                             |
| **RBI Monetary Policy meeting calendar** | Event                    | 2015 → present       | RBI website                       |

We’ll forward-fill daily so each bar has the *latest known* macro figures, then one-hot flags for “MPC week” etc.

---

## 4 Cost & Tax Benchmarks (for realistic fills)

* Historical bid-ask midpoint + spread for top-300 (pull from the same intraday vendor—spread often available).
* **Exchange fee schedules** – they hardly change, but capture rate-change dates.
* **Historical STT / stamp changes** – tiny impact but makes back-test auditable.

---

## 5 Live Feeds / Real-Time APIs

| Need                                         | Recommended Feed                                          | Why                                        |
| -------------------------------------------- | --------------------------------------------------------- | ------------------------------------------ |
| **5-min OHLCV live**                         | Same vendor that gave historical (keeps schema identical) |                                            |
| **Order-book snapshot (optional for later)** | Broker’s WebSocket (if offered)                           | for slippage analytics                     |
| **Corporate actions / filings intraday**     | NSE “NEAPS Notification” WebSocket or email scrapes       | lets us schedule fundamental lag correctly |

---

## 6 Formats & Delivery Expectations

* **Store everything in UTC + 05:30 (Asia/Kolkata) tz**; convert to UTC only if we ever co-locate.
* Ingest raw CSV → Parquet partitioned by `date=` for lightning-fast DuckDB scans.
* Create one **survivorship table** listing every symbol’s list-/delist dates; needed for realistic universe filters.

---

## 7 To-Do

1. **Pick vendors** for intraday & fundamentals (TrueData vs. GDFL vs. Refinitiv, etc.).
2. Verify **licensing/quotas** allow bulk historical download—some APIs throttle to 1 y per call.
3. Download a *sample day* of 5-min bars and post the CSV headers here so I can stub the feature-store schema.
4. Confirm you can export fundamentals with *announcement timestamp*, not just period-end dates.

Once you have sample files / API docs in hand, share the field lists and we’ll:

## RBI Website:

Link: `https://data.rbi.org.in/#/dbie/dataquery_enhanced`

```
Daily - FOREX_RATE_AFY_RN
Daily - YIELD_TB_RN
Monthly - CPI_RUC_RN
Monthly - INX_WPI_RN
Weekly - FR_EXG_RESV_RN
Monthly - INX_NEER_REER_M_RN
Quarterly  - IND_EXTRN_DEBT_RN
Daily - MONEY_MKT_OP_RN
Daily - SEC_MKT_RN
Weekly - RMC_W_RN
Monthly - RMC_M_RN
Monthly - IIP
Monthly - WHOLE_PRICE_INDEX_INF_RN
```


Link: `https://data.rbi.org.in/BOE/OpenDocument/2409211437/OpenDocument/opendoc/openDocument.jsp?logonSuccessful=true&shareId=1`

```
Repo rate and Reverse Repo rate
```

# Plan going forward


## 1  Write the exact label and backtest budget into code

> **Target**   `fwd_ret_5d = ln(Close(+5) / Close(0))`
> **Horizon**  5 **calendar** days (≈ 3-4 trading sessions).
> **Universe** All NSE equities that traded that day.
> **Capital**  INR 60 lacs, 10 bps one-way cost, 10 bps slippage.
> **Metric**   Out-of-sample Sharpe after costs.

Put that in a README or notebook header so you (and I) stop re-answering “what are we predicting?”

---

## 2  Create a *point-in-time* daily price ladder (Python / SQL)

**Goal:** one row per symbol-date with corporate-action-adjusted OHLCV and a forward 5-day return.

1. **Pull raw close prices** from your Dhan table.
2. **Adjust for actions**

   ```sql
   -- Example: compute cumulative split factor
   SELECT t.symbol,
          t.date,
          t.close,
          COALESCE(prod.split_factor,1) AS cfa
   FROM   raw_prices t
   LEFT JOIN (
       SELECT symbol,
              ex_date,
              1.0 * new_share_terms / old_share_terms AS split_factor
       FROM   events_capital_change
       WHERE  event_type IN ('Stock Split','Bonus')
   ) prod
   ON t.symbol = prod.symbol
      AND t.date >= prod.ex_date;
   ```

   Multiply `Close`, `Open`, `High`, `Low` by the cumulative factor; divide `Volume`.
3. **Write the adjusted series** to a new table `pt_prices_adj`.
4. **Label creation**

   ```python
   import pandas as pd, numpy as np
   df = pd.read_sql('select symbol,date,close from pt_prices_adj order by symbol,date', conn)
   df['close_fwd_5'] = df.groupby('symbol')['close'].shift(-5)
   df['fwd_ret_5d']  = np.log(df['close_fwd_5'] / df['close'])
   df.to_sql('pt_prices_features', conn, if_exists='replace', index=False)
   ```

*Sanity check:* the distribution of `fwd_ret_5d` should be centred near 0 with σ≈2-3 %.

---

## 3  Implement a dead-simple baseline strategy (20 / 100-day SMA cross)

1. **Feature**

   ```python
   df['sma20']  = df.groupby('symbol')['close'].transform(lambda x: x.rolling(20).mean())
   df['sma100'] = df.groupby('symbol')['close'].transform(lambda x: x.rolling(100).mean())
   df['signal'] = np.where(df['sma20'] > df['sma100'], 1, -1)   # long/flat for now
   ```
2. **Hold for 5 days**: when a signal is generated, hold that weight for 5 trading days (simplest approximation to your 3–5-day horizon).
3. **Back-test loop**
   *Daily capital allocation:* equal-weight among active longs, cash otherwise; cap weight so position size ≤ INR 2 lacs.
   Deduct 20 bps round-trip per trade.
4. **Metrics** — cumulative return, annualised Sharpe, max drawdown.

If this baseline shows Sharpe ≫ 0 after costs, you have evidence of exploitable momentum; if not, momentum at this horizon is probably dead for the broad NSE universe.

---

### What to send me when you’re done

* A CSV (or screenshot) with **Sharpe, CAGR, drawdown** for 2015-2024.
* A quick note on any data-quality problems you hit (missing days, split factors off, etc.).

Once that’s in hand, we’ll:

* add fundamentals & macro as extra features,
* fit a 3-state HMM regime and slice performance by regime,
* move to LightGBM with purged walk-forward CV.

