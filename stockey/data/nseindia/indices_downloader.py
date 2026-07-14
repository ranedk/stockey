# indices_downloader.py
import argparse
import json
import os
import sys
import random
from datetime import datetime, timedelta

import redis
from environs import Env
from playwright.sync_api import sync_playwright
from advisory.fallback_telemetry import record_local_fallback_event
from utils import store
from utils.date import reverse_daterange
from utils.sync import get_redis_client


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:indices:downloaded"
NSE_INDICES_DOWNLOAD_LOOKBACK_DAYS = max(env.int("NSE_INDICES_DOWNLOAD_LOOKBACK_DAYS", 365), 1)
# An empty (0-byte) NSE response is a transient failure, NOT a real download. Retry it a few times in the
# same run, then keep retrying it on the next few daily runs (it stays un-persisted so it re-appears as a
# candidate), and only give up after a window -- so a transient empty never becomes a permanent gap.
NSE_INDICES_EMPTY_DOWNLOAD_RETRIES = max(env.int("NSE_INDICES_EMPTY_DOWNLOAD_RETRIES", 2), 0)
NSE_INDICES_EMPTY_RETRY_WINDOW_DAYS = max(env.int("NSE_INDICES_EMPTY_RETRY_WINDOW_DAYS", 4), 1)
EMPTY_ATTEMPTS_HASH = "nse:indices:empty_first_seen"   # date(YYYY-MM-DD) -> first-empty ISO date
EMPTY_GAVEUP_SET = "nse:indices:empty_gaveup"          # dates we stopped retrying after the window
SOURCE_PREFIX = "indices"
SYNC_SOURCE_NAME = "data.nseindia.indices_downloader"
STOCKEY_RUN_STATE: dict[str, object] = {}


def _redis_str_map(raw: dict) -> dict[str, str]:
    """Decode a Redis hash (bytes-or-str keys/values) to a plain str->str dict."""
    def _s(v):
        return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return {_s(k): _s(v) for k, v in (raw or {}).items()}


def partition_empty_attempts(
    first_seen: dict[str, str], existing: set[str], *, today: datetime, window_days: int
) -> tuple[list[str], list[str]]:
    """Split empty-attempt dates into (retry_now, give_up). retry: within the window and still missing from
    the store (re-attempt on this run). give_up: first-empty older than window_days -> stop retrying."""
    retry: list[str] = []
    give_up: list[str] = []
    for date_str, first_iso in first_seen.items():
        if date_str in existing:  # got downloaded another way -> caller clears the marker
            continue
        try:
            first = datetime.fromisoformat(first_iso)
        except (ValueError, TypeError):
            give_up.append(date_str)
            continue
        if (today.date() - first.date()).days > window_days:
            give_up.append(date_str)
        else:
            retry.append(date_str)
    return sorted(set(retry)), sorted(set(give_up))


def extract_downloaded_date_from_key(key: str) -> str | None:
    try:
        return datetime.strptime(os.path.basename(key), "indices_%Y-%m-%d.zip").strftime("%Y-%m-%d")
    except ValueError as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=str(key),
            fallback_type="nse_indices_download_key_date_parse_failed",
            severity="warn",
            reason="Stored indices archive key did not match indices_YYYY-MM-DD.zip and will not be treated as downloaded.",
            error=exc,
            metadata={"key": str(key), "source_prefix": SOURCE_PREFIX},
        )
        return None


