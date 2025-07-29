# bhavcopy_downloader.py
import asyncio
import random
from datetime import datetime, timedelta

import redis.asyncio as redis
import pandas as pd
from environs import Env
from playwright.async_api import async_playwright
from utils.db import upsert_to_db


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:events"


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


async def dowload_events(
    playwright,
    formatted_date: str,
    rop: redis.Redis,
) -> bool:
    """
    Download the calendar csv file for all events
    Returns True on success, False on any exception.
    """
    browser = await playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()

    await page.goto("https://www.nseindia.com")
    await page.wait_for_timeout(get_random(1000, 3000))
    await page.goto('https://www.nseindia.com/companies-listing/corporate-filings-event-calendar')
    await page.wait_for_timeout(get_random(8000, 10000))


    download = await asyncio.gather(
        page.wait_for_event("download", timeout=5_000),
        page.get_by_role("link", name='csv Download (.csv)').click(),
    )
    download = download[0]

    file_path = f"calendar_{formatted_date}.csv"
    await download.save_as(file_path)

    parse_csv(file_path)

    print(f"✅ Success: {formatted_date}")
    await rop.sadd(REDIS_SET, formatted_date)

    await page.close()
    await browser.close()


def parse_csv(csv_file):
    df = pd.read_csv(csv_file)
    df.columns = ['symbol', 'company', 'purpose', 'details', 'date']
    df['date'] = pd.to_datetime(df['date'])
    df = df.drop(columns=["company"])
    df = df.drop_duplicates(subset=["date", "symbol", "purpose"], keep='first')
    upsert_to_db(df, "nseindia_events", unique_keys=["date", "symbol", "purpose"])
    return df


async def main() -> None:
    rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    async with async_playwright() as p:
        date_obj = datetime.today()
        formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19

        if await rop.sismember(REDIS_SET, formatted_date):
            return

        success = await dowload_events(
            p, formatted_date, rop
        )

    await rop.aclose()


if __name__ == "__main__":
    asyncio.run(main())
