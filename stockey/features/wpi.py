import pandas as pd
from pandas.tseries.offsets import MonthBegin, MonthEnd

from utils.db import upsert_to_db
from .tutils import build_nearest_release_rows


if __name__ == "__main__":
    df = build_nearest_release_rows(
        "feature_wpi",
        target_date_col="date",
        source_table="public.eaindustry_wpi",
        source_date_col="date",
        release_mapper=lambda period: period
        + MonthEnd(0)
        + MonthBegin(1)
        + pd.offsets.Day(13),
        col_map={"value": "wpi", "cname": "cname", "name": "name"},
    )
    upsert_to_db(df, "feature_wpi", unique_keys=["date", "cname"], timescaledb_column="date")