def load_downloaded_dates_from_store() -> set[str]:
    downloaded: set[str] = set()
    for key in store.list_files("indices"):
        parsed = extract_downloaded_date_from_key(key)
        if parsed:
            downloaded.add(parsed)
    return downloaded


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def latest_downloaded_date(existing_members: set[str]) -> datetime | None:
    parsed = []
    for value in existing_members:
        try:
            parsed.append(datetime.strptime(value, "%Y-%m-%d"))
        except ValueError as exc:
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=str(value),
                fallback_type="nse_indices_downloaded_member_parse_failed",
                severity="warn",
                reason="Stored indices downloaded-date member was not YYYY-MM-DD and will be ignored for incremental anchoring.",
                error=exc,
                metadata={"member": str(value), "source_prefix": SOURCE_PREFIX},
            )
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
) -> str:
    """
    Automate NSE 'Archives' tab to download the ZIP for a single day.

    Returns one of: "downloaded" (a non-empty archive was persisted), "empty" (NSE served a 0-byte
    file -- a transient failure that is NOT persisted, so the date stays a candidate for retry), or
    "error" (any exception). A 0-byte file must never be saved: it would make the date look downloaded
    forever and permanently block re-fetch.
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

        weekday = datetime.strptime(display_date, "%d-%b-%Y").strftime("%A")
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        if size <= 0:
            # A 0-byte archive is a transient NSE failure, not a real report. Do NOT persist it (a saved
            # empty would make the date look downloaded forever and block re-fetch) -- leave the date
            # missing so it is retried in-run and on the next few daily runs.
            if os.path.exists(file_path):
                os.remove(file_path)
            print(f"⏭️ Empty (0-byte) download: {formatted_date} ({weekday}) -- not persisted, will retry")
            return "empty"

        store.save_file( file_path=file_path, prefix="indices")
        os.remove(file_path)

        print(f"✅ Success: {formatted_date} ({weekday})")
        rop.sadd(REDIS_SET, formatted_date)
        return "downloaded"

    except Exception as err:
        weekday = datetime.strptime(display_date, "%d-%b-%Y").strftime("%A")
        print(f"❌ Failed: {formatted_date} ({weekday})  — {err}")
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=f"indices:{formatted_date}",
            fallback_type="nse_indices_download_failed",
            severity="warn",
            reason="NSE indices archive download failed for this date; index/benchmark evidence may be incomplete until catch-up succeeds.",
            error=err,
            metadata={
                "formatted_date": formatted_date,
                "display_date": display_date,
                "weekday": weekday,
                "source_prefix": "indices",
            },
        )
        return "error"

    finally:
        page.close()
        browser.close()


def download_with_empty_retries(
    playwright,
    formatted_date: str,
    display_date: str,
    rop: redis.Redis,
) -> str:
    """Call download_indices_for_date, retrying up to NSE_INDICES_EMPTY_DOWNLOAD_RETRIES more times on an
    empty (0-byte) result within the same run. Returns the final status: "downloaded", "empty", or "error".
    A real exception ("error") is not retried here -- the outer run-level failure budget handles it."""
    status = "empty"
    for attempt in range(NSE_INDICES_EMPTY_DOWNLOAD_RETRIES + 1):
        status = download_indices_for_date(playwright, formatted_date, display_date, rop)
        if status != "empty":
            return status
        if attempt < NSE_INDICES_EMPTY_DOWNLOAD_RETRIES:
            print(f"   ↻ retrying empty download {formatted_date} ({attempt + 1}/{NSE_INDICES_EMPTY_DOWNLOAD_RETRIES})")
    return status


def main() -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Download NSE indices archive zips.")
    parser.add_argument("--backfill", action="store_true", help="Scan a historical date window instead of incremental mode")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))
    failures = 0
    downloaded_count = 0
    failed_attempt_count = 0
    attempted_count = 0
    empty_result_count = 0
    gave_up_count = 0
    existing_members = load_downloaded_dates_from_store()
    latest_done = latest_downloaded_date(existing_members)
    now = datetime.today()

    # Cross-run empty-retry bookkeeping. A date that came back empty stays un-persisted (so it never blocks
    # re-fetch) and is retried on subsequent runs until the window elapses, then we give up so it can't
    # retry forever. `first_seen` maps date -> first-empty ISO date; `gave_up` is the stop-retrying set.
    first_seen = _redis_str_map(rop.hgetall(EMPTY_ATTEMPTS_HASH))
    gave_up = {
        (m.decode() if isinstance(m, (bytes, bytearray)) else str(m))
        for m in (rop.smembers(EMPTY_GAVEUP_SET) or set())
    }
    retry_dates, give_up_dates = partition_empty_attempts(
        first_seen, existing_members, today=now, window_days=NSE_INDICES_EMPTY_RETRY_WINDOW_DAYS
    )
    for date_str in give_up_dates:
        rop.sadd(EMPTY_GAVEUP_SET, date_str)
        rop.hdel(EMPTY_ATTEMPTS_HASH, date_str)
        gave_up.add(date_str)
        gave_up_count += 1
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=f"indices:{date_str}",
            fallback_type="nse_indices_download_gave_up",
            severity="warn",
            reason=(
                f"NSE indices archive stayed empty for > {NSE_INDICES_EMPTY_RETRY_WINDOW_DAYS} days; giving "
                "up retries so the downloader stops re-attempting it. Index/benchmark evidence for this date "
                "is missing until an operator backfill re-fetches it."
            ),
            metadata={"formatted_date": date_str, "window_days": NSE_INDICES_EMPTY_RETRY_WINDOW_DAYS, "source_prefix": "indices"},
        )

    default_start_date = now - timedelta(days=NSE_INDICES_DOWNLOAD_LOOKBACK_DAYS)
    end_date = parse_datetime_arg(args.to_date) or (now - timedelta(days=1))
    if args.backfill:
        start_date = parse_datetime_arg(args.from_date) or default_start_date
    else:
        start_date = parse_datetime_arg(args.from_date) or (
            default_start_date if latest_done is None else latest_done + timedelta(days=1)
        )

    # Forward-window candidates (incremental anchors on latest_done, so it only walks the leading edge).
    if start_date <= end_date:
        if args.backfill:
            window_candidates = [
                d for d in reverse_daterange(start_date, end_date)
                if d.strftime("%Y-%m-%d") not in existing_members
            ]
        else:
            window_candidates = list(reverse_daterange(start_date, end_date))
    else:
        window_candidates = []

    # Merge in cross-run empty-retry dates -- these are recent MIDDLE gaps the forward-only incremental
    # window would otherwise never revisit. An explicit --backfill is an operator override and may also
    # re-attempt gave-up dates; incremental runs skip gave-up dates so they stop retrying.
    retry_objs = [datetime.strptime(d, "%Y-%m-%d") for d in retry_dates]
    seen: set[str] = set()
    candidate_dates: list[datetime] = []
    for date_obj in list(window_candidates) + retry_objs:
        key = date_obj.strftime("%Y-%m-%d")
        if key in seen or key in existing_members:
            continue
        if key in gave_up and not args.backfill:
            continue
        seen.add(key)
        candidate_dates.append(date_obj)
    candidate_dates.sort(reverse=True)

    if not candidate_dates:
        print("All caught up! Done")
        rop.close()
        STOCKEY_RUN_STATE = {
            "source": SYNC_SOURCE_NAME,
            "rows": 0,
            "rows_read": 0,
            "rows_written": 0,
            "from_date": start_date.strftime("%Y-%m-%d"),
            "to_date": end_date.strftime("%Y-%m-%d"),
            "mode": "backfill" if args.backfill else "incremental",
            "candidate_dates": 0,
            "missing_dates": 0,
            "download_attempts": 0,
            "attempt_count": 0,
            "failed_attempt_count": 0,
            "retry_count": 0,
            "empty_result_count": 0,
            "gave_up_count": gave_up_count,
            "source_unavailable_count": 0,
            "fallback_used": bool(gave_up_count),
            "state_advanced": False,
        }
        print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
        return 0

    print(
        "Indices download window:",
        {
            "mode": "backfill" if args.backfill else "incremental",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "latest_downloaded": None if latest_done is None else latest_done.strftime("%Y-%m-%d"),
            "empty_retry_dates": len(retry_objs),
            "gave_up": gave_up_count,
        },
    )

    try:
        with sync_playwright() as p:
            for date_obj in candidate_dates:
                if failures >= 7:
                    break
                attempted_count += 1
                formatted_date = date_obj.strftime("%Y-%m-%d")  # 2025-06-19
                display_date = date_obj.strftime("%d-%b-%Y")  # 19-Jun-2025

                status = download_with_empty_retries(
                    p, formatted_date, display_date, rop
                )
                if status == "downloaded":
                    downloaded_count += 1
                    failures = 0
                    # a real archive arrived -> clear any empty/gave-up markers so the date is settled
                    rop.hdel(EMPTY_ATTEMPTS_HASH, formatted_date)
                    rop.srem(EMPTY_GAVEUP_SET, formatted_date)
                    first_seen.pop(formatted_date, None)
                    gave_up.discard(formatted_date)
                else:
                    failed_attempt_count += 1
                    failures += 1
                    if status == "empty":
                        empty_result_count += 1
                        # record the first-empty date so the cross-run retry window can bound the retries
                        if formatted_date not in first_seen:
                            iso = now.date().isoformat()
                            rop.hset(EMPTY_ATTEMPTS_HASH, formatted_date, iso)
                            first_seen[formatted_date] = iso
    finally:
        rop.close()

    stopped_after_failures = failures >= 7
    skipped_after_failure_stop = max(len(candidate_dates) - attempted_count, 0)
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": downloaded_count,
        "rows_read": attempted_count,
        "rows_written": downloaded_count,
        "from_date": start_date.strftime("%Y-%m-%d"),
        "to_date": end_date.strftime("%Y-%m-%d"),
        "mode": "backfill" if args.backfill else "incremental",
        "latest_downloaded": None if latest_done is None else latest_done.strftime("%Y-%m-%d"),
        "candidate_dates": len(candidate_dates),
        "missing_dates": len(candidate_dates),
        "download_attempts": attempted_count,
        "attempt_count": attempted_count,
        "failed_attempt_count": failed_attempt_count,
        "retry_count": 0,
        "empty_result_count": empty_result_count,
        "gave_up_count": gave_up_count,
        "downloaded_dates": downloaded_count,
        "source_unavailable_count": failed_attempt_count,
        "skipped_after_failure_stop": skipped_after_failure_stop,
        "stopped_after_consecutive_failures": stopped_after_failures,
        "fallback_used": bool(gave_up_count),
        "state_advanced": downloaded_count > 0,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    if stopped_after_failures:
        sys.exit("Stopped after 7 consecutive failures during backfill.")
    print("All caught up! Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
