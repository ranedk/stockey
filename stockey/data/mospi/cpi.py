import calendar
from datetime import date
from io import StringIO

import pandas as pd
import redis
import requests
import urllib3
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_dynamic_headers, hidden_inputs_to_dict

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "cpi:downloaded"
rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

HEADERS = get_dynamic_headers()


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


def chunk_consecutive(months: list[date], max_months: int = 12):
    """
    Group an ordered list of month starts into runs that are
    (1) consecutive and (2) no longer than *max_months*.
    Yields (run_start, run_end) pairs where run_end is **last day** of month.
    """
    i = 0
    while i < len(months):
        run_start = months[i]
        j = i
        # extend while next month is exactly +1 and length ≤ max_months
        while (
            j + 1 < len(months)
            and months[j + 1] == months[j] + relativedelta(months=1)
            and (j + 1) - i + 1 <= max_months
        ):
            j += 1
        run_end = last_of_month(months[j])
        yield run_start, run_end
        i = j + 1


def download_cpi_data(from_date: date, to_date: date):
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


def sync_cpi_data():
    """
    1. Build the list of months from ten years ago up to *last* month.
    2. Ask Redis which months are missing.
    3. Download the gaps in ≤12-month chunks.
    4. Mark the months as present in Redis once the download succeeds.
    """
    today = date.today()
    last_month = first_of_month(today - relativedelta(months=1))
    start_month = first_of_month(today - relativedelta(years=10))

    # Build the complete month list and filter out those already present
    all_months = list(month_iter(start_month, last_month))
    month_keys = [m.isoformat() for m in all_months]
    have = {k for k in month_keys if rop.sismember(REDIS_SET, k)}
    missing_months = [m for m in all_months if m.isoformat() not in have]

    if not missing_months:
        print("CPI data is already up-to-date ✅")
        return

    for run_start, run_end in chunk_consecutive(missing_months):
        try:
            print("Downloading CPI %s → %s", run_start, run_end)
            download_cpi_data(run_start, run_end)

            new_members = [
                m.isoformat() for m in month_iter(run_start, first_of_month(run_end))
            ]
            rop.sadd(REDIS_SET, *new_members)
        except Exception:
            print("Failed to download CPI for %s → %s", run_start, run_end)
            break


if __name__ == "__main__":
    sync_cpi_data()
