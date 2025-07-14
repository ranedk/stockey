import time
from datetime import date, timedelta
from io import StringIO
import requests
import urllib3
from bs4 import BeautifulSoup
import pandas as pd

from utils.http import hidden_inputs_to_dict, get_dynamic_headers


HEADERS = get_dynamic_headers()


def get_wpi_for_year(year):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    response = session.get('https://eaindustry.nic.in/default.asp', headers=HEADERS)
    cookies = session.cookies.get_dict()

    data = {
        'Fopt_wmy': 'M',
        'Fyear1': year,
        'Fcomm_name': 'All',
    }
    response = requests.post('https://eaindustry.nic.in/choose_item_201112.asp', cookies=cookies, headers=HEADERS, data=data)

    soup = BeautifulSoup(response.content, 'html.parser')

    results = []
    for li in soup.select('ul.ul-choose-item li'):
        cname_input = li.find('input', {'name': 'cname'})
        commname_input = li.find('input', {'name': 'commname'})

        if cname_input and commname_input:
            cname = cname_input['value']
            commname = commname_input['value']
            text = commname_input['value'].strip()
            results.append([cname, commname, text])

    important_items = [r for r in results if r[2].startswith("(")]

    for item in important_items:
        data = {
            'hfAntiCSRFToken': '',
            'cname': item[0],
            'commname': item[1],
        }

        response = requests.post('https://eaindustry.nic.in/display_data_201112.asp', cookies=cookies, headers=HEADERS, data=data)

        # Parse HTML
        soup = BeautifulSoup(response.content, 'html.parser')
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
        for i, month in enumerate(months):
            value = values[i]
            if value is not None:
                # Use 1st of each month as the date
                date_str = f"{year}-{i+1:02d}-01"
                date = pd.to_datetime(date_str)
                records.append({"date": date, "value": float(value)})

        df = pd.DataFrame(records)
        df['cname'] = item[0]
        df['name'] = item[1]
        print(df)
        time.sleep(1)


get_wpi_for_year('2015')


