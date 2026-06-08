import argparse
import random
from datetime import datetime
from typing import List
from urllib.parse import urlencode

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from advisory.sync_state import persist_sync_state
from data.dhanlive.dhan_db import get_nse_equity
from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.sync import choose_from_date, get_db_max_date, get_redis_client, get_redis_cursor, load_tracked_symbols, normalize_date_window, parse_datetime_arg, set_redis_cursor

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env.int("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:corporate_actions"
SYNC_SOURCE_NAME = "data.nseindia.corporate_actions"


def get_random(min_ms: int, max_ms: int) -> int:
    return int(random.uniform(min_ms, max_ms))


def fetch_corporate_actions(page, symbol: str, issuer: str, from_date: datetime, to_date: datetime) -> pd.DataFrame:
    page.goto("https://www.nseindia.com/companies-listing/corporate-filings-actions")
    page.wait_for_timeout(get_random(1000, 2000))

    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
        "symbol": symbol,
        "issuer": issuer,
    }
    url = f"https://www.nseindia.com/api/corporates-corporateActions?{urlencode(params)}"
    data = page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'same-origin' });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return await res.json();
        }""",
        url,
    )

    df = pd.DataFrame(data)
    if df.empty:
        return df

    rename_map = {
        "symbol": "symbol",
        "series": "series",
        "faceVal": "face_value",
        "subject": "subject",
        "exDate": "date",
        "recDate": "record_date",
        "bcStartDate": "start_date",
        "bcEndDate": "end_date",
        "ndStartDate": "nd_start_date",
        "ndEndDate": "nd_end_date",
        "comp": "company",
        "isin": "isin",
        "caBroadcastDate": "ca_broadcast_date",
    }
    df = df.rename(columns=rename_map)
    df = df.reindex(columns=list(rename_map.values()))

    for col in ["date", "record_date", "start_date", "end_date", "nd_start_date", "nd_end_date", "ca_broadcast_date"]:
        s = (
            df[col]
            .astype("string")
            .str.strip()
            .replace({"": pd.NA, "-": pd.NA, "None": pd.NA, "null": pd.NA})
        )
        df[col] = pd.to_datetime(s, format="%d-%b-%Y")

    return df.sort_values("date").drop_duplicates(subset=["date", "symbol"], keep="last")


def sync_corporate_actions(symbols: List[str], from_date: datetime | None = None, to_date: datetime | None = None) -> None:
    _, to_date = normalize_date_window(from_date, to_date)
    redis_client = get_redis_client(REDIS_HOST, REDIS_PORT)
    rows_written = 0
    latest_item_ts = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            counter = 0
            try:
                for symbol in symbols:
                    eqt = get_nse_equity(symbol)
                    issuer = eqt.display_name

                    effective_from_date = choose_from_date(
                        from_date,
                        [
                            get_redis_cursor(redis_client, f"{REDIS_SET}:{symbol}"),
                            get_db_max_date("nseindia_corporate_actions", filters={"symbol": symbol}),
                        ],
                    )
                    if effective_from_date > to_date:
                        continue

                    if counter % 10 == 0:
                        page.goto("https://www.nseindia.com")
                        page.wait_for_timeout(get_random(1000, 3000))

                    df = fetch_corporate_actions(page, symbol, issuer, effective_from_date, to_date)
                    if not df.empty:
                        df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
                        rows_written += int(len(df))
                        max_date = pd.to_datetime(df["date"], utc=True, errors="coerce").max()
                        latest_item_ts = max_date if latest_item_ts is None or max_date > latest_item_ts else latest_item_ts
                        upsert_to_db(
                            df,
                            "nseindia_corporate_actions",
                            unique_keys=["date", "symbol"],
                            timescaledb_column="date",
                        )
                    set_redis_cursor(redis_client, f"{REDIS_SET}:{symbol}", to_date)
                    counter += 1
            finally:
                page.close()
                browser.close()
                redis_client.close()
    except Exception as exc:
        persist_sync_state(source_name=SYNC_SOURCE_NAME, status="error", error_text=f"{type(exc).__name__}: {exc}", state={"symbols": len(symbols), "rows_written": rows_written})
        raise
    persist_sync_state(source_name=SYNC_SOURCE_NAME, status="ok", last_success_at=pd.Timestamp.utcnow(), last_item_ts=latest_item_ts, state={"symbols": len(symbols), "rows_written": rows_written})


def main():
    parser = argparse.ArgumentParser(description="Sync NSE corporate actions for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols, STOCKEY_SYMBOLS, or config/tracked_symbols.txt")

    sync_corporate_actions(
        symbols=symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )


if __name__ == "__main__":
    main()
