# bulk_block_short_downloader.py
import random
from datetime import datetime, timedelta

import redis
from playwright.sync_api import sync_playwright
from utils import store

REDIS_HOST = "localhost"
REDIS_PORT = 6379
CDP_ENDPOINT = "http://localhost:9222"  # Chrome started with --remote-debugging-port=9222  # Chromium or webkit won't work with NSE website


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def download_data(
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

    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    try:
        page.goto("https://www.nseindia.com/report-detail/display-bulk-and-block-deals")
        page.wait_for_timeout(get_random(1000, 2000))
        page.locator("#segment_dropdown").select_option(dtype)
        page.wait_for_timeout(get_random(2000, 3000))

        page.get_by_role("link", name="Custom").click()
        page.wait_for_timeout(get_random(2000, 3000))

        js_code = f'$(".startDate-block-deals.dtpicker.form-control").val("{from_date_str}");$(".endDate-block-deals.dtpicker.form-control").val("{to_date_str}");'
        page.evaluate(js_code)
        page.wait_for_timeout(1000)

        page.get_by_role("button", name="GO").click()
        page.wait_for_timeout(get_random(2000, 3000))

        with page.expect_download(timeout=10_000) as download_info:
            page.get_by_role("link", name="csv Download (.csv)").click()
        download = download_info.value

        file_path = f"{dtype}_{from_date_str}_{to_date_str}.csv"
        download.save_as(file_path)

        store.save_file(file_path, prefix="nsedeals")

        print(f"✅ Success: {from_date_str} - {to_date_str}")
        mark_dates_as_downloaded(r, dtype, from_date, to_date)
        return True

    except Exception as err:
        print(f"❌ Failed: {from_date_str} - {to_date_str} - {err}")
        return False

    finally:
        page.close()
        browser.close()


def daterange(start_date, end_date):
    for n in range(int((end_date - start_date).days) + 1):
        yield start_date + timedelta(n)


def mark_dates_as_downloaded(r, dtype, start_date, end_date):
    r.sadd(
        f"nse:{dtype}",
        *(dt.strftime("%Y-%m-%d") for dt in daterange(start_date, end_date)),
    )


# Check missing dates
def get_missing_dates(r, dtype, start_date, end_date):
    pipe = r.pipeline(transaction=False)
    for dt in daterange(start_date, end_date):
        pipe.sismember(f"nse:{dtype}", dt.strftime("%Y-%m-%d"))
    flags = pipe.execute()
    return [d for d, have in zip(daterange(start_date, end_date), flags) if not have]


def get_next_download_block(r, dtype, g_start, g_end):
    # All missing calendar days for this dtype
    missing = get_missing_dates(r, dtype, g_start, g_end)
    if not missing:
        return None  # nothing left to fetch

    # newest-first
    missing.sort(reverse=True)

    end = missing[0]  # newest day in the gap
    start = end
    prev = end
    count = 1

    for dt in missing[1:]:
        if (prev - dt).days == 1 and count < 365:  # still consecutive
            start = dt
            prev = dt  # <-- advance the reference!
            count += 1
        else:
            break  # gap or 365-day limit reached

    return start, end  # inclusive


def main() -> None:
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        global_start = datetime.strptime("2013-01-01", "%Y-%m-%d")
        global_end = datetime.today()

        with sync_playwright() as p:
            for dtype in ["block_deals", "bulk_deals", "short_selling"]:
                while True:
                    block = get_next_download_block(
                        r, dtype, global_start, global_end
                    )
                    if not block:
                        print(f"All data downloaded for {dtype} ✅")
                        break
                    print(
                        f"Download {dtype} {block[0].strftime('%d-%m-%Y')} and {block[1].strftime('%d-%m-%Y')}"
                    )
                    download_data(p, dtype, block[0], block[1], r)
    finally:
        r.close()


if __name__ == "__main__":
    main()
