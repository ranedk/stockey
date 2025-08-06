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
REDIS_SET = "nse:corporate_actions"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def get_corporate_actions(
    page,
    symbol: str,
    issuer: str,
    from_date: datetime,
    to_date: datetime,
) -> bool:
    """
    Automate NSE corporate actions download from
    https://www.nseindia.com/companies-listing/corporate-filings-actions
    """
    page.goto(
        "https://www.nseindia.com/companies-listing/corporate-filings-actions"
    )
    page.wait_for_timeout(get_random(1000, 2000))

    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
        "symbol": symbol,
        "issuer": issuer,
    }

    url = f"https://www.nseindia.com/api/corporates-corporateActions?{urlencode(params)}"

    data = page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return await res.json();
        }""",
        url,
    )

    df = pd.DataFrame(data)
    rename_map = {
        'symbol': 'symbol',
        'series': 'series',
        'faceVal': 'face_value',
        'subject': 'subject',
        'exDate': 'date',
        'recDate': 'record_date',
        'bcStartDate': 'start_date',
        'bcEndDate': 'end_date',
        'ndStartDate': 'nd_start_date',
        'ndEndDate': 'nd_end_date',
        'comp': 'company',
        'isin': 'isin',
        'caBroadcastDate': 'ca_broadcast_date'
    }

    df = df.rename(columns=rename_map)
    df = df.reindex(columns=list(rename_map.values()))

    for col in ["date", "record_date", "start_date", "end_date", "nd_start_date", "nd_end_date", "ca_broadcast_date"]:
        s = df[col].astype("string").str.strip().replace({"": pd.NA, "-": pd.NA, "None": pd.NA, "null": pd.NA})
        df[col] = pd.to_datetime(s, format="%d-%b-%Y")

    unique_keys = [
        "date",
        "symbol",
    ]

    df = df.sort_values("date").drop_duplicates(
        subset=unique_keys, keep="last"
    )
    upsert_to_db(
        df, "nseindia_corporate_actions", unique_keys=unique_keys, timescaledb_column="date"
    )
    rop.set(f"{REDIS_SET}:{symbol}", to_date.strftime("%Y-%m-%d"))
    return df


def sync_corporate_actions(symbols: List[str]) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        counter = 0
        for symbol in symbols:
            eqt = get_nse_equity(symbol)
            issuer = eqt.display_name

            from_date = rop.get(f"{REDIS_SET}:{symbol}")
            if from_date:
                from_date = datetime.strptime(from_date, "%Y-%m-%d")
            else:
                from_date = datetime(2014, 1, 1)

            if counter % 10 == 0:
                page.goto("https://www.nseindia.com")
                page.wait_for_timeout(get_random(1000, 3000))

            to_date = datetime.today()
            get_corporate_actions(page, symbol, issuer, from_date, to_date)
            counter += 1

        page.close()
        browser.close()
        rop.close()


if __name__ == "__main__":
    symbols = ["SHAKTIPUMP", "HDFCBANK"]
    sync_corporate_actions(symbols)
