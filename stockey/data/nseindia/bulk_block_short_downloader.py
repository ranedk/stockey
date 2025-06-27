# bulk_block_short_downloader.py
import asyncio
import random
from datetime import datetime, timedelta

from playwright.async_api import async_playwright
import redis.asyncio as redis


REDIS_HOST = "localhost"
REDIS_PORT = 6379
CDP_ENDPOINT = "http://localhost:9222"  # Chrome started with --remote-debugging-port=9222  # Chromium or webkit won't work with NSE website


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


async def download_data(
    playwright,
    dtype: str,
    from_date: datetime,
    to_date: datetime,
    r: redis.Redis,
) -> bool:
    """
    Automate NSE Block Bulk data download
    Returns True on success, False on any exception.
    """

    if dtype not in ["short_selling", "block_deals", "bulk_deals"]:
        raise ValueError(
            'Wrong value for dtype, can only be "short_selling", "block_deals", "bulk_deals"'
        )

    from_date_str = from_date.strftime("%d-%m-%Y")
    to_date_str = to_date.strftime("%d-%m-%Y")

    browser = await playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()

    try:
        await page.goto(
            "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"
        )
        await page.wait_for_timeout(get_random(1000, 2000))
        await page.locator("#segment_dropdown").select_option(dtype)
        await page.wait_for_timeout(get_random(2000, 3000))

        await page.get_by_role("link", name="Custom").click()
        await page.wait_for_timeout(get_random(2000, 3000))

        js_code = f'$(".startDate-block-deals.dtpicker.form-control").val("{from_date_str}");$(".endDate-block-deals.dtpicker.form-control").val("{to_date_str}");'
        await page.evaluate(js_code)
        await page.wait_for_timeout(1000)

        await page.get_by_role("button", name="GO").click()
        await page.wait_for_timeout(get_random(2000, 3000))

        download = await asyncio.gather(
            page.wait_for_event("download", timeout=10_000),
            page.get_by_role("link", name="csv Download (.csv)").click(),
        )
        # page.wait_for_event is first element
        download = download[0]

        file_path = f"{dtype}_{from_date_str}_{to_date_str}.csv"
        await download.save_as(file_path)

        print(f"✅ Success: {from_date_str} - {to_date_str}")
        await mark_dates_as_downloaded(r, dtype, from_date, to_date)
        return True

    except Exception as err:
        print(f"❌ Failed: {from_date_str} - {to_date_str} - {err}")
        return False

    finally:
        if r is not None:
            await page.close()
        await browser.close()


def daterange(start_date, end_date):
    for n in range(int((end_date - start_date).days) + 1):
        yield start_date + timedelta(n)


async def mark_dates_as_downloaded(r, dtype, start_date, end_date):
    await r.sadd(f"nse:{dtype}", *(dt.strftime("%Y-%m-%d") for dt in daterange(start_date, end_date)))


# Check missing dates
async def get_missing_dates(r, dtype, start_date, end_date):
    pipe = r.pipeline(transaction=False)
    for dt in daterange(start_date, end_date):
        pipe.sismember(f"nse:{dtype}", dt.strftime("%Y-%m-%d"))
    flags = await pipe.execute()
    return [d for d, have in zip(daterange(start_date, end_date), flags) if not have]


# Find consecutive ranges of missing dates
def find_consecutive_date_ranges(dates):
    if not dates:
        return []
    dates = sorted(dates)
    ranges = []
    start = dates[0]
    prev = dates[0]

    for current in dates[1:]:
        if (current - prev).days == 1:
            prev = current
        else:
            ranges.append((start, prev))
            start = current
            prev = current
    ranges.append((start, prev))
    return ranges


# Split range into 365 day blocks
def split_range_into_chunks(start_date, end_date, max_days=365):
    chunks = []
    current_start = start_date
    while current_start <= end_date:
        current_end = min(current_start + timedelta(days=max_days - 1), end_date)
        chunks.append((current_start, current_end))
        current_start = current_end + timedelta(days=1)
    return chunks


# Main function to get next block to download
async def get_next_download_block(r, dtype, global_start, global_end):
    missing = await get_missing_dates(r, dtype, global_start, global_end)
    ranges = find_consecutive_date_ranges(missing)
    for r_start, r_end in ranges:
        chunks = split_range_into_chunks(r_start, r_end)
        for chunk in chunks:
            return chunk
    return None


async def main() -> None:
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        global_start = datetime.strptime("2013-01-01", "%Y-%m-%d")
        global_end = datetime.strptime("2025-06-20", "%Y-%m-%d")

        async with async_playwright() as p:
            for dtype in ["block_deals", "bulk_deals", "short_selling"]:
                block = await get_next_download_block(r, dtype, global_start, global_end)
                if not block:
                    print(f"All data downloaded for {dtype} ✅")
                    continue
                print(
                    f"Download {dtype} {block[0].strftime('%d-%m-%Y')} and {block[1].strftime('%d-%m-%Y')}"
                )
                success = await download_data(
                    p, dtype, block[0], block[1], r
                )
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
