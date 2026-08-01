from datetime import date, datetime, timedelta
import json

import pandas as pd
import requests
from bs4 import BeautifulSoup
from environs import Env

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import upsert_to_db
from utils.http import get_dynamic_headers, hidden_inputs_to_dict
from utils.parsers import table_to_grid
from . import fpi_utils as futils

env = Env()
env.read_env()

HEADERS = get_dynamic_headers()
FPI_LOOKBACK_DAYS = max(env.int("NSDL_FPI_LOOKBACK_DAYS", 365), 1)
SYNC_SOURCE_NAME = "data.nsdl.fpi"
STOCKEY_RUN_STATE: dict[str, object] = {}


def get_fpi_data(rdate: date):
    print(f"Fetching FPI data for date: {rdate}")
    session = requests.Session()
    response = session.get(
        "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx", headers=HEADERS
    )
    cookies = session.cookies.get_dict()
    hidden = hidden_inputs_to_dict(response.content)

    data = {
        "hdnDate": rdate.strftime("%d-%b-%Y"),
        "HdnValexceldata": "",
        "hdnFlag": "",
        "__EVENTTARGET": "btnSubmit1",
    }
    data = {**hidden, **data}

    response = requests.post(
        "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx",
        cookies=cookies,
        headers=HEADERS,
        data=data,
        timeout=120,
    )
    html = response.content
    fii_investments_df, fii_derivatives_df = parse_html_to_dfs(html)
    upsert_to_db(
        fii_investments_df,
        "fii_investments",
        unique_keys=["date", "instrument"],
        timescaledb_column="date",
    )
    upsert_to_db(
        fii_derivatives_df,
        "fii_derivatives",
        unique_keys=["date", "instrument"],
        timescaledb_column="date",
    )
    return {
        "date": rdate.isoformat(),
        "investment_rows": int(len(fii_investments_df)),
        "derivative_rows": int(len(fii_derivatives_df)),
        "rows": int(len(fii_investments_df) + len(fii_derivatives_df)),
    }


def filter_bad_rows(df):
    """
    Filter out rows that are not valid dates or totals.
    """
    first_col = df.columns[0]
    date_pat = r"^\d{2}-[A-Za-z]{3}-\d{4}$"
    valid_mask = df[first_col].astype(str).str.match(
        date_pat
    ) | df[  # rows that look like dates
        first_col
    ].astype(
        str
    ).str.startswith(
        "Total for"
    )  # rows that start with 'Total for'
    return df[valid_mask].copy()


def coerce_numeric_columns(df, columns):
    for col in columns:
        cleaned = df[col].astype("string").str.replace(",", "", regex=False)
        df[col] = pd.to_numeric(cleaned, errors="coerce")
    return df


def parse_investments_table(table_html):
    grid = table_to_grid(table_html)
    grid_df = pd.DataFrame(grid)
    grid_df = grid_df.iloc[3:-1]

    df = filter_bad_rows(grid_df)
    df = df.iloc[:, :8]

    df.columns = [
        "date",
        "itype",
        "iroute",
        "gross_purchases_inr_crore",
        "gross_sales_inr_crore",
        "net_investment_inr_crore",
        "net_investment_usd_million",
        "usd_inr_rate",
    ]
    df["instrument"] = (
        df["itype"].apply(futils.to_snake) + "_" + df["iroute"].apply(futils.to_snake)
    )
    df["instrument"] = df["instrument"].apply(
        lambda x: "total" if x == "debt_total" else x
    )
    df = coerce_numeric_columns(
        df,
        [
            "gross_purchases_inr_crore",
            "gross_sales_inr_crore",
            "net_investment_inr_crore",
            "net_investment_usd_million",
            "usd_inr_rate",
        ],
    )
    df.drop(columns=["itype", "iroute", "usd_inr_rate"], inplace=True)
    df = futils.fix_date(df)
    return df


