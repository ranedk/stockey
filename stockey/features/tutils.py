import pandas as pd
from pandas.tseries.offsets import MonthBegin, MonthEnd
from typing import Callable, Mapping, Sequence, Union, Optional
from utils.db import upsert_to_db, get_max_date
from utils.db import sql_to_df


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


def read_crawled_table(table_name, date_column, start_period_dt):
    """
    Read table from database with date_column >= start_period_dt
    """
    params = {"start": start_period_dt.tz_convert("UTC")}

    sql = f"""
        SELECT *
        FROM {table_name}
        WHERE {date_column} >= %(start)s
        ORDER BY {date_column}, cname
    """
    df = sql_to_df(sql, params=params)
    df[date_column] = pd.to_datetime(df[date_column])
    return df


def _value_map(value_cols: Union[str, Sequence[str], Mapping[str, str]]) -> dict:
    if isinstance(value_cols, str):
        return {value_cols: value_cols}
    if isinstance(value_cols, Mapping):
        return dict(value_cols)
    return {c: c for c in value_cols}


def build_feature_from_monthly(
    source_table: str,
    feature_table: str,
    *,
    # source schema
    date_col: str = "date",  # source period or date
    value_cols: Union[str, Sequence[str], Mapping[str, str]] = "value",
    # keys/output
    unique_keys: Sequence[str] = (
        "date",
    ),  # must include "date"; others = dims (e.g., "cname")
    # period/release logic
    period_fn: Optional[
        Callable[[pd.Series], pd.Series]
    ] = None,  # default: month-end of source date
    release_strategy: Optional[
        Callable[[pd.Series], pd.Series]
    ] = None,  # default: next-month 14th
    snap_direction: str = "forward",  # snap release to trading day: "forward" or "backward"
    # ranges
    start_period_dt: pd.Timestamp = pd.Timestamp("2013-01-01"),
    start_if_empty: pd.Timestamp = pd.Timestamp("2014-01-01"),
):
    """Build daily features from a monthly source by mapping each monthly period to its
    publish date (via a strategy), snapping that date to a trading calendar, and
    backfilling onto trading days. Writes results incrementally to `feature_table`
    via `upsert_to_db`.

    Algorithm:

    1. Determine write window: [max(feature_table.date)+1d, today] else [start_if_empty, today].
    2. Load trading days for the window; load monthly source rows since `start_period_dt`.
    3. Compute monthly `period` (default: month-end of `date_col`, or via `period_fn`).
    4. Compute `release_candidate = release_strategy(period)` (default: 14th of next month).
    5. Snap to trading day per `snap_direction` ("forward" or "backward") → `release_date`.
    6. Drop unreleased future periods (`release_date` > today).
    7. Deduplicate to one row per (`period`, dims), where dims = `unique_keys` − {"date"}.
    8. For each trading day x dims, backward asof-join with releases to carry last known values.
    9. Rename values per `value_cols` mapping; keep names if list/str is provided.
    10. Upsert columns: `date`, dims, renamed values, `period`, `release_date`.

    Args:
    source_table (str):
    Qualified name of the monthly source table to read.
    feature_table (str):
    Qualified name of the destination feature table to upsert into.
    date_col (str, optional):
    Column in `source_table` representing the source row date. Defaults to "date".
    value_cols (str | Sequence[str] | Mapping[str, str], optional):
    Value columns to carry into the feature table.
     - str: single column, kept as-is.
     - Sequence[str]: multiple columns, kept as-is.
     - Mapping[str, str]: source→destination rename map. Defaults to "value".
    unique_keys (Sequence[str], optional):
    Composite key for the feature table. MUST include "date".
    Keys other than "date" are treated as dimension columns (dims). Defaults to ("date",).
    period_fn (Callable[[pd.Series], pd.Series] | None, optional):
    Function mapping the source `date_col` Series → monthly period Series (Timestamp).
    Defaults to coercing `date_col` to month-end.
    release_strategy (Callable[[pd.Series], pd.Series] | None, optional):
    Function mapping `period` → publish-date candidates.
    Defaults to 14th of next month: `period + MonthBegin(1) + 13 days`.
    snap_direction (str, optional):
    "forward" to snap to the next trading day on/after the candidate, or "backward"
    to snap to the prior trading day. Defaults to "forward".
    start_period_dt (pd.Timestamp, optional):
    Earliest source date to read from the monthly table. Defaults to 2013-01-01.
    start_if_empty (pd.Timestamp, optional):
    Start of the trading window if `feature_table` has no rows. Defaults to 2014-01-01.

    Behavior:

    * Incremental: only writes trading days after the last date present in `feature_table`.
    * Uses `get_trading_days` (must return DataFrame with `date`) and `read_crawled_table`
    (must accept `date_column` and `start_period_dt`).
    * Upserts via `upsert_to_db(out, feature_table, unique_keys)`.

    Raises:
    ValueError: If "date" is not in `unique_keys`, or if any specified `value_cols`
    are missing from the source data.

    Returns:
    None. Prints the number of rows upserted.

    Example:
    build_feature_from_monthly(
    source_table="public.eaindustry_wpi",
    feature_table="public.feature_wpi",
    date_col="date",
    value_cols={"value": "wpi_value"},
    unique_keys=["date", "cname"],
    release_strategy=lambda period: period + MonthBegin(1) + pd.offsets.Day(13),
    snap_direction="forward",
    start_period_dt=pd.Timestamp("2013-01-01"),
    start_if_empty=pd.Timestamp("2014-01-01"),
    )
    """

    now = pd.Timestamp.now().normalize()
    if "date" not in unique_keys:
        raise ValueError("unique_keys must include 'date'.")

    dims = [k for k in unique_keys if k != "date"]
    vmap = _value_map(value_cols)

    # incremental window
    last_dt = get_max_date(feature_table)
    start_dt = (
        (last_dt + pd.Timedelta(days=1)) if last_dt is not None else start_if_empty
    )

    # trading days to write for
    tdays = get_trading_days(start_dt, now)
    if tdays.empty:
        print("No new trading days to process.")
        return

    # source monthly data
    df = read_crawled_table(
        source_table, date_column=date_col, start_period_dt=start_period_dt
    )
    if df.empty:
        print("No data rows found in source.")
        return

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col]).dt.normalize()

    # compute monthly period
    if period_fn is None:
        df["period"] = df[date_col] + MonthEnd(0)
    else:
        df["period"] = pd.to_datetime(period_fn(df[date_col])).dt.normalize()

    # de-dup to one row per (period, dims)
    dedupe_keys = ["period"] + dims
    df = df.sort_values([date_col] + list(vmap.keys())).drop_duplicates(
        subset=dedupe_keys, keep="last"
    )

    # release candidate via strategy
    if release_strategy is None:
        # default: 14th of next month
        df["release_candidate"] = (
            df["period"] + MonthBegin(1) + pd.offsets.Day(13)
        ).dt.normalize()
    else:
        df["release_candidate"] = pd.to_datetime(
            release_strategy(df["period"])
        ).dt.normalize()

    # trading calendar to snap release dates
    cal_start = min(start_period_dt, pd.Timestamp("2013-01-01"))
    cal = get_trading_days(cal_start, now).rename(columns={"date": "release_aligned"})[
        ["release_aligned"]
    ]
    cal = cal.sort_values("release_aligned").reset_index(drop=True)

    left_rc = (
        df[["release_candidate"]]
        .sort_values("release_candidate")
        .reset_index(drop=True)
    )
    snapped = pd.merge_asof(
        left_rc,
        cal,
        left_on="release_candidate",
        right_on="release_aligned",
        direction="forward" if snap_direction == "forward" else "backward",
    )
    df = df.sort_values("release_candidate").reset_index(drop=True)
    df["release_date"] = snapped["release_aligned"]

    # drop unreleased future periods
    df = df[df["release_date"].notna() & (df["release_date"] <= now)].copy()

    # prepare right side (rename value cols)
    df = df.rename(columns=vmap)
    renamed_values = list(vmap.values())

    right_cols = ["release_date", "period"] + renamed_values + dims
    right = df[right_cols].sort_values(dims + ["release_date"]).reset_index(drop=True)

    # LEFT: trading days × dims (Cartesian if dims exist)
    if dims:
        dims_df = df[dims].drop_duplicates().reset_index(drop=True)
        left = (
            tdays.assign(_k=1).merge(dims_df.assign(_k=1), on="_k").drop(columns="_k")
        )
        left = left.sort_values(dims + ["date"]).reset_index(drop=True)
    else:
        left = tdays.sort_values("date").reset_index(drop=True)

    # asof: last known release at each trading day
    merged = pd.merge_asof(
        left,
        right,
        left_on="date",
        right_on="release_date",
        by=dims if dims else None,
        direction="backward",
    )
    merged = merged[merged["release_date"].notna()].copy()

    # final output
    out_cols = ["date"] + dims + renamed_values + ["period", "release_date"]
    out = (
        merged[out_cols]
        .sort_values(dims + ["date"] if dims else ["date"])
        .reset_index(drop=True)
    )

    # upsert
    upsert_to_db(out, feature_table, unique_keys=list(unique_keys))
    print(f"Upserted {len(out)} rows into {feature_table}.")
