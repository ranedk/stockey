from datetime import date, timedelta
from io import StringIO
import requests
import urllib3
from bs4 import BeautifulSoup
import pandas as pd

from utils.http import hidden_inputs_to_dict, get_dynamic_headers


HEADERS = get_dynamic_headers()


def get_cookies_and_data(from_date: date, to_date: date):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    response = session.get('https://cpi.mospi.gov.in/', headers=HEADERS, verify=False)
    cookies = session.cookies.get_dict()

    response = requests.get('https://cpi.mospi.gov.in/TimeSeries_2012.aspx', cookies=cookies, headers=HEADERS, verify=False)
    hidden = hidden_inputs_to_dict(response.content)

    data = {
        'ctl00$Content1$DropDownList1': str(from_date.year),
        'ctl00$Content1$DropDownList3': from_date.strftime("%m"),
        'ctl00$Content1$CheckBoxList1$0': '99',
        'ctl00$Content1$DropDownList5': '27b',
        'ctl00$Content1$DropDownList8': 'Group',
        'ctl00$Content1$Button2': 'View Indices',
        'ctl00$Content1$DropDownList2': str(to_date.year),
        'ctl00$Content1$DropDownList4': to_date.strftime("%m"),
    }

    data.update(hidden)
    response = requests.post('https://cpi.mospi.gov.in/TimeSeries_2012.aspx', cookies=cookies, headers=HEADERS, data=data, verify=False)
    return response.content


def table_to_df(table_html):
    soup = BeautifulSoup(table_html, "html.parser")
    tables = soup.select("table.tableView")
    last_table = tables[-1]
    df_list = pd.read_html(StringIO(str(last_table)))
    df = df_list[0]

    # Create a 'cpi_for_month' date from Year and Month
    df["cpi_for_month"] = pd.to_datetime(df["Year"].astype(str) + "-" + df["Month"] + "-01")

    # Create 'reported_on' as 12th of the next month
    df["reported_on"] = df["cpi_for_month"] + pd.DateOffset(months=1)
    df["reported_on"] = df["reported_on"].apply(lambda d: d.replace(day=12))

    return df

table_html = get_cookies_and_data(date(2024,1,1), date(2025,1,1))
from IPython import embed
embed()
df = table_to_df(table_html)




d = """

response = requests.post('https://cpi.mospi.gov.in/TimeSeries_2012.aspx', cookies=cookies, headers=headers, data=data)
"""
