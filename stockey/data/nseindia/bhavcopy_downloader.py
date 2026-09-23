# bhavcopy_downloader.py
import json
import os
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from utils.cdp import connect_over_cdp as connect_over_cdp_guarded
from utils.fallback_telemetry import record_local_fallback_event
from utils.ingestion_state import get_failed_entries
from utils import store
from utils.date import reverse_daterange
from utils.nse_rate_limiter import nse_goto
from utils.sync import get_redis_client, filter_missing_date_members

env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:downloaded"
NSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS = max(env.int("NSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS", 365), 1)
# An empty (0-byte) NSE response is a transient failure, not a real report. Retry it a few times in the
# same run before giving up for this run (the date stays missing -> re-attempted on the next run too).
NSE_BHAVCOPY_EMPTY_DOWNLOAD_RETRIES = max(env.int("NSE_BHAVCOPY_EMPTY_DOWNLOAD_RETRIES", 2), 0)
SOURCE_PREFIX = "bhavcopy"
SYNC_SOURCE_NAME = "data.nseindia.bhavcopy_downloader"
STOCKEY_RUN_STATE: dict[str, object] = {}


def extract_downloaded_date_from_key(key: str) -> str | None:
    try:
        return datetime.strptime(os.path.basename(key), "bhavcopy_%Y-%m-%d.zip").strftime("%Y-%m-%d")
    except ValueError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(key),
            fallback_type="nse_bhavcopy_download_key_date_parse_failed",
            severity="warn",
            reason="Stored bhavcopy archive key did not match bhavcopy_YYYY-MM-DD.zip and will not be treated as downloaded.",
            error=exc,
            metadata={"key": str(key), "source_prefix": SOURCE_PREFIX},
        )
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
    browser = connect_over_cdp_guarded(playwright, CDP_ENDPOINT, caller="data.nseindia.bhavcopy_downloader")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    try:
        page.wait_for_timeout(get_random(2000, 5000))
        nse_goto(page, "https://www.nseindia.com")
        page.wait_for_timeout(get_random(1000, 3000))
        nse_goto(page, "https://www.nseindia.com/all-reports")
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

        # A zero-byte download is a placeholder/failed fetch, never a valid day; storing it
        # freezes the feed (the parser can only mark it failed, and "file exists" would stop
        # future re-downloads). Refuse it so this date stays missing and retries.
        if os.path.getsize(file_path) == 0:
            os.remove(file_path)
            print(f"❌ Zero-byte bhavcopy download for {formatted_date}; not stored, will retry")
            return False

        store.save_file( file_path=file_path, prefix="bhavcopy")
        os.remove(file_path)

        weekday = datetime.strptime(display_date, "%d-%b-%Y").strftime("%A")
        print(f"✅ Success: {formatted_date} ({weekday})")
        rop.sadd(REDIS_SET, formatted_date)
        return True

    except Exception as err:
        weekday = datetime.strptime(display_date, "%d-%b-%Y").strftime("%A")
        print(f"❌ Failed: {formatted_date} ({weekday})  — {err}")
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=f"{SOURCE_PREFIX}:{formatted_date}",
            fallback_type="nse_bhavcopy_download_failed",
            severity="warn",
            reason="NSE bhavcopy archive download failed for this date; market-wide evidence may be incomplete until catch-up succeeds.",
            error=err,
            metadata={
                "formatted_date": formatted_date,
                "display_date": display_date,
                "weekday": weekday,
                "source_prefix": SOURCE_PREFIX,
            },
        )
        return False

    finally:
        page.close()
        browser.close()


def download_bhavcopy_with_retries(
    playwright,
    formatted_date: str,
    display_date: str,
    rop: redis.Redis,
) -> bool:
    """Retry a failed bhavcopy download a few times within the same run (a transient empty/failure often
    succeeds on a retry). The date also stays a candidate on the next run, so this only tightens the gap."""
    for attempt in range(NSE_BHAVCOPY_EMPTY_DOWNLOAD_RETRIES + 1):
        if download_bhavcopy_for_date(playwright, formatted_date, display_date, rop):
            return True
        if attempt < NSE_BHAVCOPY_EMPTY_DOWNLOAD_RETRIES:
            print(f"   retrying bhavcopy {formatted_date} ({attempt + 1}/{NSE_BHAVCOPY_EMPTY_DOWNLOAD_RETRIES})")
    return False


