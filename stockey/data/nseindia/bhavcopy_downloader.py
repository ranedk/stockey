# bhavcopy_downloader.py
import os
import random
from datetime import datetime, timedelta

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from utils import store
from utils.date import reverse_daterange

env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:downloaded"


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
    rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    failures = 0

    with sync_playwright() as p:
        for date_obj in reverse_daterange(datetime(2014,1,1), datetime.today() - timedelta(days=1)):
            if failures >= 7:
                break
            formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19
            display_date = date_obj.strftime("%d-%b-%Y")  # 19-Jun-2025

            if rop.sismember(REDIS_SET, formatted_date):
                print(f"⏩ Already downloaded: {formatted_date}")
                continue

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
