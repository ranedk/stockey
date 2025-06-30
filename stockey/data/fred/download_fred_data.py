"""
us_macro_fred.py
----------------
Download & cache core U-S macro time-series from FRED.

Dependencies
------------
pip install pandas pandas-datareader pyarrow  # pyarrow for Parquet

Optional: set an env-var  FRED_API_KEY=your_key  (recommended by FRED,
but pandas-datareader still works anonymously for these series).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Dict

import pandas as pd
from pandas_datareader import data as pdr


# ------- 1.  Series map (FRED code -> friendly column name) ---------- #
FRED_SERIES: Dict[str, str] = {
    # Rates & risk-sentiment
    "DGS10":       "ust10y_yield",        # 10-year Treasury constant-maturity
    "EFFR":        "fedfunds_eff",        # Effective Fed-funds rate (daily)
    "VIXCLS":      "vix_close",           # CBOE VIX close
    # Growth & inflation
    "PAYEMS":      "nonfarm_payrolls",    # NFP (level, ‘000)
    "CPIAUCSL":    "cpi_headline",        # CPI SA
    "CPILFESL":    "cpi_core",            # CPI core SA
    #"NAPM":        "ism_mfg_pmi",         # ISM manufacturing PMI
    # USD & commodities
    "DTWEXBGS":    "broad_usd_index",     # Trade-weighted dollar (goods only)
    "DCOILWTICO":  "wti_crude_spot",      # WTI crude spot $/bbl
}

# ------- 2.  Generic downloader ------------------------------------- #
def fetch_fred_series(
    series: Dict[str, str] = FRED_SERIES,
    start: str | date = "2010-01-01",
    end: str | date = date.today(),
    resample: str | None = "D",          # "D"→daily, None→native freq.
) -> pd.DataFrame:
    """
    Returns a DataFrame with friendly column names, one column per series.
    Missing values (weekends, holidays) are forward-filled after resampling.
    """
    df = pdr.DataReader(list(series.keys()), "fred", start, end)
    df = df.rename(columns=series)

    if resample:
        df = (
            df.resample(resample)
            .last()
            .ffill()
        )

    return df


# ------- 3.  CLI / quick test --------------------------------------- #
if __name__ == "__main__":
    print("⏳  Downloading series from FRED …")
    macro_df = fetch_fred_series()
    from IPython import embed
    embed()


