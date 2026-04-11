import tempfile
import time
from datetime import date, datetime

import numpy as np
import pandas as pd
import redis
import requests
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_dynamic_headers
from utils.date import daterange
from utils.sync import get_redis_client

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
DOWNLOADED = "fbilgec:downloaded"
rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


HEADERS = get_dynamic_headers()


def get_cookies():
    response = requests.get("https://www.fbil.org.in/", headers=HEADERS)
    return response.cookies.get_dict()


def try_parsing_date(text):
    for fmt in ("%d-%b-%Y", "%d %b, %Y", "%d/%b/%Y", "%d/%b/%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError("no valid date format found")


def parse_xls(xls_path, fdate):
    df = pd.read_excel(xls_path, sheet_name="G-Sec")
    trade_date = df.iloc[1, 2]
    try:
        if not isinstance(trade_date, (date, datetime)):
            trade_date = try_parsing_date(trade_date)
    except (ValueError, TypeError):
        trade_date = fdate

    expected_cols = [
        "isin",
        "coupon_pct",
        "maturity_date",
        "clean_price",
        "ytm_sa",
        "remark1",
        "remark2",
        "liquidity_signal",
    ]
    df_quote = pd.read_excel(xls_path, sheet_name="G-Sec", skiprows=5)
    df_quote = df_quote.dropna(axis=1, how="all")
    df_quote = df_quote.iloc[:, : len(expected_cols)]
    df_quote.columns = expected_cols[: df_quote.shape[1]]

    for col in expected_cols[df_quote.shape[1] :]:
        df_quote[col] = np.nan
    df_quote = df_quote[expected_cols]
    df_quote = df_quote.dropna(subset=["isin", "coupon_pct"])

    try:
        df_par = pd.read_excel(xls_path, sheet_name="Par Yield", skiprows=5)
    except ValueError:
        df_par = pd.read_excel(xls_path, sheet_name="Par-Yield", skiprows=5)
    df_par = df_par.iloc[:, :3]
    df_par.columns = [
        "tenor_years",
        "par_yield_sa",
        "par_yield_ann",
    ]
    df_par = df_par.dropna(axis=1, how="all")

    # Convert types
    df_quote["coupon_pct"] = pd.to_numeric(df_quote["coupon_pct"], errors="coerce")
    df_quote["clean_price"] = df_quote["clean_price"].astype(float)
    df_quote["ytm_sa"] = df_quote["ytm_sa"].astype(float)
    df_quote["maturity_date"] = pd.to_datetime(
        df_quote["maturity_date"], format="%d-%b-%Y"
    )
    df_quote = df_quote.dropna(subset=["coupon_pct"])
    df_quote = df_quote.drop_duplicates("isin", keep="last")

    df_par = df_par.dropna(axis=1, how="all")
    df_par = df_par.dropna(subset=["tenor_years", "par_yield_sa", "par_yield_ann"])
    df_par["tenor_years"] = df_par["tenor_years"].astype(float)
    df_par[["par_yield_sa", "par_yield_ann"]] = df_par[
        ["par_yield_sa", "par_yield_ann"]
    ].astype(float)

    df_quote.insert(0, "trade_date", trade_date)
    df_par.insert(0, "trade_date", trade_date)

    return df_quote, df_par


def download_gsec(fdate: date, cookies):
    if fdate.strftime("%a").lower() in ["sat", "sun"]:
        return

    formatted_date = fdate.strftime("%Y-%m-%d")
    print("GSec for ", formatted_date)

    params = {
        #'date': '2025-06-05', # format date
        "date": formatted_date
    }
    url = "https://www.fbil.org.in/wasdm/gsec/downloadPublished"
    s = requests.Session()
    response = s.get(
        url, params=params, headers=HEADERS, cookies=cookies, stream=True, timeout=30
    )
    if response.status_code != 200:
        print("Skipping (with error) GSec for ", formatted_date, fdate.strftime("%a"))
        return

    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as tmp:
        tmp.write(response.content)
        print(tmp.name)
        df_quote, df_par = parse_xls(tmp.name, fdate)

    df_quote = df_quote.rename(columns={"trade_date": "date"})
    df_par = df_par.rename(columns={"trade_date": "date"})

    df_quote["date"] = pd.to_datetime(df_quote["date"], format="%Y-%m-%d")
    df_par["date"] = pd.to_datetime(df_par["date"], format="%Y-%m-%d")

    upsert_to_db(
        df_quote,
        "fbil_gsec_quote",
        unique_keys=["date", "isin"],
        timescaledb_column="date",
    )
    upsert_to_db(
        df_par,
        "fbil_gsec_par",
        unique_keys=["date", "tenor_years"],
        timescaledb_column="date",
    )

    rop.set(DOWNLOADED, formatted_date)
    time.sleep(1)
    print("Downloaded GSec for ", formatted_date)


def download_all_gsec_data():
    cookies = get_cookies()
    today = datetime.now()
    from_date = rop.get(DOWNLOADED)
    if from_date:
        from_date = datetime.strptime(from_date, "%Y-%m-%d")
    else:
        from_date = datetime(2014, 1, 1)

    for fdate in daterange(from_date, today):
        download_gsec(fdate, cookies=cookies)


if __name__ == "__main__":
    # parse_xls("")
    # parse_xls("")
    download_all_gsec_data()
