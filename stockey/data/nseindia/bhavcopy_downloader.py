# bhavcopy_downloader.py
import os
import random
from datetime import datetime, timedelta

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from utils.ingestion_state import get_failed_entries
from utils import store
from utils.date import reverse_daterange
from utils.sync import get_redis_client, filter_missing_date_members

env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:downloaded"
NSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS = max(env.int("NSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS", 365), 1)
SOURCE_PREFIX = "bhavcopy"


def extract_downloaded_date_from_key(key: str) -> str | None:
    try:
        return datetime.strptime(os.path.basename(key), "bhavcopy_%Y-%m-%d.zip").strftime("%Y-%m-%d")
    except ValueError:
        return None


def load_downloaded_dates_from_store() -> set[str]:
    failed_keys = {row["object_key"] for row in get_failed_entries(SOURCE_PREFIX)}
    downloaded: set[str] = set()
    for key in store.list_files("bhavcopy"):
        if key in failed_keys:
            continue
        parsed = extract_downloaded_date_from_key(key)
        if parsed:
            downloaded.add(parsed)
    return downloaded


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def download_bhavcopy_for_date(
    playwright,
    formatted_date: str,
    display_date: str,
    rop: redis.Redis,
) -> bool:
    """
    Automate NSE 'Archives' tab to download the ZIP for a single day.
    Returns True on success, False on any exception.
    """
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    try:
        page.wait_for_timeout(get_random(2000, 5000))
        page.goto("https://www.nseindia.com")
        page.wait_for_timeout(get_random(1000, 3000))
        page.goto("https://www.nseindia.com/all-reports")
        page.wait_for_timeout(get_random(1000, 2000))

        page.get_by_role("tab", name="Archives").click()
        page.wait_for_timeout(get_random(2000, 3000))

        # Fill the calendar input via jQuery (NSE page already loads jQuery)
        js_code = f'$("#cr_equity_archives_date").val("{display_date}");'
        page.evaluate(js_code)
        page.wait_for_timeout(1000)

        page.get_by_role("checkbox", name="Select All Reports").click()
        page.wait_for_timeout(1000)

        with page.expect_download(timeout=10_000) as download_info:
            page.get_by_role("link", name="Multiple file Download ").click()
        download = download_info.value

        file_path = f"bhavcopy_{formatted_date}.zip"
        download.save_as(file_path)

        store.save_file( file_path=file_path, prefix="bhavcopy")
        os.remove(file_path)

        weekday = datetime.strptime(display_date, "%d-%b-%Y").strftime("%A")
        print(f"✅ Success: {formatted_date} ({weekday})")
        rop.sadd(REDIS_SET, formatted_date)
        return True

    except Exception as err:
        weekday = datetime.strptime(display_date, "%d-%b-%Y").strftime("%A")
        print(f"❌ Failed: {formatted_date} ({weekday})  — {err}")
        return False

    finally:
        page.close()
        browser.close()


def main() -> None:
    rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))
    failures = 0
    existing_members = load_downloaded_dates_from_store()
    start_date = datetime.today() - timedelta(days=NSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS)
    all_dates = list(
        reverse_daterange(start_date, datetime.today() - timedelta(days=1))
    )
    missing_dates = filter_missing_date_members(all_dates, existing_members)

    with sync_playwright() as p:
        for date_obj in missing_dates:
            if failures >= 7:
                break
            formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19
            display_date = date_obj.strftime("%d-%b-%Y")  # 19-Jun-2025

            success = download_bhavcopy_for_date(
                p, formatted_date, display_date, rop
            )
            failures = 0 if success else failures + 1

    if failures >= 7:
        print("📉 Stopped after 7 consecutive failures.")
    else:
        print("All caught up! Done")
    rop.close()


if __name__ == "__main__":
    main()
