# bhavcopy_downloader.py
import random
from urllib.parse import urlencode
from typing import List
from datetime import datetime
import pandas as pd

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from data.dhanlive.dhan_db import get_nse_equity
from utils.db import upsert_to_db

env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:insider_deals"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def get_insider_deals(
    page,
    symbol: str,
    issuer: str,
    from_date: datetime,
    to_date: datetime,
) -> bool:
    """
    Automate NSE insider deals download from
    https://www.nseindia.com/companies-listing/corporate-filings-insider-trading
    """
    page.goto("https://www.nseindia.com/companies-listing/corporate-filings-insider-trading")
    page.wait_for_timeout(get_random(1000, 2000))

    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
        "symbol": symbol,
        "issuer": issuer,
    }

    url = f"https://www.nseindia.com/api/corporates-pit?{urlencode(params)}"

    data = page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return await res.json();
        }""",
        url,
    )

    df = pd.DataFrame(data["data"])
    rename_map = {
        "did": "disclosure_id",
        "symbol": "symbol",
        "acqName": "insider_name",
        "personCategory": "person_category",
        "tdpTransactionType": "transaction_type",
        "secAcq": "quantity",
        "secVal": "value_inr",
        "acqfromDt": "trade_date_from",
        "acqtoDt": "trade_date_to",
        "intimDt": "date",
        "acqMode": "acq_mode",
        "secType": "security_type",
        "derivativeType": "derivative_type",
        "exchange": "exchange",
        "befAcqSharesPer": "holding_pct_before",
        "afterAcqSharesPer": "holding_pct_after",
        "date": "reporting_date",
    }

    df = df.rename(columns=rename_map, errors="ignore")
    df = df[[
        "disclosure_id","symbol","insider_name","person_category","transaction_type",
        "quantity","value_inr","trade_date_from","trade_date_to","intimation_date",
        "acq_mode","security_type","derivative_type",
        "holding_pct_before","holding_pct_after"
    ]]

    for col in ["quantity","value_inr","holding_pct_before","holding_pct_after"]:
        df[col] = pd.to_numeric(df[col], errors="ignore")

    for col in ["trade_date_from","trade_date_to","intimation_date","reporting_date", "date"]:
        df[col] = pd.to_datetime(df[col])

    upsert_to_db(
        df,
        "nseindia_insider_deals",
        unique_keys=["disclosure_id", "date", "symbol", "insider_name", "transaction_type"],
        timescaledb_column="date"
    )
    rop.set(f"{REDIS_SET}:{symbol}", to_date.strftime('%Y-%m-%d'))

    return df


def sync_insider_deals(symbols: List[str]) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        page.goto("https://www.nseindia.com")
        page.wait_for_timeout(get_random(1000, 3000))

        for symbol in symbols:
            eqt = get_nse_equity(symbol)
            issuer = eqt.display_name

            from_date = rop.get(f"{REDIS_SET}:{symbol}")
            if from_date:
                from_date = datetime.strptime(from_date, "%Y-%m-%d")
            else:
                from_date = datetime(2014, 1, 1)

            to_date = datetime.today()
            get_insider_deals(
                page, symbol, issuer, from_date, to_date
            )
    rop.close()
    page.close()
    browser.close()


if __name__ == "__main__":
    symbols = ["SHAKTIPUMP", "HDFCBANK"]
    sync_insider_deals(symbols)
