import argparse
import random
from datetime import datetime
from typing import List
from urllib.parse import urlencode

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from data.dhanlive.dhan_db import get_nse_equity
from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.sync import choose_from_date, get_db_max_date, get_redis_client, get_redis_cursor, load_tracked_symbols, normalize_date_window, parse_datetime_arg, set_redis_cursor

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env.int("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:earnings_events"


def get_random(min_ms: int, max_ms: int) -> int:
    return int(random.uniform(min_ms, max_ms))


def fetch_earnings_events(page, symbol: str, issuer: str, from_date: datetime, to_date: datetime) -> pd.DataFrame:
    page.goto("https://www.nseindia.com/companies-listing/corporate-filings-financial-results")
    page.wait_for_timeout(get_random(1000, 2000))

    frames = []
    for period in ["Quarterly", "Half-Yearly", "Annual"]:
        params = {
            "index": "equities",
            "from_date": from_date.strftime("%d-%m-%Y"),
            "to_date": to_date.strftime("%d-%m-%Y"),
            "symbol": symbol,
            "issuer": issuer,
            "period": period,
        }
        url = f"https://www.nseindia.com/api/corporates-financial-results?{urlencode(params)}"
        data = page.evaluate(
            """async (url) => {
                const res = await fetch(url, { credentials: 'same-origin' });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return await res.json();
            }""",
            url,
        )
        if not data:
            continue

        df = pd.DataFrame(data)
        rename_map = {
            "symbol": "symbol",
            "companyName": "company",
            "industry": "industry",
            "audited": "audited",
            "cumulative": "cumulative",
            "period": "period",
            "financialYear": "financial_year",
            "seqNumber": "seq_number",
            "bank": "bank",
            "fromDate": "from_date",
            "toDate": "to_date",
            "exchdisstime": "reporting_date",
            "consolidated": "consolidated",
            "isin": "isin",
        }
        df = df.rename(columns=rename_map)
        df = df.reindex(columns=list(rename_map.values()))
        for col in ["from_date", "to_date"]:
            df[col] = pd.to_datetime(df[col], format="%d-%b-%Y")
        df["reporting_date"] = pd.to_datetime(df["reporting_date"], format="%d-%b-%Y %H:%M:%S")
        df["date"] = df["to_date"]
        frames.append(df)
        page.wait_for_timeout(get_random(1000, 2000))

    if not frames:
        return pd.DataFrame()

    full_df = pd.concat(frames, ignore_index=True)
    unique_keys = ["date", "symbol", "reporting_date", "period"]
    return full_df.sort_values("reporting_date").drop_duplicates(subset=unique_keys, keep="last")


def sync_earnings_events(symbols: List[str], from_date: datetime | None = None, to_date: datetime | None = None) -> None:
    _, to_date = normalize_date_window(from_date, to_date)
    redis_client = get_redis_client(REDIS_HOST, REDIS_PORT)

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
                        get_db_max_date("nseindia_earnings_events", filters={"symbol": symbol}),
                    ],
                )
                if effective_from_date > to_date:
                    continue

                if counter % 10 == 0:
                    page.goto("https://www.nseindia.com")
                    page.wait_for_timeout(get_random(1000, 3000))

                df = fetch_earnings_events(page, symbol, issuer, effective_from_date, to_date)
                if not df.empty:
                    df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
                    upsert_to_db(
                        df,
                        "nseindia_earnings_events",
                        unique_keys=["date", "symbol", "reporting_date", "period"],
                        timescaledb_column="date",
                    )
                set_redis_cursor(redis_client, f"{REDIS_SET}:{symbol}", to_date)
                counter += 1
        finally:
            page.close()
            browser.close()
            redis_client.close()


def main():
    parser = argparse.ArgumentParser(description="Sync NSE earnings events for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols, STOCKEY_SYMBOLS, or config/tracked_symbols.txt")

    sync_earnings_events(
        symbols=symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )


if __name__ == "__main__":
    main()
