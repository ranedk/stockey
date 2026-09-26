# bhavcopy_downloader.py
import argparse
import json
import random
from datetime import datetime

import redis
import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright
from utils.cdp import connect_over_cdp as connect_over_cdp_guarded
from utils.fallback_telemetry import record_local_fallback_event
from utils.sync_state import persist_sync_state
from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.nse_rate_limiter import nse_goto
from utils.sync import get_redis_client


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:events"
SYNC_SOURCE_NAME = "data.nseindia.recent_events"
STOCKEY_RUN_STATE: dict[str, object] = {}


def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def classify_recent_events_error(error: object) -> str:
    text = str(error or "").lower()
    if "cdp" in text or "browser" in text or "target closed" in text or "connection refused" in text:
        return "source_unavailable"
    if "timed out" in text or "timeout" in text or "net::" in text or "download" in text:
        return "source_unavailable"
    if "could not parse recent event dates" in text or "columns" in text or "length mismatch" in text:
        return "parse_failed"
    return "failed"


def build_run_state(
    *,
    formatted_date: str,
    rows_written: int,
    classification: str,
    error: str | None = None,
) -> dict[str, object]:
    return {
        "source": SYNC_SOURCE_NAME,
        "rows": int(rows_written),
        "rows_read": 1,
        "rows_written": int(rows_written),
        "classification": classification,
        "status": "ok" if classification in {"ok", "no_data"} else "failed",
        "from_date": formatted_date,
        "to_date": formatted_date,
        "event_date": formatted_date,
        "fallback_used": False,
        "state_advanced": int(rows_written) > 0,
        "no_data_count": 1 if classification == "no_data" else 0,
        "source_unavailable_count": 1 if classification == "source_unavailable" else 0,
        "parse_failed_count": 1 if classification == "parse_failed" else 0,
        "error": error,
    }


def dowload_events(
    playwright,
    formatted_date: str,
    rop: redis.Redis,
) -> dict[str, object]:
    """
    Download the calendar csv file for all events
    Returns True on success, False on any exception.
    """
    browser = connect_over_cdp_guarded(playwright, CDP_ENDPOINT, caller="data.nseindia.recent_events")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()

    nse_goto(page, "https://www.nseindia.com")
    page.wait_for_timeout(get_random(1000, 3000))
    nse_goto(page, 'https://www.nseindia.com/companies-listing/corporate-filings-event-calendar')
    page.wait_for_timeout(get_random(8000, 10000))

    with page.expect_download(timeout=5_000) as download_info:
        page.get_by_role("link", name='csv Download (.csv)').click()
    download = download_info.value

    file_path = f"calendar_{formatted_date}.csv"
    download.save_as(file_path)

    df = parse_csv(file_path)

    print(f"✅ Success: {formatted_date}")
    rop.sadd(REDIS_SET, formatted_date)

    page.close()
    browser.close()
    return {"formatted_date": formatted_date, "rows": int(len(df)), "file_path": file_path}


def parse_event_dates(values: pd.Series) -> pd.Series:
    raw = values.astype("string").str.strip()
    parsed = pd.to_datetime(raw, format="%d-%B-%Y", errors="coerce")
    missing = parsed.isna() & raw.notna() & raw.ne("")
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(raw.loc[missing], format="%d-%b-%Y", errors="coerce")
    missing = parsed.isna() & raw.notna() & raw.ne("")
    if missing.any():
        try:
            parsed.loc[missing] = pd.to_datetime(raw.loc[missing], format="mixed", dayfirst=True, errors="coerce")
        except ValueError as exc:
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source="nse_recent_events.date",
                fallback_type="nse_recent_events_mixed_date_parser_failed",
                severity="info",
                reason="Pandas mixed date parser was unavailable or rejected NSE event-calendar dates; falling back to dayfirst parser.",
                deterministic_fallback=True,
                error=exc,
                metadata={
                    "sample_values": raw.loc[missing].head(5).astype(str).tolist(),
                    "fallback_parser": "pd.to_datetime(dayfirst=True)",
                },
            )
            parsed.loc[missing] = pd.to_datetime(raw.loc[missing], dayfirst=True, errors="coerce")
    return parsed


def parse_csv(csv_file):
    df = pd.read_csv(csv_file)
    df.columns = ['symbol', 'company', 'purpose', 'details', 'date']
    df['date'] = parse_event_dates(df['date'])
    bad_dates = df[df["date"].isna()]
    if not bad_dates.empty:
        sample = bad_dates["date"].head(5).astype(str).tolist()
        raise ValueError(f"Could not parse recent event dates: {sample}")
    df = df.drop(columns=["company"])
    df = df.drop_duplicates(subset=["date", "symbol", "purpose"], keep='first')
    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
    upsert_to_db(df, "nseindia_events", unique_keys=["date", "symbol", "purpose"])
    return df


def main() -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Download and parse NSE recent event calendar")
    parser.add_argument("--date", help="Optional event calendar date in YYYY-MM-DD; defaults to today")
    args = parser.parse_args()
    rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))
    rows_written = 0
    formatted_date = args.date or datetime.today().strftime("%Y-%m-%d")

    try:
        with sync_playwright() as p:
            result = dowload_events(p, formatted_date, rop)
            rows_written = int(result.get("rows") or 0)
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        classification = classify_recent_events_error(error_text)
        STOCKEY_RUN_STATE = build_run_state(
            formatted_date=formatted_date,
            rows_written=rows_written,
            classification=classification,
            error=error_text,
        )
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="nse_recent_events",
            fallback_type="nse_recent_events_sync_failed",
            severity="error",
            reason="NSE recent event-calendar sync failed; event-calendar evidence may be stale until this source reruns successfully.",
            error=exc,
            metadata={"event_date": formatted_date, "classification": classification},
        )
        persist_sync_state(
            source_name=SYNC_SOURCE_NAME,
            status="error",
            error_text=error_text,
            state=STOCKEY_RUN_STATE,
        )
        raise
    finally:
        rop.close()
    STOCKEY_RUN_STATE = build_run_state(
        formatted_date=formatted_date,
        rows_written=rows_written,
        classification="ok" if rows_written > 0 else "no_data",
    )
    persist_sync_state(
        source_name=SYNC_SOURCE_NAME,
        status="ok",
        last_success_at=pd.Timestamp.utcnow(),
        last_item_ts=pd.Timestamp(formatted_date),
        state=STOCKEY_RUN_STATE,
    )
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
