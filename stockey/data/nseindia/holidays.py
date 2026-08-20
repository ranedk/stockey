# trading_days.py
import json
import random
import numpy as np
from datetime import datetime

import redis
import pandas as pd
from environs import Env
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utils.fallback_telemetry import record_local_fallback_event
from utils.db import upsert_to_db
from utils.nse_rate_limiter import nse_goto
from utils.sync import get_redis_client


env = Env()
env.read_env()


REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
CDP_ENDPOINT = env("CDP_ENDPOINT")
REDIS_SET = "nse:trading_days"
rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))
SYNC_SOURCE_NAME = "data.nseindia.holidays"
STOCKEY_RUN_STATE: dict[str, object] = {}


nse_product_info = {
    "CM": {
        "category": "Capital Market",
        "name": "Equities (Cash segment)",
        "definition": "All regular T+1 equity trading on the main board and SME board."
    },
    "CMOT": {
        "category": "Capital Market",
        "name": "Equities - Optional T+0",
        "definition": "Same-day (T+0) settlement trades in the cash market."
    },
    "MF": {
        "category": "Capital Market",
        "name": "Mutual Funds",
        "definition": "Orders routed through MFSS / NMF II platforms - subscriptions, redemptions, SIPs, etc."
    },
    "SLBS": {
        "category": "Capital Market",
        "name": "Securities Lending & Borrowing Scheme",
        "definition": "Stock-lending & borrowing transactions that enable short-selling and yield enhancement."
    },
    "FO": {
        "category": "Derivatives Market",
        "name": "Equity Derivatives (F&O)",
        "definition": "Index and stock futures and options traded on the NSE F&O segment."
    },
    "CD": {
        "category": "Derivatives Market",
        "name": "Currency Derivatives",
        "definition": "INR and cross-currency futures and options pairs such as USD-INR, EUR-INR."
    },
    "COM": {
        "category": "Derivatives Market",
        "name": "Commodity Derivatives",
        "definition": "Futures and options on bullion, energy, base metals and agricultural commodities."
    },
    "IRD": {
        "category": "Derivatives Market",
        "name": "Interest Rate Derivatives",
        "definition": "Futures on GOI bonds, MIBOR and other fixed-income rate-hedging instruments."
    },
    "CBM": {
        "category": "Debt Market",
        "name": "Corporate Bonds",
        "definition": "Secondary-market corporate bond trades, including listed and unlisted private placements."
    },
    "NDM": {
        "category": "Debt Market",
        "name": "New Debt Segment",
        "definition": "Order-matched trading of government and other debt securities on the wholesale debt platform."
    },
    "NTRP": {
        "category": "Debt Market",
        "name": "Negotiated Trade Reporting Platform",
        "definition": "Off-market debt deals reported for settlement under the new debt platform."
    },
    "EGR": {
        "category": "Electronic Gold Receipts",
        "name": "Gold Segment",
        "definition": "All Gold receipts reported for settlement"
    }
}



def get_random(min_ms: int, max_ms: int) -> int:
    """Return a random int in milliseconds between min_ms and max_ms."""
    return int(random.uniform(min_ms, max_ms))


