Below is the **check-list of data you’ll need to assemble before we write a single line of modelling code.**
I’ve grouped everything by feature-block and flagged the *bare-minimum* history to support our rolling walk-forward back-tests (train 12 m → val 1 m → test 1 m, stepped monthly).

---

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

## 7 To-Do for You Before Next Call

1. **Pick vendors** for intraday & fundamentals (TrueData vs. GDFL vs. Refinitiv, etc.).
2. Verify **licensing/quotas** allow bulk historical download—some APIs throttle to 1 y per call.
3. Download a *sample day* of 5-min bars and post the CSV headers here so I can stub the feature-store schema.
4. Confirm you can export fundamentals with *announcement timestamp*, not just period-end dates.

Once you have sample files / API docs in hand, share the field lists and we’ll:

* Write the `ingestion/` scripts.
* Define the DuckDB DDL.
* Plug in the cost model & get our first *dry-run* back-test running.

**Collect the data first—nothing kills an MVP faster than a half-empty feature matrix.**

