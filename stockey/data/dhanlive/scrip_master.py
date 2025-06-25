"""
update_dhan_master.py
Usage:
    python update_dhan_master.py
"""

import sys
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import duckdb
import pandas as pd
import requests
from environs import Env


env = Env()
env.read_env()
DUCKDB_FILE = Path(env("DUCKDB"))
SCHEMA = env("SCHEMA")  # place the table in its own schema


DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.dhan_instrument_master (
    exch_id                 VARCHAR,
    segment                 VARCHAR,
    security_id             BIGINT,
    isin                    VARCHAR,
    instrument              VARCHAR,
    underlying_security_id  BIGINT,
    underlying_symbol       VARCHAR,
    symbol_name             VARCHAR,
    display_name            VARCHAR,
    instrument_type         VARCHAR,
    series                  VARCHAR,
    lot_size                INTEGER,
    sm_expiry_date          DATE,
    strike_price            DOUBLE,
    option_type             VARCHAR,
    tick_size               DOUBLE,
    expiry_flag             VARCHAR,
    bracket_flag            VARCHAR,
    cover_flag              VARCHAR,
    asm_gsm_flag            VARCHAR,
    asm_gsm_category        VARCHAR,
    buy_sell_indicator      VARCHAR,
    buy_co_min_margin_per   DOUBLE,
    sell_co_min_margin_per  DOUBLE,
    buy_co_sl_range_max_perc DOUBLE,
    sell_co_sl_range_max_perc DOUBLE,
    buy_co_sl_range_min_perc DOUBLE,
    sell_co_sl_range_min_perc DOUBLE,
    buy_bo_min_margin_per   DOUBLE,
    sell_bo_min_margin_per  DOUBLE,
    buy_bo_sl_range_max_perc DOUBLE,
    sell_bo_sl_range_max_perc DOUBLE,
    buy_bo_sl_range_min_perc DOUBLE,
    sell_bo_sl_min_range    DOUBLE,
    buy_bo_profit_range_max_perc DOUBLE,
    sell_bo_profit_range_max_perc DOUBLE,
    buy_bo_profit_range_min_perc DOUBLE,
    sell_bo_profit_range_min_perc DOUBLE,
    mtf_leverage            DOUBLE,

    valid_from              TIMESTAMP,
    valid_to                TIMESTAMP,
    load_ts                 TIMESTAMP
);

CREATE INDEX idx_active_instruments
ON {SCHEMA}.dhan_instrument_master (security_id, valid_to);

"""

UPDATE_SQL = f"""
-- STEP 1  : close current version where anything has changed OR disappeared
UPDATE {SCHEMA}.dhan_instrument_master dst
SET    valid_to = $load_ts
FROM   _stage st
WHERE  dst.security_id = st.security_id
  AND  dst.valid_to IS NULL
  AND  (
        dst.series         IS DISTINCT FROM st.series OR
        dst.lot_size       IS DISTINCT FROM st.lot_size OR
        dst.sm_expiry_date IS DISTINCT FROM st.sm_expiry_date OR
        dst.strike_price   IS DISTINCT FROM st.strike_price OR
        dst.option_type    IS DISTINCT FROM st.option_type OR
        dst.instrument     IS DISTINCT FROM st.instrument OR
        dst.expiry_flag    IS DISTINCT FROM st.expiry_flag
      );
