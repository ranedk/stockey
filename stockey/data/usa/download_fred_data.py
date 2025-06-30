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

import requests
import pandas as pd
from pandas_datareader import data as pdr


FRED_SERIES: Dict[str, str] = {
    # Rates & risk-sentiment
    "DGS10":       "ust10y_yield",        # 10-year Treasury constant-maturity
    "EFFR":        "fedfunds_eff",        # Effective Fed-funds rate (daily)
    "VIXCLS":      "vix_close",           # CBOE VIX close

    # Growth & inflation
    "PAYEMS":      "nonfarm_payrolls",    # NFP (level, ‘000)
    "CPIAUCSL":    "cpi_headline",        # CPI SA
    "CPILFESL":    "cpi_core",            # CPI core SA

    # USD & commodities
    "DTWEXBGS":    "broad_usd_index",     # Trade-weighted dollar (goods only)
    "DCOILWTICO":  "wti_crude_spot",      # WTI crude spot $/bbl
}

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

def fetch_ism_manufacturing():

    headers = {
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9,uz;q=0.8',
        'origin': 'https://www.fxstreet.com',
        'priority': 'u=1, i',
        'referer': 'https://www.fxstreet.com/',
        'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'cross-site',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
    }

    response = requests.get(
        'https://calendar-api.fxsstatic.com/en/api/v1/events/2e1d69f3-8273-4096-b01b-8d2034d4fade/historical',
        headers=headers,
    )
    ism = response.json()
    df = pd.DataFrame.from_dict(ism)
    df = df.drop(columns=['id', 'ratioDeviation'])
    return df



# ------- 3.  CLI / quick test --------------------------------------- #
if __name__ == "__main__":
    macro_df = fetch_fred_series()
    ism = fetch_ism_manufacturing()
    from IPython import embed
    embed()