# NSE publishes the day's bhavcopy from ~16:00 IST, and reliably by ~17:30 (the hour
# the public downloaders schedule themselves). The evening chain runs 19:15 IST, so it
# CAN have the session that just closed -- but end_date was datetime.today() - 1 day,
# hard-coded, so it never asked for it: a session's file arrived only with the next
# morning's 07:10 IST run, its adjusted prices only at 02:15 UTC after that, and every
# systrader order sheet was therefore a session stale (found 2026-09-17).
NSE_BHAVCOPY_SAME_DAY_AFTER_IST = time(17, 30)


def _ist_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Kolkata"))


def download_end_date(now: datetime | None = None) -> datetime:
    """The newest date worth asking NSE for: today once the file is published and today
    is a trading day, else yesterday. A non-trading 'today' is never asked for -- the
    download would fail, and seven consecutive failures stop the run."""
    current = now or _ist_now()
    naive_today = datetime(current.year, current.month, current.day)
    if current.time() < NSE_BHAVCOPY_SAME_DAY_AFTER_IST:
        return naive_today - timedelta(days=1)
    try:
        from utils.advisory_date import latest_trading_day_on_or_before

        latest = latest_trading_day_on_or_before(naive_today.date())
        if latest.date() == naive_today.date():
            return naive_today
    except Exception as exc:  # calendar unavailable: behave as before rather than guess
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=SOURCE_PREFIX,
            fallback_type="nse_bhavcopy_trading_day_lookup_failed",
            severity="warn",
            reason="Could not confirm today is a trading day; asking NSE only up to yesterday.",
            error=exc,
            metadata={"ist_now": current.isoformat()},
        )
    return naive_today - timedelta(days=1)


def main() -> int:
    global STOCKEY_RUN_STATE
    rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))
    failures = 0
    downloaded_count = 0
    failed_attempt_count = 0
    attempted_count = 0
    existing_members = load_downloaded_dates_from_store()
    start_date = datetime.today() - timedelta(days=NSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS)
    end_date = download_end_date()
    all_dates = list(
        reverse_daterange(start_date, end_date)
    )
    missing_dates = filter_missing_date_members(all_dates, existing_members)

    try:
        with sync_playwright() as p:
            for date_obj in missing_dates:
                if failures >= 7:
                    break
                attempted_count += 1
                formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19
                display_date = date_obj.strftime("%d-%b-%Y")  # 19-Jun-2025

                success = download_bhavcopy_with_retries(
                    p, formatted_date, display_date, rop
                )
                if success:
                    downloaded_count += 1
                    failures = 0
                else:
                    failed_attempt_count += 1
                    failures += 1

        stopped_after_failures = failures >= 7
        if stopped_after_failures:
            print("📉 Stopped after 7 consecutive failures.")
        else:
            print("All caught up! Done")
    finally:
        rop.close()

    skipped_after_failure_stop = max(len(missing_dates) - attempted_count, 0)
    # A hardcoded "ok"/0 here regardless of outcome would hide a fully failed run (e.g. NSE
    # blocking this session) behind a false success -- bhavcopy_downloader's purpose is
    # CRITICAL in download_runner's classification, so this status/return code is what lets a
    # blocked run actually surface as a failure instead of silently reporting "ok". Only a
    # TOTAL failure (nothing downloaded despite attempts) flips this -- a partial run (some
    # dates succeeded, one flaky date didn't) is still real forward progress and stays "ok",
    # matching download_runner's own partial_failed-vs-source_unavailable distinction.
    total_failure = attempted_count > 0 and downloaded_count == 0
    status = "source_unavailable" if total_failure else "ok"
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": downloaded_count,
        "rows_read": attempted_count,
        "rows_written": downloaded_count,
        "from_date": start_date.strftime("%Y-%m-%d"),
        "to_date": end_date.strftime("%Y-%m-%d"),
        "candidate_dates": len(all_dates),
        "missing_dates": len(missing_dates),
        "download_attempts": attempted_count,
        "attempt_count": attempted_count,
        "failed_attempt_count": failed_attempt_count,
        "retry_count": 0,
        "downloaded_dates": downloaded_count,
        "source_unavailable_count": failed_attempt_count,
        "skipped_after_failure_stop": skipped_after_failure_stop,
        "stopped_after_consecutive_failures": stopped_after_failures,
        "fallback_used": False,
        "state_advanced": downloaded_count > 0,
    }
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 1 if total_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