"""

INSERT_SQL = f"""
-- STEP 2  : insert brand-new security_id OR changed version
INSERT INTO {SCHEMA}.dhan_instrument_master
SELECT
       st.exch_id, st.segment, st.security_id, st.isin, st.instrument,
       st.underlying_security_id, st.underlying_symbol, st.symbol_name, st.display_name,
       st.instrument_type, st.series, st.lot_size, st.sm_expiry_date, st.strike_price,
       st.option_type, st.tick_size, st.expiry_flag, st.bracket_flag, st.cover_flag,
       st.asm_gsm_flag, st.asm_gsm_category, st.buy_sell_indicator,
       st.buy_co_min_margin_per, st.sell_co_min_margin_per,
       st.buy_co_sl_range_max_perc, st.sell_co_sl_range_max_perc,
       st.buy_co_sl_range_min_perc, st.sell_co_sl_range_min_perc,
       st.buy_bo_min_margin_per, st.sell_bo_min_margin_per,
       st.buy_bo_sl_range_max_perc, st.sell_bo_sl_range_max_perc,
       st.buy_bo_sl_range_min_perc, st.sell_bo_sl_min_range,
       st.buy_bo_profit_range_max_perc, st.sell_bo_profit_range_max_perc,
       st.buy_bo_profit_range_min_perc, st.sell_bo_profit_range_min_perc,
       st.mtf_leverage,
       $load_ts                AS valid_from,
       NULL                    AS valid_to,
       $load_ts                AS load_ts
FROM   _stage st
LEFT   JOIN {SCHEMA}.dhan_instrument_master m
       ON st.security_id = m.security_id
      AND m.valid_to IS NULL
WHERE  m.security_id IS NULL;
"""


def india_today() -> str:
    """Return YYYYMMDD for 'today' in Asia/Kolkata (UTC+5:30)."""
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    return ist_now.strftime("%Y%m%d")


def download_latest_scrip_master_csv() -> str:
    """Download latest script master"""
    tmpfile = tempfile.NamedTemporaryFile(delete=False)
    TIMEOUT = 30
    URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    try:
        resp = requests.get(URL, timeout=TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        print(" FAILED")
        sys.exit(f"[ERROR] Request failed: {exc}")

    if len(resp.content) < 10_000:  # ~10 KB
        sys.exit("[ERROR] Downloaded file suspiciously small. Aborting.")

    tmpfile.write(resp.content)
    tmpfile.close()
    return tmpfile.name


def update_scrip_master_from_csv(csv_path: str):
    """Update the database with new scrip master"""
    csv_file = Path(csv_path)
    if not csv_file.exists():
        sys.exit(f"File {csv_file} not found.")

    # 1. read CSV – duckdb can do it directly, but pandas lets us fix dtypes easily
    df = pd.read_csv(
        csv_file,
        dtype={
            "EXCH_ID": "string",
            "SEGMENT": "string",
            "SECURITY_ID": "Int64",
            "ISIN": "string",
            "INSTRUMENT": "string",
            "UNDERLYING_SECURITY_ID": "Int64",
            "UNDERLYING_SYMBOL": "string",
            "SYMBOL_NAME": "string",
            "DISPLAY_NAME": "string",
            "INSTRUMENT_TYPE": "string",
            "SERIES": "string",
            "LOT_SIZE": "float64",
            "SM_EXPIRY_DATE": "string",
            "STRIKE_PRICE": "float64",
            "OPTION_TYPE": "string",
            "TICK_SIZE": "float64",
            # remaining numeric columns will default to float64
        },
    )

    # parse date columns *after* reading to preserve blanks as NaT
    df["SM_EXPIRY_DATE"] = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce")

    # 2. connect to DuckDB
    con = duckdb.connect(DUCKDB_FILE.as_posix())
    con.execute(DDL)

    # 3. load staging table (temporary)
    con.register("df", df)
    con.execute("CREATE OR REPLACE TEMP TABLE _stage AS SELECT * FROM df;")

    # 4. run update and insert inside a single transaction
    load_ts = pd.Timestamp.utcnow()
    con.execute("BEGIN;")
    con.execute(UPDATE_SQL, {"load_ts": load_ts})
    con.execute(INSERT_SQL, {"load_ts": load_ts})
    con.execute("COMMIT;")

    print(f"{len(df):,} rows ingested.   load_ts = {load_ts}")


if __name__ == "__main__":
    csv_path = download_latest_scrip_master_csv()
    print("Downloaded file at: ", csv_path)
    update_scrip_master_from_csv(csv_path)
