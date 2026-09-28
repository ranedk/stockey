"""RBI's register of NBFCs -> fundamentals_rbi_nbfc_registry.

For the universe rebuild's groups (docs/UNIVERSE_PRD.md section 4). The exchange label
says "NBFC" or "Holding Company"; the regulator's register says what the company is
licensed as: ICC (investment and credit company, the ordinary lender), HFC (housing
finance), MFI (microfinance), IFC (infrastructure finance), CIC (core investment
company: a registered holding company), and a few smaller types. Checked 2026-09-25:
580 of ~8,560 entries are listed companies (CIN starting "L"), including JIOFIN, TVS
Holdings and Cholamandalam Financial Holdings as CIC.

rbidocs.rbi.org.in answers plain requests with a bot-check page, so the XLSX is fetched
through the shared Chrome (utils.cdp). RBI refreshes the file roughly quarterly; each
load is stored under the file's own "as on" date, so older snapshots stay readable.

    python -m fundamentals.collectors.rbi_nbfc_registry
"""

from __future__ import annotations

import io
import json
import re

import pandas as pd

from utils.db import sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.collectors.rbi_nbfc_registry"
RESULTS_TABLE = "fundamentals_rbi_nbfc_registry"
XLSX_URL = "https://rbidocs.rbi.org.in/rdocs/content/DOCs/List_of_NBFCs_and_ARCs_registered_with_the_RBI.XLSX"
WARMUP_URL = "https://rbidocs.rbi.org.in/rdocs/content/DOCs/"
SHEET = "List of NBFCs"
STOCKEY_RUN_STATE: dict[str, object] = {}

_COLUMNS = {
    "NBFC Name": "nbfc_name",
    "Regional Office": "regional_office",
    "Whether have CoR for holding/ Accepting Public Deposits": "deposit_taking",
    "Classification": "classification",
    "Corporate Identification Number": "cin",
    "Layer": "layer",
}


def download_xlsx() -> bytes:
    from playwright.sync_api import sync_playwright

    from utils.cdp import connect_over_cdp

    with sync_playwright() as p:
        browser = connect_over_cdp(p, "http://localhost:9222", caller=SYNC_SOURCE_NAME)
        page = browser.contexts[0].new_page()
        try:
            page.goto(WARMUP_URL, timeout=60000)
            page.wait_for_timeout(5000)  # the bot check sets its cookie from JS
            resp = page.request.get(XLSX_URL, timeout=120000)
            body = resp.body()
        finally:
            page.close()
    if resp.status != 200 or body[:2] != b"PK":
        raise RuntimeError(f"RBI NBFC list: HTTP {resp.status}, not an XLSX (bot check not passed?)")
    return body


def parse_xlsx(content: bytes) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(content), sheet_name=SHEET, header=None)
    title = str(raw.iat[0, 0])
    m = re.search(r"as on ([A-Za-z]+ \d{1,2}, \d{4})", title)
    if not m:
        raise ValueError(f"RBI NBFC list: no 'as on' date in title {title!r}")
    as_of = pd.to_datetime(m.group(1)).date()
    df = pd.read_excel(io.BytesIO(content), sheet_name=SHEET, header=1)
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in _COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"RBI NBFC list: missing columns {missing}")
    df = df[list(_COLUMNS)].rename(columns=_COLUMNS)
    for c in df.columns:
        df[c] = df[c].astype("string").str.strip()
    df = df.dropna(subset=["nbfc_name", "cin"])
    df["classification"] = df["classification"].str.rstrip("*")  # "ICC*" footnote marker
    df["deposit_taking"] = df["deposit_taking"].str.lower().eq("yes")
    df["is_listed"] = df["cin"].str.upper().str.startswith("L")
    df["as_of_date"] = as_of
    return df.drop_duplicates(["as_of_date", "cin"], keep="last").reset_index(drop=True)


def _ensure_as_of_date_type() -> None:
    """upsert_to_db infers a Python date as TEXT on table creation (the repo's known trap;
    fundamentals_sector_reference hit it first). Converted once, right after the first write."""
    from utils.schema_migrations import apply_schema_migration

    apply_schema_migration(
        migration_id="20260928_fundamentals_rbi_nbfc_registry_as_of_date_date",
        description="fundamentals_rbi_nbfc_registry.as_of_date TEXT -> DATE.",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": [RESULTS_TABLE]},
        statements=[f"ALTER TABLE {RESULTS_TABLE} ALTER COLUMN as_of_date TYPE DATE USING as_of_date::date"],
    )


def load_latest_registry() -> pd.DataFrame:
    return sql_to_df(
        f"SELECT * FROM {RESULTS_TABLE} WHERE as_of_date = (SELECT max(as_of_date) FROM {RESULTS_TABLE})"
    )


def main() -> int:
    global STOCKEY_RUN_STATE
    df = parse_xlsx(download_xlsx())
    df["load_ts"] = pd.Timestamp.now(tz="UTC")
    upsert_to_db(df, RESULTS_TABLE, unique_keys=["as_of_date", "cin"])
    _ensure_as_of_date_type()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": len(df),
        "rows_written": len(df),
        "as_of_date": str(df["as_of_date"].iloc[0]),
        "listed": int(df["is_listed"].sum()),
        "by_classification_listed": df.loc[df["is_listed"], "classification"].value_counts().to_dict(),
        "fallback_used": False,
        "state_advanced": True,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
