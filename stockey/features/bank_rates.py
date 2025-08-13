import pandas as pd
from pandas.tseries.offsets import MonthBegin, MonthEnd

from utils.db import upsert_to_db
from .tutils import build_nearest_release_rows


if __name__ == "__main__":
    df = build_nearest_release_rows(
        target_table="features_bank_rates",
        target_date_col="date",
        source_table="public.rbi_bank_rates",
        source_date_col="date",
        release_mapper=None,
        source_sql="""
            SELECT date, bank_rate, repo_rate, reverse_repo_rate, sdf_rate, msf_rate, crr, slr
            FROM public.rbi_bank_rates
        """,
        stack_value_cols=("bank_rate","repo_rate","reverse_repo_rate", "sdf_rate","msf_rate","crr","slr"),
        source_unique_cols=(),
        pivot_wide=True,
    )
    upsert_to_db(
        df,
        "features_bank_rates",
        unique_keys=["asof_date"],
        timescaledb_column="asof_date",
    )