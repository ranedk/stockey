# bhavcopy_downloader.py
import random
from datetime import datetime

import redis
import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright
from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.sync import get_redis_client


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:events"


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def dowload_events(
    playwright,
    formatted_date: str,
    rop: redis.Redis,
) -> bool:
    """
    Download the calendar csv file for all events
    Returns True on success, False on any exception.
    """
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    page.goto("https://www.nseindia.com")
    page.wait_for_timeout(get_random(1000, 3000))
    page.goto('https://www.nseindia.com/companies-listing/corporate-filings-event-calendar')
    page.wait_for_timeout(get_random(8000, 10000))

    with page.expect_download(timeout=5_000) as download_info:
        page.get_by_role("link", name='csv Download (.csv)').click()
    download = download_info.value

    file_path = f"calendar_{formatted_date}.csv"
    download.save_as(file_path)

    parse_csv(file_path)

    print(f"✅ Success: {formatted_date}")
    rop.sadd(REDIS_SET, formatted_date)

    page.close()
    browser.close()


def parse_csv(csv_file):
    df = pd.read_csv(csv_file)
    df.columns = ['symbol', 'company', 'purpose', 'details', 'date']
    df['date'] = pd.to_datetime(df['date'])
    df = df.drop(columns=["company"])
    df = df.drop_duplicates(subset=["date", "symbol", "purpose"], keep='first')
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(df, "nseindia_events", unique_keys=["date", "symbol", "purpose"])
    return df


def main() -> None:
    rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))

    with sync_playwright() as p:
        date_obj = datetime.today()
        formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19

        dowload_events(
            p, formatted_date, rop
        )

    rop.close()


if __name__ == "__main__":
    main()
