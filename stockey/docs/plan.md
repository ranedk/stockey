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


# Plan for second level of cleanup: FEATURE TABLES

## 1 Generate **the missing date set** in one shot

```sql
WITH mkt_days AS (          -- all trading dates in your OHLCV
    SELECT DISTINCT trade_date
    FROM   stg_ohlcv_daily
),
gaps AS (                   -- dates absent from *any* ftr table
    SELECT m.trade_date
    FROM   mkt_days m
    LEFT  JOIN ftr_daily_matrix f
           ON f.trade_date = m.trade_date
    WHERE  f.trade_date IS NULL
)
SELECT * FROM gaps;         -- ~0-N rows, only what you still need
```
---

## 2 Insert the missing block in one set-based statement

```sql
INSERT INTO ftr_daily_matrix (trade_date, repo_rate, cpi, …)
SELECT g.trade_date,
       LAST_VALUE(r.rate     IGNORE NULLS)
         OVER (ORDER BY g.trade_date) AS repo_rate,
       LAST_VALUE(c.value    IGNORE NULLS)
         OVER (ORDER BY g.trade_date) AS cpi,
       …
FROM   gaps                g
LEFT   JOIN cln_rbi_bank_rates r
       ON r.effective_date <= g.trade_date
LEFT   JOIN cln_macro_cpi   c
       ON c.release_date   <= g.trade_date
-- add joins for every series
QUALIFY ROW_NUMBER() OVER (PARTITION BY g.trade_date ORDER BY 1) = 1;
```

*Window functions give you the forward-fill on the fly; you never touch older rows.*
---

## 3 Make the script self-contained & idempotent

```bash
BEGIN;                              # 1️⃣ single txn
  CREATE TEMP TABLE delta AS … ;    # build block
  INSERT INTO ftr_daily_matrix       -- 2️⃣ idempotent insert
  SELECT * FROM delta
  ON CONFLICT (trade_date) DO NOTHING;
COMMIT;
```

If the job crashes before `COMMIT`, nothing is half-written.

---

## 4 Health guard-rails to keep

1. **Gap test** after INSERT:

   ```sql
   SELECT COUNT(*) FROM dim_calendar d
   WHERE  d.is_trading_day
     AND  d.cal_date <= :latest_bhavcopy_date
     AND  NOT EXISTS (SELECT 1
                      FROM   ftr_daily_matrix f
                      WHERE  f.trade_date = d.cal_date);
   ```

   If > 0 ➜ alert.

2. **Freshness test** for each macro: `MAX(release_date)` ≥ `latest_bhavcopy_date – expected_lag`.

# Better implementation using generic python scripts

```
# etl_features.py
import pandas as pd
from typing import List, Dict, Literal, Callable, Optional
from sqlalchemy import create_engine, text

Strategy = Literal["ffill", "step", "binary", "custom"]

# ---------------------------------------------------------------------
# 0.  DB connection helper (adjust URI or pass an existing engine)
# ---------------------------------------------------------------------
def make_engine(uri: str):
    return create_engine(uri, pool_pre_ping=True, future=True)

# ---------------------------------------------------------------------
# 1.  Main driver ------------------------------------------------------
# ---------------------------------------------------------------------
def load_into_ftr(
    eng,
    *,
    stg_table: str,
    stg_date_col: str,
    stg_value_cols: List[str] | Dict[str, str],  # {"stg_col": "ftr_col"}
    ftr_table: str,
    calendar_table: str = "dim_calendar",
    ohlcv_table: str = "stg_ohlcv_daily",
    trade_date_col: str = "trade_date",
    strategy: Strategy | Callable[[pd.Series], pd.Series] = "ffill",
    chunksize: int = 10_000,
):
    """
    Move data from `stg_table` to `ftr_table` for trading dates that are
    present in OHLCV but missing from the ftr table, applying a fill strategy.

    Parameters
    ----------
    eng              : SQLAlchemy engine
    stg_table        : name of staging macro table (e.g., 'stg_rbi_bank_rates')
    stg_date_col     : the column that tells *when the value first became public*
    stg_value_cols   : list or {'stg_col': 'ftr_col'}
    ftr_table        : name of wide daily feature table
    calendar_table   : trading-calendar dimension (must flag trading days)
    ohlcv_table      : table whose latest date defines “up-to-date”
    trade_date_col   : date column name in ftr_table
    strategy         : 'ffill' | 'step' | 'binary' | callable(series)->series
    """
    # ------------ 1a.  Normalise column mapping -----------------------
    if isinstance(stg_value_cols, list):
        stg_value_cols = {c: c for c in stg_value_cols}  # same names
    ftr_cols = list(stg_value_cols.values())

    # ------------ 1b.  Identify dates we still need -------------------
    with eng.begin() as con:
        last_done = con.execute(
            text(f"SELECT COALESCE(MAX({trade_date_col}), '1900-01-01') "
                 f"FROM {ftr_table}")
        ).scalar()

        gap_dates = pd.read_sql(
            f"""
            SELECT cal_date AS {trade_date_col}
            FROM   {calendar_table}
            WHERE  is_trading_day
              AND  cal_date >  :last_done
              AND  cal_date <= (SELECT MAX({trade_date_col})
                                FROM   {ohlcv_table})
            ORDER  BY cal_date
            """,
            con,
            params={"last_done": last_done},
            parse_dates=[trade_date_col],
        )

    if gap_dates.empty:
        print(f"[{ftr_table}] up-to-date — nothing to do")
        return 0

    start, end = gap_dates.iloc[0, 0], gap_dates.iloc[-1, 0]

    # ------------ 1c.  Pull the staging slice once --------------------
    stg_df = pd.read_sql(
        f"""
        SELECT {stg_date_col}, {', '.join(stg_value_cols.keys())}
        FROM   {stg_table}
        WHERE  {stg_date_col} <= :end
        """,
        eng,
        params={"end": end},
        parse_dates=[stg_date_col],
    ).sort_values(stg_date_col)

    if stg_df.empty:
        raise ValueError(f"No data found in {stg_table} up to {end}")

    stg_df.rename(columns=stg_value_cols, inplace=True)

    # ------------ 1d.  Re-index on the missing trading dates ----------
    df = gap_dates.set_index(trade_date_col)

    # Join & fill
    df = df.join(
        stg_df.set_index(stg_date_col), how="left"
    )

    if callable(strategy):
        df[ftr_cols] = strategy(df[ftr_cols])
    else:
        _apply_builtin_strategy(df, ftr_cols, strategy)

    df.reset_index(inplace=True)

    # ------------ 1e.  Bulk insert ------------------------------------
    n_before = len(df)
    df.to_sql(
        ftr_table,
        eng,
        if_exists="append",
        index=False,
        method="multi",
        chunksize=chunksize,
    )
    print(f"[{ftr_table}] inserted {n_before} new rows")
    return n_before


# ---------------------------------------------------------------------
# 2.  Built-in fill strategies ----------------------------------------
# ---------------------------------------------------------------------
def _apply_builtin_strategy(df: pd.DataFrame, cols: List[str], strategy: Strategy):
    if strategy == "ffill":
        df[cols] = df[cols].ffill()
    elif strategy == "step":
        # Alias for ffill (used for rate changes)
        df[cols] = df[cols].ffill()
    elif strategy == "binary":
        for c in cols:
            df[c] = df[c].notna().astype(int)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

```

