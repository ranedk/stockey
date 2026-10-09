"""NSE announcement lists for the trigger study's sampled stocks.

One request per company returns its whole list since 2015 (probed 2026-10-08: TCS 2,645 rows in
one call). The company's own category (`desc`) is kept but NOT trusted -- companies mis-tag
often -- so the queue classifies on the text instead.
"""
from __future__ import annotations

import hashlib

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from utils.cdp import connect_over_cdp
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.nse_rate_limiter import nse_goto, nse_request_gate

FILINGS_TABLE = "fundamentals_trigger_filings"
FROM_DATE = "01-01-2015"
TO_DATE = "31-03-2023"   # the last discovery window ends 2023-03-31; the check years stay unread

_JS = """async (url) => {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) return { __error: r.status };
    return await r.json();
}"""


# The study's ~30 hours of NSE requests share utils/nse_rate_limiter's gate with the daily
# collectors. Inside these UTC windows (morning catch-up chain; evening EOD + screener +
# portfolio) it pauses, so the production jobs never queue behind it.
BUSY_UTC = ((1 * 60 + 30, 3 * 60 + 45), (13 * 60 + 30, 18 * 60))


def nse_busy(now=None) -> bool:
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    m = now.hour * 60 + now.minute
    return any(a <= m < b for a, b in BUSY_UTC)


def wait_until_quiet(log=print) -> bool:
    """True if it paused -- the caller then reloads NSE's home page, since a session left
    idle for hours may have lost its cookies."""
    import time
    if not nse_busy():
        return False
    log("[trigger_study] NSE busy window -- pausing", flush=True)
    while nse_busy():
        time.sleep(60)
    return True


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {FILINGS_TABLE} (
                filing_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, announced_at TIMESTAMP NOT NULL,
                nse_category TEXT, text TEXT, attachment_url TEXT, company_name TEXT,
                nse_industry TEXT, seq_id TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.execute(f"CREATE INDEX IF NOT EXISTS {FILINGS_TABLE}_sym_dt ON {FILINGS_TABLE} (symbol, announced_at)")


def _rows(symbol: str, payload: list[dict]) -> pd.DataFrame:
    out = []
    for r in payload:
        when = pd.to_datetime(r.get("an_dt") or r.get("sort_date"), format="mixed", dayfirst=True, errors="coerce")
        if pd.isna(when):
            continue
        text = (r.get("attchmntText") or "").strip()
        key = "|".join([symbol, str(when), r.get("desc") or "", r.get("attchmntFile") or "", text])
        out.append({
            "filing_id": hashlib.sha1(key.encode()).hexdigest(), "symbol": symbol,
            "announced_at": when.to_pydatetime(), "nse_category": r.get("desc"), "text": text,
            "attachment_url": r.get("attchmntFile"), "company_name": r.get("sm_name"),
            "nse_industry": r.get("smIndustry"), "seq_id": str(r.get("seq_id") or ""),
        })
    return pd.DataFrame(out)


def fetched_symbols() -> set[str]:
    return set(sql_to_df(f"SELECT DISTINCT symbol FROM {FILINGS_TABLE}")["symbol"])


def fetch(symbols: list[str], *, refetch: bool = False) -> dict:
    ensure_table()
    todo = [s for s in symbols if refetch or s not in fetched_symbols()]
    env = Env()
    env.read_env()
    done, failed, rows = [], {}, 0
    if not todo:
        return {"fetched": 0, "failed": {}, "rows": 0}
    with sync_playwright() as p:
        browser = connect_over_cdp(p, env("CDP_ENDPOINT"), caller="fundamentals.trigger_study.filings")
        page = (browser.contexts[0] if browser.contexts else browser.new_context()).new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            page.wait_for_timeout(1500)
            for sym in todo:
                if wait_until_quiet():
                    nse_goto(page, "https://www.nseindia.com")
                url = (f"https://www.nseindia.com/api/corporate-announcements?index=equities"
                       f"&symbol={sym}&from_date={FROM_DATE}&to_date={TO_DATE}")
                try:
                    with nse_request_gate():
                        payload = page.evaluate(_JS, url)
                except Exception as exc:  # noqa: BLE001 -- one company's failure must not stop the rest
                    failed[sym] = str(exc)[:200]
                    continue
                if isinstance(payload, dict):
                    failed[sym] = f"HTTP {payload.get('__error')}"
                    continue
                df = _rows(sym, payload or [])
                if len(df):
                    upsert_to_db(df, FILINGS_TABLE, ["filing_id"])
                rows += len(df)
                done.append(sym)
        finally:
            page.close()
    return {"fetched": len(done), "failed": failed, "rows": rows}
