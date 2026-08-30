# offmarket.py -- NSE block deals, bulk deals, and short-selling downloader.
#
# REVIVED 2026-08-29 (PRD §12 todo #1): deleted 2026-08-15 alongside 14 other
# write-only tables, but for a DIFFERENT reason than the others -- this one was
# "also independently broken: NSE download-trigger timeouts on every recent
# run" (see the deletion commit). Root cause, found by diffing against
# bhavcopy_downloader.py (the collector this was NOT modeled on, but should
# have been): the old version navigated straight to the deep report page
# (`nse_goto(page, ".../display-bulk-and-block-deals")`) with no homepage
# warmup first. Every OTHER working nseindia collector visits nseindia.com
# first to establish session cookies before a deep-page navigation --
# skipping that step is the likely reason the download link never rendered
# in time. Also hardened while reviving: a bad request could span up to 365
# days (an entire year of deals in one CSV) with only a 10s download timeout
# -- NSE's report backend visibly takes longer to prepare a wide date range
# than bhavcopy's single-day ZIP, so this also caps block size and adds a
# zero-byte guard (bhavcopy_downloader's own fix for the same failure class).
#
# Feeds fundamentals PRD §12 axis 4 (ownership signal) and new L3 triggers
# (todo #3) -- NOT part of systrader's PRIMARY series, not routed through
# DATA_CONTRACT.md. purpose="fundamentals_deal_flow" (not in
# download_runner.CRITICAL_PURPOSES) so a bad NSE day here never fails the
# critical market-data path.
import json
import os
import random
from datetime import datetime, timedelta

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from utils.fallback_telemetry import record_local_fallback_event
from utils import store
from utils.date import daterange
from utils.nse_rate_limiter import nse_goto
from utils.sync import get_redis_client

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env.int("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")  # Chromium or webkit won't work with NSE website
MAX_DOWNLOAD_ATTEMPTS = 2
NSE_OFFMARKET_DOWNLOAD_LOOKBACK_DAYS = max(env.int("NSE_OFFMARKET_DOWNLOAD_LOOKBACK_DAYS", 365), 1)
# Capped well below the old 365-day ceiling -- a full year of deals in one
# request is plausibly why the download link never rendered inside the old
# 10s timeout. 30 days keeps the report backend's own response time close to
# what bhavcopy's single-day ZIP sees, at the cost of more round trips for a
# large backfill (acceptable: this runs incrementally after backfill anyway).
NSE_OFFMARKET_MAX_BLOCK_DAYS = max(env.int("NSE_OFFMARKET_MAX_BLOCK_DAYS", 30), 1)
# NSE's report-detail page does more client-side work per download (dropdown
# select, custom-range JS injection, GO click) than bhavcopy's static
# Archives tab -- 10s was tight even for a single day; give it more margin.
DOWNLOAD_TIMEOUT_MS = max(env.int("NSE_OFFMARKET_DOWNLOAD_TIMEOUT_MS", 20_000), 1_000)
MAX_CONSECUTIVE_FAILURES = max(env.int("NSE_OFFMARKET_MAX_CONSECUTIVE_FAILURES", 7), 1)
SYNC_SOURCE_NAME = "data.nseindia.offmarket"
STOCKEY_RUN_STATE: dict[str, object] = {}

DTYPES = ("block_deals", "bulk_deals", "short_selling")


def extract_downloaded_dates_from_key(key: str, dtype: str) -> set[str]:
    filename = os.path.basename(key)
    prefix = f"{dtype}_"
    suffix = ".csv"
    if not filename.startswith(prefix) or not filename.endswith(suffix):
        return set()
    body = filename[len(prefix):-len(suffix)]
    try:
        from_date_str, to_date_str = body.split("_", 1)
        start_date = datetime.strptime(from_date_str, "%d-%m-%Y")
        end_date = datetime.strptime(to_date_str, "%d-%m-%Y")
    except ValueError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(key),
            fallback_type="nse_offmarket_download_key_date_parse_failed",
            severity="warn",
            reason="Stored NSE off-market/deals file key matched the expected prefix but did not contain a parseable date range, so it will not be treated as downloaded.",
            error=exc,
            metadata={"key": str(key), "dtype": str(dtype), "filename": filename},
        )
        return set()
    return {dt.strftime("%Y-%m-%d") for dt in daterange(start_date, end_date)}


