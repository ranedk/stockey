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
REDIS_SET = "nse:earnings_events"

rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def get_earnings_events(
    page,
    symbol: str,
    issuer: str,
    from_date: datetime,
    to_date: datetime,
) -> bool:
    """
    Automate NSE earnings events download from
    https://www.nseindia.com/companies-listing/corporate-filings-financial-results
    """
    page.goto(
        "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
    )
    page.wait_for_timeout(get_random(1000, 2000))

    for period in ["Quarterly", "Half-Yearly", "Annual"]:
        params = {
            "index": "equities",
            "from_date": from_date.strftime("%d-%m-%Y"),
            "to_date": to_date.strftime("%d-%m-%Y"),
            "symbol": symbol,
            "issuer": issuer,
            "period": period,
        }

        url = f"https://www.nseindia.com/api/corporates-financial-results?{urlencode(params)}"

        data = page.evaluate(
            """async (url) => {
                const res = await fetch(url, { credentials: 'same-origin' });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return await res.json();
            }""",
            url,
        )

        if not data:
            print("No data found for", symbol, period, from_date, to_date)
            continue

        df = pd.DataFrame(data)
        rename_map = {
            "symbol": "symbol",
            "companyName": "company",
            "industry": "industry",
            "audited": "audited",
            "cumulative": "cumulative",
            "period": "period",
            "financialYear": "financial_year",
            "seqNumber": "seq_number",
            "bank": "bank",
            "fromDate": "from_date",
            "toDate": "to_date",
            "exchdisstime": "reporting_date",
            "consolidated": "consolidated",
            "isin": "isin",
        }

        df = df.rename(columns=rename_map)
        df = df.reindex(columns=list(rename_map.values()))

        for col in ["from_date", "to_date"]:
            df[col] = pd.to_datetime(df[col], format="%d-%b-%Y")

        df["reporting_date"] = pd.to_datetime(
            df["reporting_date"], format="%d-%b-%Y %H:%M:%S"
        )
        df["date"] = df["to_date"]
        unique_keys = ["date", "symbol", "reporting_date", "period"]

        df = df.sort_values("reporting_date").drop_duplicates(
            subset=unique_keys, keep="last"
        )
        upsert_to_db(
            df,
            "nseindia_earnings_events",
            unique_keys=unique_keys,
            timescaledb_column="date",
        )
        page.wait_for_timeout(get_random(1000, 2000))

    rop.set(f"{REDIS_SET}:{symbol}", to_date.strftime("%Y-%m-%d"))


def sync_earnings_events(symbols: List[str]) -> None:
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
            get_earnings_events(page, symbol, issuer, from_date, to_date)
            counter += 1

        page.close()
        browser.close()
        rop.close()


if __name__ == "__main__":
    symbols = ["SHAKTIPUMP", "HDFCBANK"]
    sync_earnings_events(symbols)
