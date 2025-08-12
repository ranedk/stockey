import pandas as pd
from pandas.io.sql import DatabaseError
from utils.db import sql_to_df
import pandas as pd
from typing import Callable, Mapping, Optional


def get_trading_days(start_dt: pd.Timestamp, end_dt: pd.Timestamp):
    """
    Get trading days between start_dt and end_dt.
    """
    sql = """
        SELECT date
        FROM public.dim_trading_days 
        WHERE date >= %(start)s AND date <= %(end)s
        ORDER BY date
    """
    df = sql_to_df(sql, params={"start": start_dt, "end": end_dt})
    return df.drop_duplicates().sort_values("date")


def get_max_date(table_name, date_column):
    """
    Get max date from table.
    """
    q = f'SELECT MAX("{date_column}") AS max_date FROM {table_name}'
    try:
        max_date = sql_to_df(q)["max_date"].iloc[0]
    except DatabaseError:
        return pd.Timestamp(2014, 1, 1)
    max_date = pd.to_datetime(max_date, errors="coerce")
    if pd.isna(max_date):
        return pd.Timestamp(2014, 1, 1)
    return max_date


def build_nearest_release_rows(
    target_table: str,
    *,
    target_date_col: str,
    source_table: str,
    source_date_col: str,
    release_mapper: Optional[Callable[[pd.Timestamp], pd.Timestamp]] = None,
    col_map: Optional[Mapping[str, str]] = None,
    today: Optional[pd.Timestamp] = None,
    source_sql: Optional[str] = None,
) -> pd.DataFrame:
    """
    Build a DataFrame by, for each trading day since the target table's max date,
    selecting the source row whose derived `release_date` is the latest on/before
    that day (as-of match).

    Workflow:
    1) Read MAX(target_date_col) from `target_table` -> `max_date`.
    2) Generate trading days in (max_date + 1 day … `today`) via `get_trading_days`.
    3) Read `source_table`, compute `release_date` from `source_date_col` using
    `release_mapper` (identity if None), then normalize to date.
    4) For each trading day, pick the nearest prior `release_date` (pd.merge_asof).
    Days with no prior release are dropped.
    5) Optionally rename columns using `col_map`.
    6) Return the assembled DataFrame.

    Args:
        conn: DB connection usable by pandas.read_sql_query.
        target_table: Table to read `target_date_col` from (for MAX date).
        target_date_col: Date column in `target_table`.
        source_table: Table containing source rows to match.
        source_date_col: Date column used to derive `release_date`.
        release_mapper: Optional callable(pd.Timestamp) -> pd.Timestamp applied
            elementwise to `source_date_col` to produce `release_date`.
            If None, `source_date_col` is used as-is.
            e.g. `lambda x: x + MonthEnd(0) + MonthBegin(0)`
        col_map: Optional dict for output column renames.
        get_trading_days: Callable(start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> list-like
            trading days (inclusive) used as as-of dates.
        today: Optional override for “now”; defaults to current date in UTC.
        source_sql: Optional SQL query to read source table.
    Returns:
        pd.DataFrame: One row per matched trading day with columns from `source_table`
        plus:
            - `asof_date`: the trading day
            - `release_date`: matched source release date
        (Both may be renamed via `col_map`.)

    Raises:
        ValueError: If no max date is found in `target_table.target_date_col`.
        KeyError: If `source_date_col` is missing from `source_table`.

    Notes:
        - All dates are coerced to Timestamp and normalized to midnight.
        - If `start_dt > today` or no trading days/source rows exist, returns empty DataFrame.

    Example:
        df = build_nearest_release_rows(
            conn,
            target_table="prices",
            target_date_col="trade_date",
            source_table="macro_wpi",
            source_date_col="period_end",
            release_mapper=lambda d: (pd.Timestamp(d) + pd.offsets.MonthBegin(1) + pd.offsets.Day(10)).normalize(),
            col_map={"asof_date": "trade_date"},
            get_trading_days=get_trading_days,
        )
    """

    # 1) max_date from target_table
    max_date = get_max_date(target_table, target_date_col)

    # 2) process_dates via get_trading_days
    if today is None:
        today = pd.Timestamp.now(tz="UTC").normalize()
    start_dt = (max_date.normalize() + pd.Timedelta(days=1))
    if start_dt > today:
        return pd.DataFrame()
    process_dates = get_trading_days(start_dt=start_dt, end_dt=today)
    if not process_dates:
        return pd.DataFrame()

    trading_dates = pd.DataFrame({"asof_date": pd.to_datetime(process_dates).normalize()}).sort_values("asof_date")

    # 3) read source_table and derive release_date via release_mapper
    if not source_sql:
        source_sql = f"SELECT * FROM {source_table}"
    df_source = sql_to_df(source_sql)
    if source_date_col not in df_source.columns:
        raise KeyError(f"{source_date_col} not in source table")

    df_source["release_date"] = release_mapper(df_source[source_date_col])
    df_source = df_source.sort_values("release_date")

    if df_source.empty:
        return pd.DataFrame()

    # 4) nearest prior row per process date (vectorized instead of manual loop)
    matched = pd.merge_asof(
        trading_dates.sort_values("asof_date"),
        df_source.sort_values("release_date"),
        left_on="asof_date",
        right_on="release_date",
        direction="backward",
        allow_exact_matches=True,
    )

    # Drop process dates with no prior release_date match
    matched = matched.dropna(subset=["release_date"])

    # 5) rename columns
    if col_map:
        matched = matched.rename(columns=col_map)

    # 6) return df (keep asof_date for traceability; drop if you don't want it)
    return matched.reset_index(drop=True)