def parse_derivatives_table(table_html):
    grid = table_to_grid(table_html)
    grid_df = pd.DataFrame(grid)
    grid_df = grid_df.iloc[4:-1]

    df = filter_bad_rows(grid_df)
    df = df.iloc[:, :8]

    df.columns = [
        "date",
        "instrument",
        "buy_number_of_contracts",
        "buy_amount",
        "sell_number_of_contracts",
        "sell_amount",
        "open_interest_eod_number_of_contracts",
        "open_interest_eod_amount",
    ]
    df["instrument"] = df["instrument"].apply(futils.to_snake)
    df = coerce_numeric_columns(
        df,
        [
            "buy_number_of_contracts",
            "buy_amount",
            "sell_number_of_contracts",
            "sell_amount",
            "open_interest_eod_number_of_contracts",
            "open_interest_eod_amount",
        ],
    )
    df = futils.fix_date(df)
    return df


def parse_html_to_dfs(table_html):
    soup = BeautifulSoup(table_html, "html.parser")
    tables = soup.select("table.tbls01")
    investments_table = tables[0]
    derivatives_table = tables[1]
    fii_investments_df = parse_investments_table(investments_table)
    fii_derivatives_df = parse_derivatives_table(derivatives_table)
    return fii_investments_df, fii_derivatives_df

def latest_downloaded_date(today: date) -> date | None:
    _, latest_db_date = futils.downloaded_for(today)
    return latest_db_date


def update_fpi_data(*, today: date | None = None) -> dict[str, object]:
    today = today or date.today()
    latest_done = latest_downloaded_date(today)
    if latest_done is not None:
        year, month = latest_done.year, latest_done.month
    else:
        start_date = today - timedelta(days=FPI_LOOKBACK_DAYS)
        year, month = start_date.year, start_date.month

    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "from_date": date(year, month, 1).isoformat(),
        "to_date": today.isoformat(),
        "month_count": 0,
        "downloaded_month_count": 0,
        "skipped_month_count": 0,
        "failed_month_count": 0,
        "attempt_count": 0,
        "download_attempts": 0,
        "retry_count": 0,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_used": False,
        "state_advanced": False,
    }

    while (year, month) <= (today.year, today.month):
        last_dom = futils.get_last_date(year, month).day
        target_date = date(year, month, last_dom)
        target_date = min(target_date, today)
        state["month_count"] = int(state["month_count"]) + 1

        found, _ = futils.downloaded_for(target_date)
        if found:
            state["skipped_month_count"] = int(state["skipped_month_count"]) + 1
        else:
            state["attempt_count"] = int(state["attempt_count"]) + 1
            state["download_attempts"] = int(state["download_attempts"]) + 1
            try:
                result = get_fpi_data(target_date) or {}
                rows = int(result.get("rows") or 0) if isinstance(result, dict) else 0
                state["rows"] = int(state["rows"]) + rows
                state["rows_written"] = int(state["rows_written"]) + rows
                state["downloaded_month_count"] = int(state["downloaded_month_count"]) + 1
                state["state_advanced"] = bool(state["state_advanced"]) or rows > 0
            except Exception as exc:
                state["failed_attempt_count"] = int(state["failed_attempt_count"]) + 1
                state["failed_month_count"] = int(state["failed_month_count"]) + 1
                state["source_unavailable_count"] = int(state["source_unavailable_count"]) + 1
                record_local_fallback_event(
                    module=SYNC_SOURCE_NAME,
                    source="nsdl_fpi",
                    fallback_type="nsdl_fpi_month_download_failed",
                    severity="warn",
                    reason="NSDL FPI monthly catch-up failed for one target month and stopped the current run so a later catch-up can retry.",
                    error=exc,
                    metadata={"target_date": target_date.isoformat(), "year": year, "month": month},
                )
                failed_months = state.setdefault("failed_months", [])
                if isinstance(failed_months, list):
                    failed_months.append({"date": target_date.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
                break

        # move to next month
        month += 1
        if month == 13:
            month = 1
            year += 1
    state["rows_read"] = int(state["month_count"])
    return state


def main() -> int:
    global STOCKEY_RUN_STATE
    # get_fpi_data(date(2014, 1, 31))
    STOCKEY_RUN_STATE = update_fpi_data()
    status = "partial" if int(STOCKEY_RUN_STATE.get("failed_month_count") or 0) else "ok"
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
