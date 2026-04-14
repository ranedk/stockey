import calendar
import json
import time
from argparse import ArgumentParser
from datetime import date
from io import StringIO

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
from environs import Env

from utils.db import sql_to_df, upsert_to_db
from utils.http import get_dynamic_headers, get_with_retries, hidden_inputs_to_dict
from utils.sync import get_db_max_date

env = Env()
env.read_env()

HEADERS = get_dynamic_headers()
NEW_CPI_START = date(2025, 1, 1)
PAGE_LOG_INTERVAL = 10
MONTH_RETRY_ATTEMPTS = 3
MONTH_RETRY_SLEEP_SECONDS = 2
CPI_LOOKBACK_DAYS = max(env.int("MOSPI_CPI_LOOKBACK_DAYS", 365), 1)


def first_of_month(d: date) -> date:
    return d.replace(day=1)


def last_of_month(d: date) -> date:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=last_day)


def month_iter(start: date, stop: date):
    """Yield first-of-month dates from *start* through *stop* inclusive."""
    current = start
    while current <= stop:
        yield current
        current += relativedelta(months=1)


def month_label(month_start: date) -> str:
    return month_start.strftime("%Y-%m")


def load_existing_cpi_months() -> set[date]:
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT date_trunc('month', cpi_for_month)::date AS cpi_for_month
            FROM mospi_cpi
            WHERE cpi_for_month IS NOT NULL
            """
        )
    except Exception as exc:
        message = str(exc).lower()
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"} or "does not exist" in message:
            print(f"[cpi] mospi_cpi not available yet while checking existing months: {exc}", flush=True)
            return set()
        raise

    if df.empty or "cpi_for_month" not in df.columns:
        return set()

    values = pd.to_datetime(df["cpi_for_month"], errors="coerce").dropna()
    return {value.date().replace(day=1) for value in values}


def download_cpi_data(from_date: date, to_date: date):
    frames = []
    for month_start in month_iter(first_of_month(from_date), first_of_month(to_date)):
        print(f"[{month_label(month_start)}] Starting download")
        frame = download_cpi_month(month_start)
        if frame is not None and not frame.empty:
            frames.append(frame)
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def download_cpi_month(month_start: date):
    month_start = first_of_month(month_start)
    month_end = last_of_month(month_start)
    if month_start < NEW_CPI_START:
        return download_legacy_cpi_data(month_start, month_end)
    return download_modern_cpi_data(month_start, month_end)


def download_legacy_cpi_data(from_date: date, to_date: date):
    print(f"[{month_label(first_of_month(from_date))}] Legacy CPI request {from_date} -> {to_date}")
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    response = session.get("https://cpi.mospi.gov.in/", headers=HEADERS, verify=False)
    cookies = session.cookies.get_dict()

    response = requests.get(
        "https://cpi.mospi.gov.in/TimeSeries_2012.aspx",
        cookies=cookies,
        headers=HEADERS,
        verify=False,
    )
    hidden = hidden_inputs_to_dict(response.content)

    data = {
        "ctl00$Content1$DropDownList1": str(from_date.year),
        "ctl00$Content1$DropDownList3": from_date.strftime("%m"),
        "ctl00$Content1$CheckBoxList1$0": "99",
        "ctl00$Content1$DropDownList5": "27b",
        "ctl00$Content1$DropDownList8": "Group",
        "ctl00$Content1$Button2": "View Indices",
        "ctl00$Content1$DropDownList2": str(to_date.year),
        "ctl00$Content1$DropDownList4": to_date.strftime("%m"),
    }

    data.update(hidden)
    response = requests.post(
        "https://cpi.mospi.gov.in/TimeSeries_2012.aspx",
        cookies=cookies,
        headers=HEADERS,
        data=data,
        verify=False,
    )
    table_html = response.content
    df = table_to_df(table_html)
    upsert_to_db(
        df, "mospi_cpi", unique_keys=["cpi_for_month", "state", "group", "sub_group"]
    )
    print(f"[{month_label(first_of_month(from_date))}] Legacy CPI upserted {len(df)} rows")
    return df


def normalize_state_name(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.lower() == "all india":
        return "ALL India"
    return raw


def derive_group_code(value: str | None) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    first = text.split(".", 1)[0]
    try:
        return float(int(first))
    except ValueError:
        return 0.0


def month_name_to_number(value: str | None) -> int:
    text = str(value or "").strip()
    return pd.Timestamp(f"2000-{text}-01").month


def fetch_modern_cpi_page(*, page: int, years: list[int], months: list[int]) -> dict:
    headers = get_dynamic_headers()
    headers.update({"Origin": "https://esankhyiki.mospi.gov.in"})
    url = (
        "https://api.mospi.gov.in/api/cpi/getCPIData"
        f"?base_year=2024"
        f"&level=Group"
        f"&page={int(page)}"
        f"&limit=500"
        f"&series=Current"
        f"&year={','.join(str(year) for year in years)}"
        f"&sector_code=1,2,3"
        f"&month_code={','.join(str(month) for month in months)}"
        f"&isView=table"
    )
    response = get_with_retries(url, headers=headers)
    payload = json.loads(response.content)
    if not isinstance(payload, dict) or "data" not in payload:
        raise ValueError("Unexpected CPI API response shape")
    return payload


def should_log_page(page: int, total_pages: int) -> bool:
    return page == 1 or page == total_pages or page % PAGE_LOG_INTERVAL == 0


def modern_cpi_json_to_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=[
                "state",
                "group",
                "sub_group",
                "description",
                "rural",
                "urban",
                "combined",
                "status",
                "cpi_for_month",
                "reported_on",
            ]
        )

    df = pd.DataFrame(rows)
    df["state"] = df["state"].map(normalize_state_name)
    df["sector"] = df["sector"].astype("string").str.strip().str.lower()
    df["code"] = df["code"].astype("string")
    df["description"] = (
        df["item"]
        .fillna(df["sub_class"])
        .fillna(df["class"])
        .fillna(df["group"])
        .fillna(df["division"])
        .fillna("CPI (General)")
    )
    df["sub_group"] = df["code"].fillna("GENERAL").astype("string")
    df["group"] = df["sub_group"].map(derive_group_code)
    df["index"] = pd.to_numeric(df["index"], errors="coerce")
    df["month_num"] = df["month"].map(month_name_to_number)
    df["cpi_for_month"] = pd.to_datetime(
        dict(year=pd.to_numeric(df["year"], errors="coerce"), month=df["month_num"], day=1),
        utc=True,
        errors="coerce",
    )
    df["reported_on"] = (df["cpi_for_month"] + pd.DateOffset(months=1)).apply(
        lambda value: value.replace(day=12) if pd.notna(value) else value
    )
    df["status"] = "P"

    out = (
        df.pivot_table(
            index=["state", "group", "sub_group", "description", "status", "cpi_for_month", "reported_on"],
            columns="sector",
            values="index",
            aggfunc="first",
        )
        .reset_index()
    )
    out.columns.name = None
    for column in ["rural", "urban", "combined"]:
        if column not in out.columns:
            out[column] = pd.NA
    return out[
        [
            "state",
            "group",
            "sub_group",
            "description",
            "rural",
            "urban",
            "combined",
            "status",
            "cpi_for_month",
            "reported_on",
        ]
    ]


def download_modern_cpi_data(from_date: date, to_date: date) -> pd.DataFrame:
    month_start = first_of_month(from_date)
    years = sorted({month.year for month in month_iter(first_of_month(from_date), first_of_month(to_date))})
    months = sorted({month.month for month in month_iter(first_of_month(from_date), first_of_month(to_date))})
    print(f"[{month_label(month_start)}] Modern CPI request years={years} months={months} page=1")
    first_payload = fetch_modern_cpi_page(page=1, years=years, months=months)
    total_pages = int((first_payload.get("meta_data") or {}).get("totalPages") or 1)
    rows = list(first_payload.get("data") or [])
    print(f"[{month_label(month_start)}] Modern CPI fetched page 1/{total_pages} rows={len(rows)}")
    for page in range(2, total_pages + 1):
        payload = fetch_modern_cpi_page(page=page, years=years, months=months)
        rows.extend(payload.get("data") or [])
        if should_log_page(page, total_pages):
            print(
                f"[{month_label(month_start)}] Modern CPI progress page {page}/{total_pages} cumulative_rows={len(rows)}"
            )

    df = modern_cpi_json_to_df(rows)
    if df.empty:
        print(f"[{month_label(month_start)}] Modern CPI returned no rows")
        return df
    df = df[
        (df["cpi_for_month"].dt.date >= first_of_month(from_date))
        & (df["cpi_for_month"].dt.date <= first_of_month(to_date))
    ].reset_index(drop=True)
    upsert_to_db(
        df, "mospi_cpi", unique_keys=["cpi_for_month", "state", "group", "sub_group"]
    )
    print(f"[{month_label(month_start)}] Modern CPI upserted {len(df)} rows")
    return df


def table_to_df(table_html):
    soup = BeautifulSoup(table_html, "html.parser")
    tables = soup.select("table.tableView")
    last_table = tables[-1]
    df_list = pd.read_html(StringIO(str(last_table)))
    df = df_list[0]

    # Create a 'cpi_for_month' date from Year and Month
    df["cpi_for_month"] = pd.to_datetime(
        df["Year"].astype(str) + "-" + df["Month"] + "-01"
    )

    # Create 'reported_on' as 12th of the next month
    df["reported_on"] = df["cpi_for_month"] + pd.DateOffset(months=1)
    df["reported_on"] = df["reported_on"].apply(lambda d: d.replace(day=12))

    df = df.iloc[:, 2:]
    df.columns = [
        "state",
        "group",
        "sub_group",
        "description",
        "rural",
        "urban",
        "combined",
        "status",
        "cpi_for_month",
        "reported_on",
    ]
    return df


def sync_cpi_data(
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    force: bool = False,
):
    """
    1. Build the list of months in scope.
    2. Check which months already exist in PostgreSQL.
    3. Download only missing months, one month at a time.
    """
    today = date.today()
    last_month = first_of_month(to_date or (today - relativedelta(months=1)))
    start_month = first_of_month(from_date or (today - relativedelta(days=CPI_LOOKBACK_DAYS)))

    all_months = list(month_iter(start_month, last_month))
    existing_months = load_existing_cpi_months()
    latest_db_month = get_db_max_date("mospi_cpi", date_column="cpi_for_month")
    missing_months = all_months if force else [m for m in all_months if m not in existing_months]

    if not missing_months:
        print("CPI data is already up-to-date ✅")
        return

    print(
        "CPI sync plan:",
        {
            "start_month": start_month.isoformat(),
            "last_month": last_month.isoformat(),
            "latest_db_month": latest_db_month.date().isoformat() if latest_db_month is not None else None,
            "existing_month_count": len(existing_months),
            "pending_month_count": len(missing_months),
            "mode": "force" if force else "resume",
        },
    )

    for index, month_start in enumerate(missing_months, start=1):
        completed = False
        for attempt in range(1, MONTH_RETRY_ATTEMPTS + 1):
            try:
                print(
                    f"[{index}/{len(missing_months)}] Downloading CPI for {month_label(month_start)} "
                    f"(attempt {attempt}/{MONTH_RETRY_ATTEMPTS})"
                )
                download_cpi_month(month_start)
                print(f"[{index}/{len(missing_months)}] Completed CPI for {month_label(month_start)}")
                completed = True
                break
            except KeyboardInterrupt:
                print(
                    f"Interrupted while downloading CPI for {month_label(month_start)}. "
                    "Completed months are already persisted; rerun to resume."
                )
                raise
            except Exception as exc:
                print(
                    f"[{index}/{len(missing_months)}] Failed CPI for {month_label(month_start)} "
                    f"on attempt {attempt}/{MONTH_RETRY_ATTEMPTS}: {exc}"
                )
                if attempt == MONTH_RETRY_ATTEMPTS:
                    break
                time.sleep(MONTH_RETRY_SLEEP_SECONDS)
        if not completed:
            break


def parse_args():
    parser = ArgumentParser(description="Sync MOSPI CPI data month-by-month.")
    parser.add_argument("--from-date", type=date.fromisoformat, default=None)
    parser.add_argument("--to-date", type=date.fromisoformat, default=None)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sync_cpi_data(from_date=args.from_date, to_date=args.to_date, force=args.force)