def load_downloaded_dates_from_store(dtype: str) -> set[str]:
    downloaded: set[str] = set()
    for key in store.list_files("nsedeals"):
        downloaded.update(extract_downloaded_dates_from_key(key, dtype))
    return downloaded


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
    Automate NSE Block/Bulk/Short-selling deals download.
    Returns True on success, False on any exception (including a zero-byte
    response, which NSE can return for a slow/blocked request without
    actually raising).
    """

    if dtype not in DTYPES:
        raise ValueError('Wrong value for dtype, can only be "short_selling", "block_deals", "bulk_deals"')

    from_date_str = from_date.strftime("%d-%m-%Y")
    to_date_str = to_date.strftime("%d-%m-%Y")

    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    file_path: str | None = None

    try:
        # Homepage warmup before the deep report page -- every other working
        # nseindia collector does this (see bhavcopy_downloader.py); the old,
        # broken version of this file skipped straight to the report page.
        page.wait_for_timeout(get_random(2000, 5000))
        nse_goto(page, "https://www.nseindia.com")
        page.wait_for_timeout(get_random(1000, 3000))
        nse_goto(page, "https://www.nseindia.com/report-detail/display-bulk-and-block-deals")
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

        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            page.get_by_role("link", name="csv Download (.csv)").click()
        download = download_info.value

        file_path = f"{dtype}_{from_date_str}_{to_date_str}.csv"
        download.save_as(file_path)

        # A zero-byte download is a placeholder/failed fetch, never a valid
        # response -- same guard bhavcopy_downloader.py added for the same
        # failure class. Storing it would freeze this range as "downloaded"
        # with nothing in it.
        if os.path.getsize(file_path) == 0:
            os.remove(file_path)
            file_path = None
            print(f"❌ Zero-byte {dtype} download for {from_date_str}-{to_date_str}; not stored, will retry")
            return False

        store.save_file(file_path, prefix="nsedeals")
        file_path = None

        print(f"✅ Success: {dtype} {from_date_str} - {to_date_str}")
        mark_dates_as_downloaded(rop, dtype, from_date, to_date)
        return True

    except Exception as err:
        print(f"❌ Failed: {dtype} {from_date_str} - {to_date_str} - {err}")
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=f"nsedeals:{dtype}:{from_date.strftime('%Y-%m-%d')}:{to_date.strftime('%Y-%m-%d')}",
            fallback_type="nse_offmarket_download_failed",
            severity="warn",
            reason="NSE off-market/deals download failed for this date block; block/bulk/short-selling evidence may be incomplete until catch-up succeeds.",
            error=err,
            metadata={
                "dtype": dtype,
                "from_date": from_date.strftime("%Y-%m-%d"),
                "to_date": to_date.strftime("%Y-%m-%d"),
                "from_date_display": from_date_str,
                "to_date_display": to_date_str,
            },
        )
        return False

    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
        page.close()
        browser.close()


def mark_dates_as_downloaded(rop, dtype, start_date, end_date):
    rop.sadd(
        f"nse:{dtype}",
        *(dt.strftime("%Y-%m-%d") for dt in daterange(start_date, end_date)),
    )


def get_missing_dates(rop, dtype, start_date, end_date, skipped_dates=None):
    skipped_dates = skipped_dates or set()
    existing_members = load_downloaded_dates_from_store(dtype)
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
        if (prev - dt).days == 1 and count < NSE_OFFMARKET_MAX_BLOCK_DAYS:  # still consecutive
            start = dt
            prev = dt  # <-- advance the reference!
            count += 1
        else:
            break  # gap or block-size limit reached

    return start, end  # inclusive


def main() -> int:
    global STOCKEY_RUN_STATE
    rop = None
    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "purpose": "fundamentals_deal_flow",
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "dtype_count": len(DTYPES),
        "blocks_attempted": 0,
        "download_attempts": 0,
        "attempt_count": 0,
        "failed_attempt_count": 0,
        "retry_count": 0,
        "fallback_count": 0,
        "skipped_blocks": 0,
        "downloaded_blocks": 0,
        "dates_downloaded": 0,
        "dates_skipped": 0,
        "stopped_after_consecutive_failures": False,
        "fallback_used": False,
        "state_advanced": False,
    }
    try:
        rop = get_redis_client(REDIS_HOST, REDIS_PORT)
        global_start = datetime.today() - timedelta(days=NSE_OFFMARKET_DOWNLOAD_LOOKBACK_DAYS)
        global_end = latest_completed_day()
        state["from_date"] = global_start.strftime("%Y-%m-%d")
        state["to_date"] = global_end.strftime("%Y-%m-%d")

        print(f"Downloading NSE offmarket data through {global_end.strftime('%d-%m-%Y')}")

        with sync_playwright() as p:
            for dtype in DTYPES:
                skipped_dates: set[str] = set()
                consecutive_failures = 0
                while True:
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"📉 Stopped {dtype} after {consecutive_failures} consecutive block failures.")
                        state["stopped_after_consecutive_failures"] = True
                        break
                    block = get_next_download_block(
                        rop, dtype, global_start, global_end, skipped_dates=skipped_dates
                    )
                    if not block:
                        print(f"All data downloaded for {dtype} ✅")
                        break
                    block_dates = [dt.strftime("%Y-%m-%d") for dt in daterange(block[0], block[1])]
                    state["blocks_attempted"] = int(state["blocks_attempted"]) + 1
                    print(f"Download {dtype} {block[0].strftime('%d-%m-%Y')} and {block[1].strftime('%d-%m-%Y')}")
                    success = False
                    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
                        state["download_attempts"] = int(state["download_attempts"]) + 1
                        state["attempt_count"] = int(state["attempt_count"]) + 1
                        if attempt > 1:
                            state["retry_count"] = int(state["retry_count"]) + 1
                        print(
                            f"Attempt {attempt}/{MAX_DOWNLOAD_ATTEMPTS} for {dtype} "
                            f"{block[0].strftime('%d-%m-%Y')} -> {block[1].strftime('%d-%m-%Y')}"
                        )
                        success = download_data(p, dtype, block[0], block[1], rop)
                        if success:
                            state["downloaded_blocks"] = int(state["downloaded_blocks"]) + 1
                            state["dates_downloaded"] = int(state["dates_downloaded"]) + len(block_dates)
                            consecutive_failures = 0
                            break
                        state["failed_attempt_count"] = int(state["failed_attempt_count"]) + 1
                    if not success:
                        skipped_dates.update(block_dates)
                        consecutive_failures += 1
                        state["skipped_blocks"] = int(state["skipped_blocks"]) + 1
                        state["dates_skipped"] = int(state["dates_skipped"]) + len(block_dates)
                        print(
                            f"⏭️ Skipping {dtype} {block[0].strftime('%d-%m-%Y')} -> "
                            f"{block[1].strftime('%d-%m-%Y')} after {MAX_DOWNLOAD_ATTEMPTS} failed attempts"
                        )
    finally:
        if rop is not None:
            rop.close()
    state["rows"] = int(state["dates_downloaded"])
    state["rows_read"] = int(state["blocks_attempted"])
    state["rows_written"] = int(state["dates_downloaded"])
    state["source_unavailable_count"] = int(state["skipped_blocks"])
    state["state_advanced"] = int(state["downloaded_blocks"]) > 0
    STOCKEY_RUN_STATE = state
    # A hardcoded "ok"/0 here regardless of outcome would hide a fully failed run (e.g. NSE
    # blocking this session) behind a false success -- purpose="fundamentals_deal_flow" is
    # non-critical (download_runner.CRITICAL_PURPOSES), so this never aborts the critical
    # market-data path, but it must still surface honestly for data_readiness/coverage
    # reporting. Only a TOTAL failure (nothing downloaded despite attempts) flips this -- a
    # partial run is still real forward progress and stays "ok".
    total_failure = int(state["blocks_attempted"]) > 0 and int(state["downloaded_blocks"]) == 0
    run_status = "source_unavailable" if total_failure else "ok"
    print(json.dumps({"status": run_status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 1 if total_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
