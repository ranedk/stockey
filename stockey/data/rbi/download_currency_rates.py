# Download data from RBI website
from io import StringIO
import asyncio
import random
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

import pandas as pd
from playwright.async_api import async_playwright
import redis.asyncio as redis
from utils.db import upsert_to_db


REDIS_HOST = "localhost"
REDIS_PORT = 6379
CDP_ENDPOINT = "http://localhost:9222"  # Chrome started with --remote-debugging-port=9222  # Chromium or webkit won't work with NSE website
REDIS_SET = "rbi:currency"


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


async def download_latest_rates(
    playwright,
    from_date: date,
    to_date: date,
    rop: redis.Redis,
) -> bool:
    """
    Automate RBI website's download. The site is made in SAP
    and is complete crap. So, playwright is only alternative
    """
    browser = await playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    print(f"Downloading {from_date} to {to_date}")

    try:
        await page.goto("https://www.rbi.org.in/scripts/ReferenceRateArchive.aspx")
        await page.wait_for_timeout(2000)

        js_code = f"""
            $("#txtFromDate").val("{from_date.strftime("%d/%m/%Y")}");
            $("#txtToDate").val("{to_date.strftime("%d/%m/%Y")}");
        """
        await page.evaluate(js_code)
        await page.wait_for_timeout(1000)

        await page.locator('input[name="btnSubmit"]').click()

        await page.wait_for_selector("div#example-one table.tablebg", timeout=120_000)
        await page.wait_for_timeout(2000)

        locator = page.locator("div#example-one table.tablebg").last
        table_html = await locator.evaluate("el => el.outerHTML")
        df = table_to_df(table_html)
        upsert_to_db(df, "rbi_currency_rates", unique_keys=["date"])

        for i in range((to_date - from_date).days):
            await rop.sadd(REDIS_SET, (from_date + timedelta(days=i)).strftime("%Y-%m-%d"))

    finally:
        await page.close()
        await browser.close()


def table_to_df(table_html):
    dfs = pd.read_html(StringIO(str(table_html)))
    df = dfs[0]
    df.columns = df.iloc[0]
    df = df.drop(df.index[0])
    df = df.reset_index(drop=True)
    df["date"] = pd.to_datetime(df["Date"], format="%d/%m/%Y")

    # Convert currency columns to float
    currency_cols = ["USD", "GBP", "EURO", "YEN"]
    for col in currency_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df

WEEKEND = {5, 6} 
def next_monday(d: date) -> date:
    """Move forward to Monday if d is Sat/Sun."""
    return d + timedelta(days=(7 - d.weekday())) if d.weekday() in WEEKEND else d

def prev_friday(d: date) -> date:
    """Move back to Friday if d is Sat/Sun."""
    return d - timedelta(days=(d.weekday() - 4)) if d.weekday() in WEEKEND else d


async def main() -> None:
    rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    async with async_playwright() as p:
        start_date  = date(2014, 1, 1)
        final_date  = date.today()

        cursor = start_date
        while cursor <= final_date:
            if cursor.weekday() in WEEKEND:              # skip weekends completely
                cursor += timedelta(days=1)
                continue
            if not await rop.sismember(REDIS_SET, cursor.isoformat()):
                break                                    # this is the first gap
            cursor += timedelta(days=1)

        while cursor <= final_date:
            # plan a one-year window
            tentative_end = cursor + relativedelta(years=1) - timedelta(days=1)
            chunk_end     = min(tentative_end, final_date)

            # make sure both edges are weekdays
            cursor    = next_monday(cursor)
            chunk_end = prev_friday(chunk_end)

            if cursor > chunk_end:          # happens if we’re at year-end weekend
                cursor += timedelta(days=1)
                continue                    # and restart the loop

            # download and mark the whole span
            await download_latest_rates(p, cursor, chunk_end, rop)

            cursor = chunk_end + timedelta(days=1)

            # fast-skip anything already present or on weekends
            while cursor <= final_date and (
                cursor.weekday() in WEEKEND or
                await rop.sismember(REDIS_SET, cursor.isoformat())
            ):
                cursor += timedelta(days=1)

    await rop.aclose()


if __name__ == "__main__":
    asyncio.run(main())


