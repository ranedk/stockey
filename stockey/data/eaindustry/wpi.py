import time
from datetime import date
import json

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup
from environs import Env

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.http import get_dynamic_headers
from utils.date import last_of_month

env = Env()
env.read_env()

HEADERS = get_dynamic_headers()
CATALOG_URL = "https://eaindustry.nic.in/default.asp"
CATALOG_POST_URL = "https://eaindustry.nic.in/choose_item_201112.asp"
ITEM_POST_URL = "https://eaindustry.nic.in/display_data_201112.asp"
MAX_ITEM_ATTEMPTS = 5
REQUEST_TIMEOUT = 120
REQUEST_SLEEP_SECONDS = 2.0
WPI_LOOKBACK_DAYS = max(env.int("EAINDUSTRY_WPI_LOOKBACK_DAYS", 365), 1)
SYNC_SOURCE_NAME = "data.eaindustry.wpi"
STOCKEY_RUN_STATE: dict[str, object] = {}


def _record_wpi_download_fallback(
    *,
    year: int,
    item: list[str],
    attempt: int,
    error: Exception,
) -> None:
    record_local_fallback_event(
        module="data.eaindustry.wpi",
        fallback_type="wpi_item_download_attempt_failed",
        source="eaindustry_wpi",
        severity="warn",
        reason=(
            "WPI item download failed on one retry attempt; the item will be retried and may remain "
            "stale if all attempts fail."
        ),
        error=error,
        metadata={
            "year": int(year),
            "cname": str(item[0]).strip() if item else "",
            "name": str(item[1]).strip() if len(item) > 1 else "",
            "attempt": int(attempt),
            "max_attempts": int(MAX_ITEM_ATTEMPTS),
        },
    )


def expected_month_count_for_year(year: int, today: date | None = None) -> int:
    today = today or date.today()
    if year < today.year:
        return 12
    if year > today.year:
        return 0
    return max(0, today.month - 1)


def load_completed_items(year: int, *, expected_months: int) -> set[str]:
    if expected_months <= 0:
        return set()
    df = sql_to_df(
        """
        SELECT cname
        FROM eaindustry_wpi
        WHERE date >= %s
          AND date <= %s
        GROUP BY cname
        HAVING COUNT(DISTINCT date) >= %s
        """,
        params=(
            pd.Timestamp(date(year, 1, 1)),
            pd.Timestamp(last_of_month(date(year, 12, 1))),
            int(expected_months),
        ),
    )
    if df.empty:
        return set()
    return {
        str(value).strip()
        for value in df["cname"].dropna().astype("string").tolist()
        if str(value).strip()
    }


