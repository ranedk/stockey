from datetime import date, timedelta
from io import StringIO
import requests
import urllib3
from bs4 import BeautifulSoup
import pandas as pd
import calendar
from datetime import date

from utils.http import hidden_inputs_to_dict, get_dynamic_headers


HEADERS = get_dynamic_headers()


def parse_table(table):
    rows = table.find_all("tr")[2:-2]

    # Track the final grid and cell occupancy
    grid = []
    span_map = {}

    for row_idx, row in enumerate(rows):
        grid_row = []
        col_idx = 0

        # Handle carried-over cells from rowspan
        while len(grid) <= row_idx:
            grid.append([])

        cells = row.find_all(["td", "th"])
        for cell in cells:
            rowspan = int(cell.get("rowspan", 1))
            colspan = int(cell.get("colspan", 1))
            text = cell.get_text(strip=True).replace("\xa0", "")

            # Find the next available column
            while col_idx < len(grid[row_idx]) and grid[row_idx][col_idx] != "":
                col_idx += 1

            for i in range(rowspan):
                for j in range(colspan):
                    r = row_idx + i
                    c = col_idx + j

                    while len(grid) <= r:
                        grid.append([])
                    while len(grid[r]) <= c:
                        grid[r].extend([""] * (c - len(grid[r]) + 1))

                    grid[r][c] = text
            col_idx += colspan

    # Pad all rows to equal length
    max_len = max(len(r) for r in grid)
    for r in grid:
        r.extend([""] * (max_len - len(r)))

    return pd.DataFrame(grid)


def get_last_date(year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def get_fpi_data(month, year):
    session = requests.Session()
    response = session.get('https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx', headers=HEADERS)
    cookies = session.cookies.get_dict()
    hidden = hidden_inputs_to_dict(response.content)

    last_date = get_last_date(year, month)
    data = {
        'hdnDate': last_date.strftime("%d-%b-%Y"),
        'HdnValexceldata': '',
        'hdnFlag': '',
        '__EVENTTARGET': 'btnSubmit1',
    }
    data = {**hidden, **data}

    response = requests.post('https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx', cookies=cookies, headers=HEADERS, data=data)
    return response.content


def table_to_df(table_html):
    soup = BeautifulSoup(table_html, "html.parser")
    tables = soup.select("table.tbls01")
    investments_table = tables[0]
    derivatives_table = tables[1]
    fii_investments_df = parse_table(investments_table)
    fii_derivatives_df = parse_table(derivatives_table)
    return fii_investments_df, fii_derivatives_df


html = get_fpi_data(4, 2018)
fii_investments_df, fii_derivatives_df = table_to_df(html)


