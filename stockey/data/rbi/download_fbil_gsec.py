import tempfile
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import redis
import requests
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_dynamic_headers

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
DOWNLOADED = "fbilgec:downloaded"
FAILED = "fbilgec:failed"
rdb = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


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
    if rdb.sismember(DOWNLOADED, formatted_date) or rdb.sismember(
        FAILED, formatted_date
    ):
        print("Already downloaded or failed")
        return

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
        rdb.sadd(FAILED, formatted_date)
        return

    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as tmp:
        tmp.write(response.content)
        print(tmp.name)
        df_quote, df_par = parse_xls(tmp.name, fdate)

    upsert_to_db(df_quote, "fbil_gsec_quote", unique_keys=["trade_date", "isin"])
    upsert_to_db(df_par, "fbil_gsec_par", unique_keys=["trade_date", "tenor_years"])

    rdb.sadd(DOWNLOADED, formatted_date)
    time.sleep(0.5)
    print("Downloaded GSec for ", formatted_date)


def download_all_gsec_data():
    cookies = get_cookies()
    today = date.today()
    n = 1
    while n <= 365 * 12:
        fdate = today - timedelta(days=n)
        download_gsec(fdate, cookies=cookies)
        n += 1


if __name__ == "__main__":
    # parse_xls("")
    # parse_xls("")
    download_all_gsec_data()
