import random
from datetime import datetime, timedelta

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from utils import store
from utils.date import daterange
from utils.sync import get_redis_set_members

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env.int("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")  # Chromium or webkit won't work with NSE website
MAX_DOWNLOAD_ATTEMPTS = 2


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def latest_completed_day() -> datetime:
    return datetime.today() - timedelta(days=1)


def download_data(
    playwright,
    dtype: str,
    from_date: datetime,
    to_date: datetime,
    rop: redis.Redis,
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
        mark_dates_as_downloaded(rop, dtype, from_date, to_date)
        return True

    except Exception as err:
        print(f"❌ Failed: {from_date_str} - {to_date_str} - {err}")
        return False

    finally:
        page.close()
        browser.close()


def mark_dates_as_downloaded(rop, dtype, start_date, end_date):
    rop.sadd(
        f"nse:{dtype}",
        *(dt.strftime("%Y-%m-%d") for dt in daterange(start_date, end_date)),
    )


# Check missing dates
def get_missing_dates(rop, dtype, start_date, end_date, skipped_dates=None):
    skipped_dates = skipped_dates or set()
    existing_members = get_redis_set_members(rop, f"nse:{dtype}")
    return [
        d
        for d in daterange(start_date, end_date)
        if d.strftime("%Y-%m-%d") not in existing_members
        and d.strftime("%Y-%m-%d") not in skipped_dates
    ]


def get_next_download_block(rop, dtype, g_start, g_end, skipped_dates=None):
    # All missing calendar days for this dtype
    missing = get_missing_dates(rop, dtype, g_start, g_end, skipped_dates=skipped_dates)
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
        rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        global_start = datetime.strptime("2014-01-01", "%Y-%m-%d")
        global_end = latest_completed_day()

        print(f"Downloading NSE offmarket data through {global_end.strftime('%d-%m-%Y')}")

        with sync_playwright() as p:
            for dtype in ["block_deals", "bulk_deals", "short_selling"]:
                skipped_dates: set[str] = set()
                while True:
                    block = get_next_download_block(
                        rop, dtype, global_start, global_end, skipped_dates=skipped_dates
                    )
                    if not block:
                        print(f"All data downloaded for {dtype} ✅")
                        break
                    print(f"Download {dtype} {block[0].strftime('%d-%m-%Y')} and {block[1].strftime('%d-%m-%Y')}")
                    success = False
                    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
                        print(
                            f"Attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS} for {dtype} "
                            f"{block[0].strftime('%d-%m-%Y')} -> {block[1].strftime('%d-%m-%Y')}"
                        )
                        success = download_data(p, dtype, block[0], block[1], rop)
                        if success:
                            break
                    if not success:
                        skipped = [dt.strftime("%Y-%m-%d") for dt in daterange(block[0], block[1])]
                        skipped_dates.update(skipped)
                        print(
                            f"⏭️ Skipping {dtype} {block[0].strftime('%d-%m-%Y')} -> "
                            f"{block[1].strftime('%d-%m-%Y')} after {MAX_DOWNLOAD_ATTEMPTS} failed attempts"
                        )
    finally:
        rop.close()


if __name__ == "__main__":
    main()
