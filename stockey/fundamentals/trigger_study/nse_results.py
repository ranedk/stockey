"""Quarterly results for the trigger study from NSE's machine-readable (XBRL) result files.

screener.in only gives us the last 13 quarters (2023 onward), so 2016-2022 had no results at
all. NSE's results listing carries an XBRL file per filing from the quarter ending Sep 2019
(probed 2026-10-08: SAFARI's list starts 2019-09-30; an explicit 2015-2019 date range returns
nothing earlier). Typed numbers, no OCR, and the exact time each result was published -- which
replaces the "quarter end + 60 days" guess.

One basis per company: consolidated when the company files it for most quarters, else
standalone, so growth is never computed across a basis switch.
"""
from __future__ import annotations

import re

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright

from utils.cdp import connect_over_cdp
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.nse_rate_limiter import nse_goto, nse_request_gate

RESULTS_TABLE = "fundamentals_trigger_results"
LAST_PERIOD = pd.Timestamp("2022-12-31")   # discovery windows end 2023-03; check years stay unread
CRORE = 1e7

_JS = """async (url) => {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) return { __error: r.status };
    return await r.json();
}"""

FIELDS = {
    "RevenueFromOperations": "sales", "OtherIncome": "other_income", "FinanceCosts": "finance_costs",
    "DepreciationDepletionAndAmortisationExpense": "depreciation", "Expenses": "expenses",
    "ProfitBeforeTax": "pbt", "ProfitLossForPeriod": "net_profit",
}
_FACT_RX = re.compile(r'<[\w-]+:(\w+)\b[^>]*contextRef="OneD"[^>]*>([^<]*)<')


def ensure_table() -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
                symbol TEXT NOT NULL, period_end DATE NOT NULL, consolidated BOOLEAN NOT NULL,
                public_at TIMESTAMP, sales DOUBLE PRECISION, other_income DOUBLE PRECISION,
                finance_costs DOUBLE PRECISION, depreciation DOUBLE PRECISION, expenses DOUBLE PRECISION,
                pbt DOUBLE PRECISION, net_profit DOUBLE PRECISION, xbrl_url TEXT, status TEXT NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, period_end, consolidated)
            )""")


def parse_xbrl(xml: str) -> dict:
    """OneD (this quarter) facts in Rs crore. Duplicate tags keep the first value."""
    out = {}
    for tag, val in _FACT_RX.findall(xml):
        col = FIELDS.get(tag)
        if col and col not in out:
            try:
                out[col] = float(val.strip()) / CRORE
            except ValueError:
                pass
    return out


def choose_rows(listing: list[dict]) -> pd.DataFrame:
    """One basis per company, one filing per quarter (the latest broadcast if refiled)."""
    df = pd.DataFrame(listing)
    if df.empty or "toDate" not in df:
        return pd.DataFrame()
    df["period_end"] = pd.to_datetime(df["toDate"], format="%d-%b-%Y", errors="coerce")
    df["public_at"] = pd.to_datetime(df["broadCastDate"], format="%d-%b-%Y %H:%M:%S", errors="coerce")
    df["consolidated"] = df["consolidated"].astype(str).str.lower().str.startswith("consolidated")
    df = df[df["period_end"].notna() & (df["period_end"] <= LAST_PERIOD) & df["xbrl"].astype(str).str.endswith(".xml")]
    if df.empty:
        return df
    quarters = df["period_end"].nunique()
    cons = df.loc[df["consolidated"], "period_end"].nunique()
    basis = cons >= 0.8 * quarters
    df = df[df["consolidated"] == basis].sort_values("public_at")
    return df.drop_duplicates("period_end", keep="last")


def fetched_symbols() -> set[str]:
    return set(sql_to_df(f"SELECT DISTINCT symbol FROM {RESULTS_TABLE}")["symbol"])


def fetch(symbols: list[str]) -> dict:
    ensure_table()
    todo = [s for s in symbols if s not in fetched_symbols()]
    if not todo:
        return {"symbols": 0}
    env = Env()
    env.read_env()
    stats = {"symbols": 0, "quarters": 0, "parse_empty": 0, "failed": {}}
    with sync_playwright() as p:
        browser = connect_over_cdp(p, env("CDP_ENDPOINT"), caller="fundamentals.trigger_study.nse_results")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            page.wait_for_timeout(1000)
            for sym in todo:
                url = f"https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol={sym}&period=Quarterly"
                try:
                    with nse_request_gate():
                        listing = page.evaluate(_JS, url)
                except Exception as exc:  # noqa: BLE001 -- one company's failure must not stop the rest
                    stats["failed"][sym] = str(exc)[:200]
                    continue
                if isinstance(listing, dict):
                    stats["failed"][sym] = f"HTTP {listing.get('__error')}"
                    continue
                rows = []
                for r in choose_rows(listing or []).itertuples():
                    rec = {"symbol": sym, "period_end": r.period_end.date(), "consolidated": bool(r.consolidated),
                           "public_at": None if pd.isna(r.public_at) else r.public_at.to_pydatetime(),
                           "xbrl_url": r.xbrl, "status": "ok"}
                    try:
                        with nse_request_gate():
                            resp = ctx.request.get(r.xbrl, timeout=60000)
                        facts = parse_xbrl(resp.text()) if resp.ok else {}
                        if not resp.ok:
                            rec["status"] = f"http_{resp.status}"
                    except Exception:  # noqa: BLE001
                        facts, rec["status"] = {}, "download_failed"
                    if rec["status"] == "ok" and "sales" not in facts:
                        rec["status"] = "parse_empty"
                        stats["parse_empty"] += 1
                    rows.append({**{c: None for c in FIELDS.values()}, **rec, **facts})
                if not rows:   # mark the company done so a rerun does not ask NSE again
                    rows = [{**{c: None for c in FIELDS.values()}, "symbol": sym, "period_end": LAST_PERIOD.date(),
                             "consolidated": False, "public_at": None, "xbrl_url": None, "status": "no_filings"}]
                upsert_to_db(pd.DataFrame(rows), RESULTS_TABLE, ["symbol", "period_end", "consolidated"])
                stats["symbols"] += 1
                stats["quarters"] += sum(1 for x in rows if x["status"] == "ok")
        finally:
            page.close()
    return stats


def load(symbols: list[str]) -> pd.DataFrame:
    """symbol, period_end, public_at, sales, opm_pct (EBITDA margin), net_profit -- Rs crore."""
    df = sql_to_df(f"""SELECT symbol, period_end, public_at, sales, other_income, finance_costs,
                              depreciation, pbt, net_profit
                         FROM {RESULTS_TABLE} WHERE status = 'ok' AND symbol = ANY(%s)""", params=(symbols,))
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["public_at"] = pd.to_datetime(df["public_at"])
    ebitda = df["pbt"] + df["finance_costs"].fillna(0) + df["depreciation"].fillna(0) - df["other_income"].fillna(0)
    df["opm_pct"] = (100 * ebitda / df["sales"]).where(df["sales"] > 0)
    return df.sort_values(["symbol", "period_end"])
