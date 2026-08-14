import argparse
import json
import random
from datetime import datetime
from typing import List
from urllib.parse import urlencode

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from utils.sync_state import persist_sync_state
from data.dhanlive.dhan_db import get_nse_equity
from utils.company_master import attach_company_master_id
from utils.db import upsert_to_db
from utils.nse_rate_limiter import nse_goto
from utils.sync import choose_from_date, get_db_max_date, get_redis_client, get_redis_cursor, load_tracked_symbols, normalize_date_window, parse_datetime_arg, set_redis_cursor

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env.int("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:earnings_events"
SYNC_SOURCE_NAME = "data.nseindia.earnings_events"
STOCKEY_RUN_STATE: dict[str, object] = {}


def get_random(min_ms: int, max_ms: int) -> int:
    return int(random.uniform(min_ms, max_ms))


def fetch_earnings_events(page, symbol: str, issuer: str, from_date: datetime, to_date: datetime) -> pd.DataFrame:
    nse_goto(page, "https://www.nseindia.com/companies-listing/corporate-filings-financial-results")
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


def sync_earnings_events(symbols: List[str], from_date: datetime | None = None, to_date: datetime | None = None) -> dict[str, object]:
    effective_from_date, to_date = normalize_date_window(from_date, to_date)
    redis_client = get_redis_client(REDIS_HOST, REDIS_PORT)
    rows_written = 0
    latest_item_ts = None
    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol or "").strip()]
    symbols_queried = 0
    symbols_skipped = 0

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            counter = 0
            try:
                for symbol in normalized_symbols:
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
                        symbols_skipped += 1
                        continue

                    if counter % 10 == 0:
                        nse_goto(page, "https://www.nseindia.com")
                        page.wait_for_timeout(get_random(1000, 3000))

                    df = fetch_earnings_events(page, symbol, issuer, effective_from_date, to_date)
                    symbols_queried += 1
                    if not df.empty:
                        df = attach_company_master_id(df, ticker_column="symbol", exchange="NSE")
                        rows_written += int(len(df))
                        max_date = pd.to_datetime(df["date"], utc=True, errors="coerce").max()
                        latest_item_ts = max_date if latest_item_ts is None or max_date > latest_item_ts else latest_item_ts
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
    except Exception as exc:
        persist_sync_state(
            source_name=SYNC_SOURCE_NAME,
            status="error",
            error_text=f"{type(exc).__name__}: {exc}",
            state={"symbols": len(normalized_symbols), "symbols_queried": symbols_queried, "symbols_skipped": symbols_skipped, "rows_written": rows_written},
        )
        raise
    state = {
        "source": SYNC_SOURCE_NAME,
        "rows": rows_written,
        "rows_read": symbols_queried,
        "rows_written": rows_written,
        "symbol_count": len(normalized_symbols),
        "symbols_queried": symbols_queried,
        "symbols_skipped": symbols_skipped,
        "from_date": pd.Timestamp(effective_from_date).date().isoformat() if effective_from_date else None,
        "to_date": pd.Timestamp(to_date).date().isoformat() if to_date else None,
        "latest_item_ts": latest_item_ts.isoformat() if latest_item_ts is not None and pd.notna(latest_item_ts) else None,
        "fallback_used": False,
        "state_advanced": rows_written > 0 or symbols_queried > 0,
    }
    persist_sync_state(source_name=SYNC_SOURCE_NAME, status="ok", last_success_at=pd.Timestamp.utcnow(), last_item_ts=latest_item_ts, state=state)
    return state


def main() -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Sync NSE earnings events for tracked symbols")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    args = parser.parse_args()

    symbols = load_tracked_symbols(args.symbols)
    if not symbols:
        raise SystemExit("No symbols provided. Use --symbols or STOCKEY_SYMBOLS.")

    STOCKEY_RUN_STATE = sync_earnings_events(
        symbols=symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
