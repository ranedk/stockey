#!/usr/bin/env python3
"""
update_dhan_master_pg.py
Usage:
    python update_dhan_master_pg.py
"""

from __future__ import annotations

import csv
import io
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from environs import Env

from utils.db import db_session

env = Env()
env.read_env()

# columns whose *value* we treat as version-defining
TRACKED_COLS = [
    "series",
    "lot_size",
    "sm_expiry_date",
    "strike_price",
    "option_type",
    "instrument",
    "expiry_flag",
]

DDL = """
CREATE TABLE IF NOT EXISTS master_dhan_instruments (
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
    lot_size                DOUBLE PRECISION,
    sm_expiry_date          DATE,
    strike_price            DOUBLE PRECISION,
    option_type             VARCHAR,
    tick_size               DOUBLE PRECISION,
    expiry_flag             VARCHAR,
    bracket_flag            VARCHAR,
    cover_flag              VARCHAR,
    asm_gsm_flag            VARCHAR,
    asm_gsm_category        VARCHAR,
    buy_sell_indicator      VARCHAR,
    buy_co_min_margin_per   DOUBLE PRECISION,
    sell_co_min_margin_per  DOUBLE PRECISION,
    buy_co_sl_range_max_perc DOUBLE PRECISION,
    sell_co_sl_range_max_perc DOUBLE PRECISION,
    buy_co_sl_range_min_perc DOUBLE PRECISION,
    sell_co_sl_range_min_perc DOUBLE PRECISION,
    buy_bo_min_margin_per   DOUBLE PRECISION,
    sell_bo_min_margin_per  DOUBLE PRECISION,
    buy_bo_sl_range_max_perc DOUBLE PRECISION,
    sell_bo_sl_range_max_perc DOUBLE PRECISION,
    buy_bo_sl_range_min_perc DOUBLE PRECISION,
    sell_bo_sl_min_range    DOUBLE PRECISION,
    buy_bo_profit_range_max_perc DOUBLE PRECISION,
    sell_bo_profit_range_max_perc DOUBLE PRECISION,
    buy_bo_profit_range_min_perc DOUBLE PRECISION,
    sell_bo_profit_range_min_perc DOUBLE PRECISION,
    mtf_leverage            DOUBLE PRECISION,
    sm_upper_limit          DOUBLE PRECISION,
    sm_lower_limit          DOUBLE PRECISION,
    sm_freeze_qty           DOUBLE PRECISION,

    valid_from              TIMESTAMP NOT NULL,
    valid_to                TIMESTAMP,
    load_ts                 TIMESTAMP NOT NULL,

    PRIMARY KEY (security_id, segment, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_master_dhan_active
    ON master_dhan_instruments (security_id, segment, valid_to);
ALTER TABLE master_dhan_instruments
    ADD COLUMN IF NOT EXISTS sm_upper_limit DOUBLE PRECISION;
ALTER TABLE master_dhan_instruments
    ADD COLUMN IF NOT EXISTS sm_lower_limit DOUBLE PRECISION;
ALTER TABLE master_dhan_instruments
    ADD COLUMN IF NOT EXISTS sm_freeze_qty DOUBLE PRECISION;
"""


def download_master_csv(timeout: int = 30) -> Path:
    url = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
    except Exception as exc:
        sys.exit(f"[ERROR] download failed: {exc}")

    if len(r.content) < 10_000:  # sanity check
        sys.exit("[ERROR] response too small, aborting")

    tmp.write(r.content)
    tmp.close()
    return Path(tmp.name)


def load_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        csv_path,
        low_memory=False,
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
            "EXPIRY_FLAG": "string",
            "BRACKET_FLAG": "string",
            "COVER_FLAG": "string",
            "ASM_GSM_FLAG": "string",
            "ASM_GSM_CATEGORY": "string",
            "BUY_SELL_INDICATOR": "string",
            "BUY_CO_MIN_MARGIN_PER": "float64",
            "BUY_CO_SL_RANGE_MAX_PERC": "float64",
            "BUY_CO_SL_RANGE_MIN_PERC": "float64",
            "BUY_BO_MIN_MARGIN_PER": "float64",
            "BUY_BO_PROFIT_RANGE_MAX_PERC": "float64",
            "BUY_BO_PROFIT_RANGE_MIN_PERC": "float64",
            "MTF_LEVERAGE": "float64",
            "SM_UPPER_LIMIT": "float64",
            "SM_LOWER_LIMIT": "float64",
            "SM_FREEZE_QTY": "float64",
        },
    )
    df["SM_EXPIRY_DATE"] = pd.to_datetime(df["SM_EXPIRY_DATE"], errors="coerce")
    df.columns = [c.lower() for c in df.columns]
    # ‼️ DROP phantom cols created by trailing delimiters
    df = df.loc[:, ~df.columns.str.contains(r"^unnamed", case=False)]
    return df.where(pd.notnull(df), None)  # NaN → None


