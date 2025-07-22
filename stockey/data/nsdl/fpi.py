from datetime import date, datetime, timedelta

import pandas as pd
import redis
import requests
from bs4 import BeautifulSoup
from environs import Env

from utils.db import upsert_to_db
from utils.http import get_dynamic_headers, hidden_inputs_to_dict
from utils.parsers import table_to_grid

from . import fpi_utils as futils

env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "nsdl:fpi:downloaded"
HEADERS = get_dynamic_headers()


def get_fpi_data(date: date):
    print(f"Fetching FPI data for date: {date}")
    session = requests.Session()
    response = session.get(
        "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx", headers=HEADERS
    )
    cookies = session.cookies.get_dict()
    hidden = hidden_inputs_to_dict(response.content)

    data = {
        "hdnDate": date.strftime("%d-%b-%Y"),
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
        unique_keys=["reporting_date", "instrument"],
    )
    upsert_to_db(
        fii_derivatives_df,
        "fii_derivatives",
        unique_keys=["reporting_date", "instrument"],
    )


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


def parse_investments_table(table_html):
    grid = table_to_grid(table_html)
    grid_df = pd.DataFrame(grid)
    grid_df = grid_df.iloc[3:-1]

    df = filter_bad_rows(grid_df)
    df = df.iloc[:, :8]

    df.columns = [
        "reporting_date",
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
    df.drop(columns=["itype", "iroute", "usd_inr_rate"], inplace=True)
    df = futils.fix_reporting_date(df)
    return df


def parse_derivatives_table(table_html):
    grid = table_to_grid(table_html)
    grid_df = pd.DataFrame(grid)
    grid_df = grid_df.iloc[4:-1]

    df = filter_bad_rows(grid_df)
    df = df.iloc[:, :8]

    df.columns = [
        "reporting_date",
        "instrument",
        "buy_number_of_contracts",
        "buy_amount",
        "sell_number_of_contracts",
        "sell_amount",
        "open_interest_eod_number_of_contracts",
        "open_interest_eod_amount",
    ]
    df["instrument"] = df["instrument"].apply(futils.to_snake)
    df = futils.fix_reporting_date(df)
    return df


def parse_html_to_dfs(table_html):
    soup = BeautifulSoup(table_html, "html.parser")
    tables = soup.select("table.tbls01")
    investments_table = tables[0]
    derivatives_table = tables[1]
    fii_investments_df = parse_investments_table(investments_table)
    fii_derivatives_df = parse_derivatives_table(derivatives_table)
    return fii_investments_df, fii_derivatives_df


def mark_till_date(rop, end_date):
    if end_date and isinstance(end_date, datetime):
        end_date = end_date.date()
    start = end_date.replace(day=1)
    days = (end_date - start).days
    for i in range(days + 1):
        rop.sadd(REDIS_SET, (start + timedelta(days=i)).strftime("%Y-%m-%d"))


def update_fpi_data():
    rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    today = date.today()
    year, month = 2014, 1

    while (year, month) <= (today.year, today.month):
        last_dom = futils.get_last_date(year, month).day
        target_date = date(year, month, last_dom)
        target_date = min(target_date, today)

        found = False

        if not found:
            get_fpi_data(target_date)
            mark_till_date(rop, target_date)

        # move to next month
        month += 1
        if month == 13:
            month = 1
            year += 1


if __name__ == "__main__":
    # get_fpi_data(date(2014, 1, 31))
    update_fpi_data()
