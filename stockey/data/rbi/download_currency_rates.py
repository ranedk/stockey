# Download data from RBI website
import asyncio
import random
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
from playwright.async_api import async_playwright
import redis.asyncio as redis


REDIS_HOST = "localhost"
REDIS_PORT = 6379
CDP_ENDPOINT = "http://localhost:9222"  # Chrome started with --remote-debugging-port=9222  # Chromium or webkit won't work with NSE website
REDIS_SET = "nse:downloaded"


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


async def download_latest_rates(
    playwright,
    formatted_date: str,
    r: redis.Redis,
) -> bool:
    """
    Automate RBI website's download. The site is made in SAP
    and is complete crap. So, playwright is only alternative
    """
    browser = await playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()

    try:
        await page.goto("https://www.rbi.org.in/scripts/ReferenceRateArchive.aspx")
        await page.wait_for_timeout(2000)

        js_code = f'$("#txtFromDate").val("01/07/2025");$("#txtToDate").val("{formatted_date}");'
        await page.evaluate(js_code)
        await page.wait_for_timeout(1000)

        await page.locator('input[name="btnSubmit"]').click()

        await page.wait_for_selector("div#example-one table.tablebg")
        await page.wait_for_timeout(2000)

        locator = page.locator("div#example-one table.tablebg").last
        table_html = await locator.evaluate("el => el.outerHTML")

    finally:
        await page.close()
        await browser.close()

    return table_html


def table_to_df(table_html):
    dfs = pd.read_html(table_html)
    df = dfs[0]
    df.columns = df.iloc[0]
    df = df.drop(df.index[0])
    df = df.reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], format="%d/%m/%Y")

    # Convert currency columns to float
    currency_cols = ["USD", "GBP", "EURO", "YEN"]
    for col in currency_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def main() -> None:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    async with async_playwright() as p:
        date_obj = datetime.today()
        formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19

        table_html = await download_latest_rates(p, formatted_date, r)

    await r.aclose()
    return table_html


if __name__ == "__main__":
    table_html = asyncio.run(main())
    df = table_to_df(table_html)
    from IPython import embed
    embed()


