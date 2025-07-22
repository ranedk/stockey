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

from datetime import date, timedelta
from typing import Dict

import requests
import pandas as pd
from pandas_datareader import data as pdr

from utils.db import upsert_to_db, table_has_date


__FRED_SERIES: Dict[str, str] = {
    # Rates & risk-sentiment
    "DGS10": "ust10y_yield",  # 10-year Treasury constant-maturity
    "EFFR": "fedfunds_eff",  # Effective Fed-funds rate (daily)
    "VIXCLS": "vix_close",  # CBOE VIX close
    # Growth & inflation
    "PAYEMS": "nonfarm_payrolls",  # NFP (level, ‘000)
    "CPIAUCSL": "cpi_headline",  # CPI SA
    "CPILFESL": "cpi_core",  # CPI core SA
    # USD & commodities
    "DTWEXBGS": "broad_usd_index",  # Trade-weighted dollar (goods only)
    "DCOILWTICO": "wti_crude_spot",  # WTI crude spot $/bbl
    "NGDPRNSAXDCINQ": "india_gdp",  # India GDP numbers
    "DEXINUS": "inr_usd_spot",  # INR USD Spot price
}


def fetch_fred_series(
    series: Dict[str, str] = __FRED_SERIES,
    resample: str | None = "D",  # "D"→daily, None→native freq.
) -> pd.DataFrame:
    """
    Returns a DataFrame with friendly column names, one column per series.
    Missing values (weekends, holidays) are forward-filled after resampling.
    """
    today = date.today()
    try:
        found, latest_date = table_has_date("macro_usa", "date", today)
    except:
        found = False
        latest_date = date(2014, 1, 1)

    start = latest_date - timedelta(
        days=10
    )  # Some values like inr_usd get updated later
    end = date.today()

    if found:
        # No need to query further if table has latest data
        return
    if latest_date >= today:
        # latest_date is today, no need to query further
        return

    df = pdr.DataReader(list(series.keys()), "fred", start, end)
    df = df.rename(columns=series)

    if resample:
        df = df.resample(resample).last().ffill()

    df = df.reset_index().rename(columns={"DATE": "date"})

    df_india_gdp = df[["date", "india_gdp"]]
    df_usa = df.drop(columns=["india_gdp"])

    upsert_to_db(
        df_usa,
        "macro_usa",
        unique_keys=["date"],
    )

    upsert_to_db(
        df_india_gdp,
        "macro_india_gdp",
        unique_keys=["date"],
    )


def fetch_ism_manufacturing():
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,uz;q=0.8",
        "origin": "https://www.fxstreet.com",
        "priority": "u=1, i",
        "referer": "https://www.fxstreet.com/",
        "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    }

    response = requests.get(
        "https://calendar-api.fxsstatic.com/en/api/v1/events/2e1d69f3-8273-4096-b01b-8d2034d4fade/historical",
        headers=headers,
        timeout=100,
    )
    ism = response.json()
    df = pd.DataFrame.from_dict(ism)
    df = df.drop(columns=["id", "ratioDeviation"]).rename(columns={"dateUtc": "date"})

    upsert_to_db(
        df,
        "macro_usa_ism",
        unique_keys=["date"],
    )
    return df


if __name__ == "__main__":
    fetch_fred_series()
    fetch_ism_manufacturing()
