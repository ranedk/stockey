import pandas as pd
from pandas.tseries.offsets import MonthBegin
from .tutils import build_feature_from_monthly

if __name__ == "__main__":
    # Example: WPI
    build_feature_from_monthly(
        source_table="public.eaindustry_wpi",
        feature_table="public.feature_wpi",
        date_col="date",
        value_cols={"value": "wpi_value"},       # or ["value1","value2"] or "value"
        unique_keys=["date", "cname"],           # dims inferred = ["cname"]
        # period_fn: leave None if source date is month-end/within month
        release_strategy=lambda period: period + MonthBegin(1) + pd.offsets.Day(13),  # 14th next month
        snap_direction="forward",
        start_period_dt=pd.Timestamp("2013-01-01"),
        start_if_empty=pd.Timestamp("2014-01-01"),
    )