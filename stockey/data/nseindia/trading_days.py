# trading_days.py
import random
import numpy as np
from datetime import datetime

import redis
import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright
from utils.db import upsert_to_db


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:trading_days"
rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


nse_product_info = {
    "CM": {
        "category": "Capital Market",
        "name": "Equities (Cash segment)",
        "definition": "All regular T+1 equity trading on the main board and SME board."
    },
    "CMOT": {
        "category": "Capital Market",
        "name": "Equities - Optional T+0",
        "definition": "Same-day (T+0) settlement trades in the cash market."
    },
    "MF": {
        "category": "Capital Market",
        "name": "Mutual Funds",
        "definition": "Orders routed through MFSS / NMF II platforms - subscriptions, redemptions, SIPs, etc."
    },
    "SLBS": {
        "category": "Capital Market",
        "name": "Securities Lending & Borrowing Scheme",
        "definition": "Stock-lending & borrowing transactions that enable short-selling and yield enhancement."
    },
    "FO": {
        "category": "Derivatives Market",
        "name": "Equity Derivatives (F&O)",
        "definition": "Index and stock futures and options traded on the NSE F&O segment."
    },
    "CD": {
        "category": "Derivatives Market",
        "name": "Currency Derivatives",
        "definition": "INR and cross-currency futures and options pairs such as USD-INR, EUR-INR."
    },
    "COM": {
        "category": "Derivatives Market",
        "name": "Commodity Derivatives",
        "definition": "Futures and options on bullion, energy, base metals and agricultural commodities."
    },
    "IRD": {
        "category": "Derivatives Market",
        "name": "Interest Rate Derivatives",
        "definition": "Futures on GOI bonds, MIBOR and other fixed-income rate-hedging instruments."
    },
    "CBM": {
        "category": "Debt Market",
        "name": "Corporate Bonds",
        "definition": "Secondary-market corporate bond trades, including listed and unlisted private placements."
    },
    "NDM": {
        "category": "Debt Market",
        "name": "New Debt Segment",
        "definition": "Order-matched trading of government and other debt securities on the wholesale debt platform."
    },
    "NTRP": {
        "category": "Debt Market",
        "name": "Negotiated Trade Reporting Platform",
        "definition": "Off-market debt deals reported for settlement under the new debt platform."
    }
}



def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def dowload_trading_days(
    playwright,
) -> bool:
    """
    Download the calendar csv file for all events
    https://www.nseindia.com/resources/exchange-communication-holidays
    """
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    page.goto("https://www.nseindia.com")
    page.wait_for_timeout(get_random(1000, 2000))
    page.goto('https://www.nseindia.com/resources/exchange-communication-holidays')
    page.wait_for_timeout(get_random(1000, 2000))

    url = 'https://www.nseindia.com/api/holiday-master?type=trading'
    data = page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return await res.json();
        }""",
        url,
    )

    full_df = pd.DataFrame()
    for k,v in data.items():
        df = pd.DataFrame(v)
        df['type'] = k
        df['type_name'] = nse_product_info[k]['name']
        df = df.rename(columns={
            'tradingDate': 'date',
            'weekDay': 'weekday',
            'description': 'holiday',
            'morning_session': 'morning_session',
            "evening_session": "evening_session", 
            "Sr_no": "sr_no"
        })
        df = df.drop(columns=["sr_no", "weekday"])
        df['date'] = pd.to_datetime(df['date'], format="%d-%b-%Y")
        df = df.replace(to_replace=[None], value=np.nan)
        full_df = pd.concat([full_df, df])

    full_df = full_df.reset_index(drop=True)
    full_df = full_df.drop_duplicates(subset=["date", "type"], keep='last')
    upsert_to_db(full_df, "nseindia_trading_days", unique_keys=["date", "type"], timescaledb_column="date")
    rop.set(REDIS_SET, datetime.today().strftime("%Y-%m-%d"))

    page.close()
    browser.close()


def main() -> None:

    with sync_playwright() as p:
        if rop.get(REDIS_SET):
            print("Last crawl on ", rop.get(REDIS_SET))

        dowload_trading_days(p)

    rop.close()


if __name__ == "__main__":
    main()
