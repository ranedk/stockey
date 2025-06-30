# Download data from RBI website
import asyncio
import random
from datetime import datetime, timedelta

from playwright.async_api import async_playwright
import redis.asyncio as redis


REDIS_HOST = "localhost"
REDIS_PORT = 6379
CDP_ENDPOINT = (
    "http://localhost:9222"  # Chrome started with --remote-debugging-port=9222
)                            # Chromium or webkit won't work with NSE website
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
        await page.goto("https://data.rbi.org.in/DBIE/#/dbie/home")
        await page.wait_for_timeout(10000)

        await page.get_by_role('link', name='Indicators', exact=True).click()
        await page.wait_for_timeout(2000)

        await page.locator('a').filter(has_text='Financial Sector Indicators').click()
        await page.wait_for_timeout(2000)

        async with page.expect_popup() as popup_info:
            await page.get_by_text("Key Rates").click()

        rates_page = await popup_info.value
        await rates_page.wait_for_timeout(10000)
        frame = rates_page.frame(name="openDocChildFrame")

        await frame.get_by_role('button', name='Export (Ctrl+E)').click()
        await rates_page.wait_for_timeout(3000)

        async with rates_page.expect_download(timeout=15_000) as dl_info:
            await frame.get_by_role('button', name='Export', exact=True).click()

        download = await dl_info.value
        file_path = f"rbi_rates_{formatted_date}.xlsx"
        await download.save_as(file_path)

    finally:
        await page.close()
        await browser.close()


async def main() -> None:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    async with async_playwright() as p:
            date_obj = datetime.today()
            formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19

            await download_latest_rates(
                p, formatted_date, r
            )

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
