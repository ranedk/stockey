import time
from datetime import date

import pandas as pd
import redis
import requests
import urllib3
from bs4 import BeautifulSoup
from environs import Env

from utils.db import upsert_to_db
from utils.sync import get_db_max_date
from utils.http import get_dynamic_headers
from utils.date import last_of_month

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "wpi:downloaded"
rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


HEADERS = get_dynamic_headers()


def get_wpi_for_year(year):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    response = session.get("https://eaindustry.nic.in/default.asp", headers=HEADERS)
    cookies = session.cookies.get_dict()

    data = {
        "Fopt_wmy": "M",
        "Fyear1": year,
        "Fcomm_name": "All",
    }
    response = requests.post(
        "https://eaindustry.nic.in/choose_item_201112.asp",
        cookies=cookies,
        headers=HEADERS,
        data=data,
        timeout=120
    )

    soup = BeautifulSoup(response.content, "html.parser")

    results = []
    for li in soup.select("ul.ul-choose-item li"):
        cname_input = li.find("input", {"name": "cname"})
        commname_input = li.find("input", {"name": "commname"})

        if cname_input and commname_input:
            cname = cname_input["value"]
            commname = commname_input["value"]
            text = commname_input["value"].strip()
            results.append([cname, commname, text])

    important_items = [r for r in results if r[2].startswith("(")]

    for item in important_items:
        print(f"Downloading WPI for {year}:  {item[1]}")
        data = {
            "hfAntiCSRFToken": "",
            "cname": item[0],
            "commname": item[1],
        }

        response = requests.post(
            "https://eaindustry.nic.in/display_data_201112.asp",
            cookies=cookies,
            headers=HEADERS,
            data=data,
            timeout=120
        )

        # Parse HTML
        soup = BeautifulSoup(response.content, "html.parser")
        table = soup.find("table", {"class": "tblWpiIndexWithBorder"})

        # Get month names from header row
        header_cells = table.find("tr", class_="tr-heading").find_all("td")
        months = [cell.get_text(strip=True) for cell in header_cells[1:]]

        # Get data row
        data_row = table.find("tr", class_="tr-index").find_all("td")
        year = data_row[0].get_text(strip=True)
        values = [cell.get_text(strip=True) or None for cell in data_row[1:]]

        # Create date and value pairs
        records = []
        for i, _ in enumerate(months):
            value = values[i]
            if value is not None:
                # Use 1st of each month as the date
                rdate = last_of_month(date(int(year), i+1, 1))
                rdate = pd.to_datetime(rdate)
                rop.sadd(REDIS_SET, rdate.strftime("%Y-%m-%d"))
                records.append({"date": rdate, "value": float(value)})

        df = pd.DataFrame(records)
        df["cname"] = item[0]
        df["name"] = item[1]
        upsert_to_db(df, "eaindustry_wpi", unique_keys=["date", "cname"], timescaledb_column="date")
        time.sleep(0.2)


def sync_wpi():
    today = date.today()
    latest_db_date = get_db_max_date("eaindustry_wpi")
    for year in range(2014, today.year + 1):
        for month in range(1, 13):
            if year == today.year and month >= today.month:
                continue
            rdate = last_of_month(date(year, month, 1))
            if latest_db_date is not None and rdate <= latest_db_date.date():
                rop.sadd(REDIS_SET, rdate.strftime("%Y-%m-%d"))
                print(f"Done for {rdate} (db)")
                continue
            if not rop.sismember(REDIS_SET, rdate.strftime("%Y-%m-%d")):
                print(f"Checking for {rdate}")
                get_wpi_for_year(year)
            else:
                print(f"Done for {rdate}")


def run():
    # To setup the first set of tables
    # get_wpi_for_year(2014)
    try:
        sync_wpi()
    except requests.exceptions.ConnectionError as e:
        time.sleep(20)
        HEADERS = get_dynamic_headers()
        print(f"Error: {e}")
        run()


if __name__ == "__main__":
    run()