def parse_catalog_items(html: bytes | str) -> list[list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[list[str]] = []
    seen: set[str] = set()

    for cname_input in soup.find_all("input", {"name": "cname"}):
        cname = str(cname_input.get("value") or "").strip()
        if not cname or cname in seen:
            continue

        commname_input = None
        for sibling in cname_input.find_all_next("input", {"name": "commname"}, limit=3):
            commname_input = sibling
            break
        if commname_input is None and cname_input.parent is not None:
            commname_input = cname_input.parent.find("input", {"name": "commname"})

        commname = str(commname_input.get("value") or "").strip() if commname_input else ""
        if not commname or not commname.startswith("("):
            continue

        results.append([cname, commname, commname])
        seen.add(cname)

    return results


def bootstrap_wpi_session() -> tuple[requests.Session, dict[str, str]]:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.get(CATALOG_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    return session, session.cookies.get_dict()


def fetch_wpi_catalog(year: int) -> tuple[requests.Session, dict[str, str], list[list[str]]]:
    session, cookies = bootstrap_wpi_session()
    response = session.post(
        CATALOG_POST_URL,
        cookies=cookies,
        headers=HEADERS,
        data={
            "Fopt_wmy": "M",
            "Fyear1": year,
            "Fcomm_name": "All",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    results = parse_catalog_items(response.content)
    return session, cookies, results


def download_wpi_item(
    *,
    year: int,
    item: list[str],
    session: requests.Session,
    cookies: dict[str, str],
) -> pd.DataFrame:
    response = session.post(
        ITEM_POST_URL,
        cookies=cookies,
        headers=HEADERS,
        data={
            "hfAntiCSRFToken": "",
            "cname": item[0],
            "commname": item[1],
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table", {"class": "tblWpiIndexWithBorder"})
    if table is None:
        raise ValueError(f"WPI table missing for {year} {item[1]}")

    header_row = table.find("tr", class_="tr-heading")
    value_row = table.find("tr", class_="tr-index")
    if header_row is None or value_row is None:
        raise ValueError(f"WPI rows missing for {year} {item[1]}")

    header_cells = header_row.find_all("td")
    months = [cell.get_text(strip=True) for cell in header_cells[1:]]
    data_row = value_row.find_all("td")
    row_year = int(data_row[0].get_text(strip=True))
    values = [cell.get_text(strip=True) or None for cell in data_row[1:]]

    records = []
    for index, _ in enumerate(months):
        value = values[index]
        if value is None:
            continue
        rdate = pd.to_datetime(last_of_month(date(row_year, index + 1, 1)))
        records.append({"date": rdate, "value": float(value), "cname": item[0], "name": item[1]})
    return pd.DataFrame(records)


def persist_wpi_item(df: pd.DataFrame, *, year: int, cname: str) -> None:
    if df.empty:
        return
    upsert_to_db(df, "eaindustry_wpi", unique_keys=["date", "cname"], timescaledb_column="date")


def sync_wpi_for_year(year: int, *, today: date | None = None) -> dict[str, object]:
    today = today or date.today()
    expected_months = expected_month_count_for_year(year, today=today)
    if expected_months <= 0:
        return {"year": year, "status": "skipped", "reason": "no_completed_months"}

    print(
        f"WPI year {year}: expecting {expected_months} completed month(s) from DB-backed progress",
        flush=True,
    )
    session, cookies, items = fetch_wpi_catalog(year)
    db_completed = load_completed_items(year, expected_months=expected_months)
    completed = {str(value).strip() for value in db_completed if str(value).strip()}
    print(
        f"WPI year {year}: catalog items={len(items)} completed_items={len(completed)}",
        flush=True,
    )

    if len(completed) >= len(items):
        return {
            "year": year,
            "status": "completed",
            "item_count": len(items),
            "downloaded_count": 0,
            "skipped_count": len(items),
            "failed_count": 0,
        }

    downloaded_count = 0
    skipped_count = 0
    attempt_count = 0
    retry_count = 0
    failed_attempt_count = 0
    failed: list[dict[str, object]] = []

    for position, item in enumerate(items, start=1):
        cname = str(item[0]).strip()
        if cname in completed:
            skipped_count += 1
            print(f"Done for {year}: {item[1]} (resume)", flush=True)
            continue

        print(f"Downloading WPI for {year} [{position}/{len(items)}]: {item[1]}", flush=True)
        last_error: Exception | None = None
        for attempt in range(1, MAX_ITEM_ATTEMPTS + 1):
            attempt_count += 1
            if attempt > 1:
                retry_count += 1
            try:
                df = download_wpi_item(year=year, item=item, session=session, cookies=cookies)
                persist_wpi_item(df, year=year, cname=cname)
                refreshed = load_completed_items(year, expected_months=expected_months)
                if cname in refreshed:
                    completed.add(cname)
                downloaded_count += 1
                last_error = None
                time.sleep(0.2)
                break
            except (requests.RequestException, ValueError) as exc:
                failed_attempt_count += 1
                last_error = exc
                _record_wpi_download_fallback(
                    year=year,
                    item=item,
                    attempt=attempt,
                    error=exc,
                )
                print(f"WPI retry {attempt}/{MAX_ITEM_ATTEMPTS} for {year} {item[1]}: {exc}", flush=True)
                time.sleep(REQUEST_SLEEP_SECONDS * attempt)
                session, cookies = bootstrap_wpi_session()
        if last_error is not None:
            print(f"WPI skip after {MAX_ITEM_ATTEMPTS} attempts for {year}: {item[1]} -> {last_error}", flush=True)
            failed.append({"cname": cname, "name": item[1], "error": str(last_error)})

    return {
        "year": year,
        "status": "ok" if not failed else "partial",
        "item_count": len(items),
        "downloaded_count": downloaded_count,
        "skipped_count": skipped_count,
        "failed_count": len(failed),
        "attempt_count": attempt_count,
        "retry_count": retry_count,
        "failed_attempt_count": failed_attempt_count,
        "source_unavailable_count": len(failed),
        "failed_items": failed,
    }


def sync_wpi() -> list[dict[str, object]]:
    today = date.today()
    earliest_date = today - pd.Timedelta(days=WPI_LOOKBACK_DAYS)
    start_year = earliest_date.year
    results: list[dict[str, object]] = []
    for year in range(start_year, today.year + 1):
        expected_months = expected_month_count_for_year(year, today=today)
        if expected_months <= 0:
            continue
        print(f"Checking for {last_of_month(date(year, expected_months, 1))}", flush=True)
        results.append(sync_wpi_for_year(year, today=today))
    return results


def build_run_state(results: list[dict[str, object]]) -> dict[str, object]:
    row_count = sum(int(item.get("downloaded_count") or 0) for item in results)
    skipped_count = sum(int(item.get("skipped_count") or 0) for item in results)
    failed_count = sum(int(item.get("failed_count") or 0) for item in results)
    item_count = sum(int(item.get("item_count") or 0) for item in results)
    attempt_count = sum(int(item.get("attempt_count") or 0) for item in results)
    retry_count = sum(int(item.get("retry_count") or 0) for item in results)
    failed_attempt_count = sum(int(item.get("failed_attempt_count") or 0) for item in results)
    years = [int(item["year"]) for item in results if item.get("year") is not None]
    failed_items = []
    for item in results:
        for failed in item.get("failed_items") or []:
            if isinstance(failed, dict):
                failed_items.append({"year": item.get("year"), **failed})
    return {
        "source": SYNC_SOURCE_NAME,
        "rows": row_count,
        "rows_read": item_count,
        "rows_written": row_count,
        "from_date": f"{min(years)}-01-01" if years else None,
        "to_date": f"{max(years)}-12-31" if years else None,
        "year_count": len(years),
        "years": years,
        "item_count": item_count,
        "downloaded_count": row_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "attempt_count": attempt_count,
        "download_attempts": attempt_count,
        "retry_count": retry_count,
        "failed_attempt_count": failed_attempt_count,
        "source_unavailable_count": failed_count,
        "failed_items": failed_items[:20],
        "fallback_used": False,
        "state_advanced": row_count > 0,
    }


def run() -> int:
    global STOCKEY_RUN_STATE
    results = sync_wpi()
    STOCKEY_RUN_STATE = build_run_state(results)
    failed_years = [item for item in results if int(item.get("failed_count") or 0) > 0]
    if failed_years:
        print(json.dumps({"status": "partial", "failed_years": failed_years, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    else:
        print(json.dumps({"status": "ok", "years": results, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