def download_holidays(
    playwright,
) -> dict[str, object]:
    """
    Download the calendar csv file for all events
    https://www.nseindia.com/resources/exchange-communication-holidays
    """
    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "attempt_count": 1,
        "download_attempts": 1,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_used": False,
        "state_advanced": False,
    }
    browser = None
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        nse_goto(page, "https://www.nseindia.com")
        page.wait_for_timeout(get_random(1000, 2000))
        nse_goto(page, 'https://www.nseindia.com/resources/exchange-communication-holidays')
        page.wait_for_timeout(get_random(1000, 2000))

        url = 'https://www.nseindia.com/api/holiday-master?type=trading'
        data = page.evaluate(
            """async (url) => {
                const res = await fetch(url, { credentials: 'same-origin' });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                return await res.json();
            }""",
            url,
        )

        full_df = pd.DataFrame()
        for k, v in data.items():
            df = pd.DataFrame(v)
            df['type'] = k
            segment_info = nse_product_info.get(k)
            if segment_info is None:
                record_local_fallback_event(
                    module=SYNC_SOURCE_NAME,
                    source=SYNC_SOURCE_NAME,
                    fallback_type="nse_holidays_unknown_segment",
                    severity="warn",
                    reason="NSE holidays returned a product segment not in the local product map; using the raw segment key as its name.",
                    metadata={"segment_key": str(k)},
                )
            df['type_name'] = (segment_info or {}).get('name', k)
            df = df.rename(columns={
                'tradingDate': 'date',
                'weekDay': 'weekday',
                'description': 'holiday',
                'morning_session': 'morning_session',
                "evening_session": "evening_session",
                "Sr_no": "sr_no"
            })
            df = df.drop(columns=["sr_no", "weekday"])
            df['date'] = pd.to_datetime(df['date'], format="%d-%b-%Y")
            df = df.replace(to_replace=[None], value=np.nan)
            full_df = pd.concat([full_df, df])

        full_df = full_df.reset_index(drop=True)
        full_df = full_df.drop_duplicates(subset=["date", "type"], keep='last')
        upsert_to_db(full_df, "nseindia_holidays", unique_keys=["date", "type"], timescaledb_column="date")
        rop.set(REDIS_SET, datetime.today().strftime("%Y-%m-%d"))
        state.update(
            {
                "status": "ok",
                "rows": int(len(full_df)),
                "rows_read": int(len(full_df)),
                "rows_written": int(len(full_df)),
                "product_type_count": int(full_df["type"].nunique()) if not full_df.empty else 0,
                "from_date": full_df["date"].min().date().isoformat() if not full_df.empty else None,
                "to_date": full_df["date"].max().date().isoformat() if not full_df.empty else None,
                "state_advanced": bool(len(full_df) > 0),
            }
        )
        return state
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        state.update(
            {
                "status": "source_unavailable",
                "failed_attempt_count": 1,
                "source_unavailable_count": 1,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=SYNC_SOURCE_NAME,
            fallback_type="nse_holidays_source_unavailable",
            severity="warn",
            reason="NSE holidays browser/API source was unavailable; trading calendar freshness may be degraded.",
            error=exc,
            metadata={
                "classification": "source_unavailable",
                "attempt_count": state["attempt_count"],
            },
        )
        print(f"NSE holidays download skipped: {type(exc).__name__}: {exc}", flush=True)
        return state
    except Exception as exc:
        state.update(
            {
                "status": "failed",
                "failed_attempt_count": 1,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source=SYNC_SOURCE_NAME,
            fallback_type="nse_holidays_download_failed",
            severity="error",
            reason="NSE holidays download or parsing failed; trading calendar freshness may be degraded.",
            error=exc,
            metadata={
                "classification": "failed",
                "attempt_count": state["attempt_count"],
            },
        )
        print(f"NSE holidays download failed: {type(exc).__name__}: {exc}", flush=True)
        return state
    finally:
        if page is not None:
            page.close()
        if browser is not None:
            browser.close()


def main() -> int:
    global STOCKEY_RUN_STATE
    with sync_playwright() as p:
        if rop.get(REDIS_SET):
            print("Last crawl on ", rop.get(REDIS_SET))

        STOCKEY_RUN_STATE = download_holidays(p)

    rop.close()
    status = str(STOCKEY_RUN_STATE.get("status") or "ok")
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    # BUG FOUND LIVE 2026-08-20 (re-audit, HIGH): this used to unconditionally `return 0`
    # regardless of `status` -- download_holidays() classifies "failed"/"source_unavailable"
    # via record_local_fallback_event but never surfaces that through the exit code, so
    # data.download_runner's run_download_module() (which only looks at the exit code to
    # decide its own top-level "status": "ok"/"failed") reported a completely failed
    # holidays download as a successful step, every time. A stale trading calendar is a
    # correctness risk for every date-aware check downstream (data_readiness.py, the
    # dim_trading_days-based clamp in ohlcv.py, ...), so it must not fail silently.
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
