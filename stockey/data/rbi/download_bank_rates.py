# Download data from RBI website – sync version
import numpy as np
import pandas as pd
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from utils.db import upsert_to_db

CDP_ENDPOINT = "http://localhost:9222"


def parse_excel_file(file_path: str) -> pd.DataFrame:
    xls = pd.ExcelFile(file_path)
    sheet_names = xls.sheet_names

    preview_df = xls.parse(sheet_names[0], header=None, nrows=20)
    header_rows = preview_df.iloc[5:8].fillna("")
    combined_headers = (
        header_rows.astype(str).agg(" ".join).str.strip().replace("", np.nan)
    )

    data_df = xls.parse(sheet_names[0], header=None, skiprows=8)
    data_df.columns = combined_headers.values
    data_df.dropna(how="all", inplace=True)

    column_index_rename_map = {
        0: "",
        1: "effective_date",
        2: "bank_rate",
        3: "repo_rate",
        4: "reverse_repo_rate",
        5: "sdf_rate",
        6: "msf_rate",
        7: "crr",
        8: "slr",
    }

    data_df = data_df.iloc[:-1]
    data_df.columns = [
        column_index_rename_map.get(i, col) for i, col in enumerate(data_df.columns)
    ]
    data_df = data_df.drop(columns=data_df.columns[0])
    data_df.replace("-", np.nan, inplace=True)
    return data_df


def download_latest_rates(playwright) -> bool:
    """
    Automate RBI website's download using the sync Playwright API.
    """
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    rates_page = None

    try:
        page.goto("https://data.rbi.org.in/DBIE/#/dbie/home")
        page.wait_for_timeout(10_000)

        page.get_by_role("link", name="Indicators", exact=True).click()
        page.wait_for_timeout(2_000)

        page.locator("a").filter(has_text="Financial Sector Indicators").click()
        page.wait_for_timeout(2_000)

        with page.expect_popup() as popup_info:
            page.get_by_text("Key Rates").click()

        rates_page = popup_info.value
        rates_page.wait_for_timeout(10_000)
        frame = rates_page.frame(name="openDocChildFrame")

        frame.click("#__button60")
        rates_page.wait_for_timeout(3_000)

        with rates_page.expect_download(timeout=15_000) as dl_info:
            frame.get_by_role("button", name="Export", exact=True).click()

        download = dl_info.value
        file_path = download.path()
        df = parse_excel_file(file_path)
        df["date"] = pd.to_datetime(df["effective_date"], format="%d-%m-%Y")
        df = df.drop(columns=["effective_date"])
        for col in ["bank_rate", "repo_rate", "reverse_repo_rate", "sdf_rate", "msf_rate", "crr", "slr"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        upsert_to_db(df, "rbi_bank_rates", unique_keys=["date"], timescaledb_column="date")
        print(f"RBI bank rates updated: {len(df)} rows")
        return True
    except (PlaywrightTimeoutError, PlaywrightError, Exception) as exc:
        print(f"RBI bank rates download skipped: {exc}")
        return False
    finally:
        if rates_page is not None:
            rates_page.close()
        page.close()
        browser.close()


if __name__ == "__main__":
    with sync_playwright() as p:
        download_latest_rates(p)
