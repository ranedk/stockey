# indices_downloader.py
import argparse
import os
import time
import random
from datetime import datetime, timedelta

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from utils import store
from utils.date import reverse_daterange
from utils.chrome import restart_chrome
from utils.sync import get_redis_client, get_redis_set_members


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:indices:downloaded"
DEFAULT_START_DATE = datetime(2014, 1, 1)


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def latest_downloaded_date(existing_members: set[str]) -> datetime | None:
    parsed = []
    for value in existing_members:
        try:
            parsed.append(datetime.strptime(value, "%Y-%m-%d"))
        except ValueError:
            continue
    return max(parsed) if parsed else None


def parse_datetime_arg(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def download_indices_for_date(
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

        page.get_by_role("tab", name="Indices").click()

        page.get_by_role("tab", name="Archives").click()
        page.wait_for_timeout(get_random(2000, 3000))

        # Fill the calendar input via jQuery (NSE page already loads jQuery)
        js_code = f'$("#cr_indices_archives_date").val("{display_date}");'
        page.evaluate(js_code)
        page.wait_for_timeout(1000)

        page.get_by_role("checkbox", name="Select All Reports").click()
        page.wait_for_timeout(1000)

        with page.expect_download(timeout=10_000) as download_info:
            page.get_by_role("link", name="Multiple file Download ").click()
        download = download_info.value

        file_path = f"indices_{formatted_date}.zip"
        download.save_as(file_path)

        store.save_file( file_path=file_path, prefix="indices")
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
    parser = argparse.ArgumentParser(description="Download NSE indices archive zips.")
    parser.add_argument("--backfill", action="store_true", help="Scan a historical date window instead of incremental mode")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))
    failures = 0
    existing_members = get_redis_set_members(rop, REDIS_SET)
    latest_done = latest_downloaded_date(existing_members)
    end_date = parse_datetime_arg(args.to_date) or (datetime.today() - timedelta(days=1))
    if args.backfill:
        start_date = parse_datetime_arg(args.from_date) or DEFAULT_START_DATE
    else:
        start_date = parse_datetime_arg(args.from_date) or (
            DEFAULT_START_DATE if latest_done is None else latest_done + timedelta(days=1)
        )

    if start_date > end_date:
        print("All caught up! Done")
        rop.close()
        return

    print(
        "Indices download window:",
        {
            "mode": "backfill" if args.backfill else "incremental",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "latest_downloaded": None if latest_done is None else latest_done.strftime("%Y-%m-%d"),
        },
    )

    if args.backfill:
        candidate_dates = [
            date_obj
            for date_obj in reverse_daterange(start_date, end_date)
            if date_obj.strftime("%Y-%m-%d") not in existing_members
        ]
    else:
        candidate_dates = list(reverse_daterange(start_date, end_date))

    with sync_playwright() as p:
        for date_obj in candidate_dates:
            if failures >= 7:
                break
            formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19
            display_date = date_obj.strftime("%d-%b-%Y")  # 19-Jun-2025

            success = download_indices_for_date(
                p, formatted_date, display_date, rop
            )
            failures = 0 if success else failures + 1

    if failures >= 7:
        restart_chrome()
        time.sleep(20)
        if args.backfill:
            print("Stopped after 7 consecutive failures during backfill.")
        else:
            main()
    else:
        print("All caught up! Done")
    rop.close()


if __name__ == "__main__":
    main()