def make_update_sql(tracked_cols: list[str]) -> str:
    diff_cond = " OR ".join(f"dst.{c} IS DISTINCT FROM st.{c}" for c in tracked_cols)
    return f"""
    /* close current version where anything changed */
    UPDATE master_dhan_instruments dst
    SET    valid_to = %(load_ts)s
    FROM   _stage st
    WHERE  dst.security_id = st.security_id
      AND  dst.segment = st.segment
      AND  dst.valid_to IS NULL
      AND  ({diff_cond});
    """


def postgres_type_for_series(series: pd.Series) -> str:
    dtype = series.dtype
    if pd.api.types.is_integer_dtype(dtype):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dtype):
        return "DOUBLE PRECISION"
    if pd.api.types.is_bool_dtype(dtype):
        return "BOOLEAN"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "TIMESTAMP"
    return "VARCHAR"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def make_insert_sql(columns: list[str]) -> str:
    target_columns = [*columns, "valid_from", "valid_to", "load_ts"]
    quoted_targets = ", ".join(quote_identifier(column) for column in target_columns)
    stage_values = ", ".join(f"st.{quote_identifier(column)}" for column in columns)
    return f"""
    /* insert new or changed version */
    INSERT INTO master_dhan_instruments ({quoted_targets})
    SELECT {stage_values},
           %(load_ts)s AS valid_from,
           NULL        AS valid_to,
           %(load_ts)s AS load_ts
    FROM   _stage st
    LEFT   JOIN master_dhan_instruments m
           ON m.security_id = st.security_id
           AND m.segment = st.segment
           AND m.valid_to IS NULL
    WHERE  m.security_id IS NULL;
    """


def sync_master_schema(cur: Any, df: pd.DataFrame) -> None:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'master_dhan_instruments'
        """
    )
    existing_cols = {row[0] for row in cur.fetchall()}
    for column in df.columns:
        if column in existing_cols:
            continue
        sql_type = postgres_type_for_series(df[column])
        cur.execute(f"ALTER TABLE master_dhan_instruments ADD COLUMN {quote_identifier(column)} {sql_type}")
        existing_cols.add(column)


def update_database(df: pd.DataFrame) -> None:
    load_ts = datetime.now(timezone.utc)

    with db_session() as (conn, cur):
        # schema
        cur.execute(DDL)
        sync_master_schema(cur, df)
        conn.commit()

        # temp staging table (structure cloned, dropped on COMMIT)
        cur.execute(
            """
            CREATE TEMP TABLE _stage
            ON COMMIT DROP
            AS SELECT * FROM master_dhan_instruments WHERE false;

            /* remove the audit/version columns */
            ALTER TABLE _stage
                DROP COLUMN valid_from,
                DROP COLUMN valid_to,
                DROP COLUMN load_ts;
        """
        )

        buf = io.StringIO()
        df.to_csv(
            buf,
            sep="\t",
            header=False,
            index=False,
            na_rep="\\N",  # <- NULL _must_ be \N for COPY text mode
            quoting=csv.QUOTE_NONE,
        )
        buf.seek(0)
        cur.copy_from(
            buf,
            "_stage",
            sep="\t",
            null="\\N",
            columns=df.columns.to_list(),
        )

        # versioning
        cur.execute("BEGIN;")
        cur.execute(make_update_sql(TRACKED_COLS), {"load_ts": load_ts})
        cur.execute(make_insert_sql(df.columns.to_list()), {"load_ts": load_ts})
        cur.execute("COMMIT;")

    print(
        f"{len(df):,} rows processed  |  load_ts = {load_ts.isoformat(timespec='seconds')}"
    )


if __name__ == "__main__":
    csv_path = download_master_csv()
    print(f"downloaded → {csv_path}")
    df_master = load_csv(csv_path)
    update_database(df_master)
