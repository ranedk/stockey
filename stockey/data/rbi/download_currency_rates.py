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
        await page.wait_for_timeout(10000)

        js_code = f'$("#txtFromDate").val("01/01/2014");$("#txtToDate").val("${formatted_date}") ;'
        await page.evaluate(js_code)
        await page.wait_for_timeout(1000)

        await page.locator("input").filter(name="btnSubmit").click()
        await page.wait_for_selector("table.tablebg")


    finally:
        await page.close()
        await browser.close()


async def main() -> None:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    async with async_playwright() as p:
        date_obj = datetime.today()
        formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19

        await download_latest_rates(p, formatted_date, r)

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())