## Usage

```
from etl_features import make_engine, load_into_ftr

engine = make_engine("postgresql+psycopg2://user:pw@host/db")

# 1) RBI repo rate – treated as a step function
load_into_ftr(
    engine,
    stg_table="stg_rbi_bank_rates",
    stg_date_col="effective_date",
    stg_value_cols=["rate"],
    ftr_table="ftr_daily_matrix",
    strategy="step",
)

# 2) CPI – monthly index, forward-fill after release
load_into_ftr(
    engine,
    stg_table="stg_macro_cpi",
    stg_date_col="release_date",
    stg_value_cols={"value": "cpi"},
    ftr_table="ftr_daily_matrix",
    strategy="ffill",
)

# 3) Custom fill — e.g., z-score normalise per year
def yearly_zscore(series_df: pd.DataFrame) -> pd.DataFrame:
    return (
        series_df.groupby(series_df.index.year)
                 .transform(lambda s: (s - s.mean()) / s.std())
    )

load_into_ftr(
    engine,
    stg_table="stg_macro_iip",
    stg_date_col="release_date",
    stg_value_cols={"value": "iip"},
    ftr_table="ftr_daily_matrix",
    strategy=yearly_zscore,          # custom
)
```

## 2 Feature layer (rebuilt nightly)
Create one materialised view per event-window feature and join them into your ftr_daily_matrix.

2.1 Anticipation & absorption clocks

```
-- Parameters you might tune later
\set pre_days  7   -- look-up to 7 sessions before release
\set post_days 10  -- fade out 10 sessions afterwards
\set half_life 3   -- for exponential decay

WITH base AS (
  SELECT d.cal_date AS trade_date,
         e.symbol,
         e.announce_date,
         e_period := e.for_period
  FROM   dim_calendar d
  JOIN   corp_events e
         ON d.cal_date BETWEEN
            e.announce_date - :pre_days::int
            AND e.announce_date + :post_days::int
  WHERE  e.event_type = 'EARNINGS_RELEASE'         -- the actual number
    AND  d.is_trading_day
)
SELECT trade_date,
       symbol,
       /* exponential ramp-up until release */
       CASE
         WHEN trade_date <= announce_date
         THEN 1 - EXP(  (trade_date - announce_date) / :half_life )
         /* exponential fade after release */
         ELSE EXP( - (trade_date - announce_date) / :half_life )
       END                     AS earn_event_score
FROM   base;

```

2.2 Surprise & revision signals

```
SELECT
    symbol,
    fil_date   AS release_date,
    value - LAG(value) OVER (PARTITION BY symbol, field ORDER BY fil_date)
           AS qoq_delta
FROM fundamentals_snapshot
WHERE field = 'PAT';
```

4 Python helper (plug into previous load_into_ftr)

```
def make_event_clock(df_events, pre_days=7, post_days=10, half_life=3):
    """
    Return a DataFrame indexed by trade_date with 'earn_event_score'.
    df_events must have columns ['announce_date']  and the index = trade_date.
    """
    import numpy as np

    def decay(days):          # vectorised exp decay
        return np.exp(-days / half_life)

    out = pd.Series(0.0, index=df_events.index, name="earn_event_score")

    for ann_date in df_events['announce_date'].unique():
        mask = (out.index >= ann_date - pd.Timedelta(days=pre_days)) & \
               (out.index <= ann_date + pd.Timedelta(days=post_days))
        days_to_event = (out.index[mask] - ann_date).days
        scores = np.where(days_to_event <= 0,
                          1 - decay(-days_to_event),  # ramp-up
                          decay(days_to_event))       # fade
        out.iloc[mask] = np.maximum(out.iloc[mask], scores)  # overlap safe
    return out.to_frame()

```
