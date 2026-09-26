"""Exchange (BSE) industry labels per listed company -> fundamentals_industry_classification.

Every stock already has a label from Sharpely's bulk master (master_sharpely_equity,
one request). This collector is the slower, current check on top: Sharpely's labels
lag the exchanges' reclassifications (19 of 303 differed on 2026-09-25), so where a BSE
label exists the universe groups use it. Finance and real-estate names are fetched
first, since those are the labels that decide a stock's group.

For the universe rebuild's groups (docs/UNIVERSE_PRD.md section 4). India's exchanges
share one four-level scheme (2023): macro sector > sector > industry > basic industry,
e.g. Financial Services > Financial Services > Finance > Housing Finance Company.
Checked 2026-09-25 on 12 stocks: NSE, BSE and screener.in return identical labels at
every level (screener's /market/IN.. codes are NSE's; Sharpely's sector_code is the top
two levels of the same scheme), so one source is enough.

Not usable as a bulk source (checked 2026-09-25): the calls behind BSE's Industry Watch
page. GetINDUSTRYWATCHLIST_ng lists all ~186 basic industries with codes, but
HeatMap_ng?flag=Ind&issubcode=.. returns only the day's top 30 movers per industry, padded
with "aaaa" placeholders (NBFC: 354 companies, 30 returned), and needs a `random` param.

Source: BSE's quote-header API (plain requests through the BSE rate gate, no browser),
keyed by BSE scrip code. A stock with no BSE listing (NSE-exclusive: CDSL, BSE Ltd, ...)
falls back to NSE's quote API through the shared Chrome (nse_goto + nse_request_gate).

Targets: every company (INE ISIN) trading EQ/BE on NSE's latest session. A label is
re-fetched after REFRESH_DAYS. Rows are keyed (isin, fetch_date), so an old label stays
readable for point-in-time use; `load_latest_industry()` returns the newest per ISIN.

    python -m fundamentals.collectors.industry_classification            # capped daily run
    python -m fundamentals.collectors.industry_classification --limit 0  # everything due
"""

from __future__ import annotations

import argparse
import json
import re

import pandas as pd
import requests

from fundamentals.collectors.security_master import BSE_HEADERS, lookup_bse_scrip_code
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.collectors.industry_classification"
RESULTS_TABLE = "fundamentals_industry_classification"
BSE_QUOTE_HEADER_URL = "https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w"
NSE_SYMBOL_DATA_PATH = "/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolData&marketType=N&series=EQ&symbol={symbol}"
REFRESH_DAYS = 90
DEFAULT_LIMIT = 300  # ~50 min at the 10s BSE gate; the first full load runs with --limit 0
MAX_CONSECUTIVE_FAILURES = 10
STOCKEY_RUN_STATE: dict[str, object] = {}

_TABLE_STATEMENT = f"""
    CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
        isin TEXT NOT NULL,
        fetch_date DATE NOT NULL,
        symbol TEXT,
        bse_scrip_code TEXT,
        macro_sector TEXT,
        sector TEXT,
        industry TEXT,
        basic_industry TEXT,
        source TEXT NOT NULL,
        load_ts TIMESTAMPTZ,
        UNIQUE (isin, fetch_date)
    )
"""

# Every company trading on NSE's latest session, with the newest label we hold (if any).
TARGETS_QUERY = f"""
WITH latest_session AS (
    SELECT max(date) AS d FROM nseindia_ohlcv WHERE date >= now() - interval '15 days' AND series = 'EQ'
)
SELECT DISTINCT ON (o.isin) o.isin, o.symbol, o.company_master_id, cm.bse_scrip_code, lab.fetch_date
  FROM nseindia_ohlcv o
  JOIN latest_session s ON o.date = s.d
  LEFT JOIN company_master cm ON cm.company_master_id = o.company_master_id
  LEFT JOIN LATERAL (
      SELECT fetch_date FROM {RESULTS_TABLE} r WHERE r.isin = o.isin ORDER BY fetch_date DESC LIMIT 1
  ) lab ON true
 WHERE o.date >= now() - interval '15 days' AND o.series IN ('EQ', 'BE') AND o.isin LIKE 'INE%%'
 ORDER BY o.isin, o.series
"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name=f"{RESULTS_TABLE}:ensure_table")


def load_targets(*, today: pd.Timestamp, priority_symbols: set[str] | None = None) -> pd.DataFrame:
    """Companies with no label, or one older than REFRESH_DAYS; never-labelled first,
    then `priority_symbols` (e.g. the Layer 1 universe), then oldest label first."""
    df = sql_to_df(TARGETS_QUERY)
    fetched = pd.to_datetime(df["fetch_date"])
    due = fetched.isna() | (fetched < (today.tz_localize(None).normalize() - pd.Timedelta(days=REFRESH_DAYS)))
    df = df[due].copy()
    df["_never"] = df["fetch_date"].isna()
    df["_priority"] = df["symbol"].isin(priority_symbols or set())
    df["_fetched"] = pd.to_datetime(df["fetch_date"]).fillna(pd.Timestamp("1900-01-01"))
    df = df.sort_values(["_never", "_priority", "_fetched"], ascending=[False, False, True])
    return df.drop(columns=["_never", "_priority", "_fetched"]).reset_index(drop=True)


def parse_bse_header(payload: dict) -> dict[str, str | None] | None:
    """BSE's four levels: Sector (macro) > IndustryNew (sector) > IGroup > ISubGroup.
    None when BSE returns no classification (e.g. a suspended or odd listing)."""
    def clean(v):
        v = (v or "").strip()
        return v if v and v != "-" else None

    labels = {
        "macro_sector": clean(payload.get("Sector")),
        "sector": clean(payload.get("IndustryNew")),
        "industry": clean(payload.get("IGroup")),
        "basic_industry": clean(payload.get("ISubGroup")),
    }
    return labels if labels["basic_industry"] else None


def parse_nse_symbol_data(payload: dict) -> dict[str, str | None] | None:
    """NSE's getSymbolData: macro sector is not returned there, so it stays None."""
    data = payload["equityResponse"][0] if "equityResponse" in payload else payload
    text = json.dumps(data)

    def find(key: str) -> str | None:
        m = re.search(rf'"{key}": "([^"]*)"', text)
        return m.group(1).strip() or None if m else None

    labels = {"macro_sector": None, "sector": find("sector"), "industry": find("industryInfo"),
              "basic_industry": find("basicIndustry")}
    return labels if labels["basic_industry"] else None


def fetch_bse_labels(scrip_code: str, session: requests.Session) -> dict[str, str | None] | None:
    with exchange_request_gate(domain="bse"):
        resp = session.get(BSE_QUOTE_HEADER_URL, params={"quotetype": "EQ", "scripcode": scrip_code, "seriesid": ""},
                           headers=BSE_HEADERS, timeout=30)
    resp.raise_for_status()
    return parse_bse_header(resp.json())


def fetch_nse_labels(symbols: list[str]) -> dict[str, dict[str, str | None] | None]:
    """Browser fallback for NSE-only stocks. Returns {} if Chrome is down (utils.cdp
    exits the process with a banner in that case, so this is imported lazily)."""
    from playwright.sync_api import sync_playwright

    from utils.cdp import connect_over_cdp
    from utils.nse_rate_limiter import nse_goto, nse_request_gate

    out: dict[str, dict[str, str | None] | None] = {}
    with sync_playwright() as p:
        browser = connect_over_cdp(p, "http://localhost:9222", caller=SYNC_SOURCE_NAME)
        page = browser.contexts[0].new_page()
        try:
            nse_goto(page, "https://www.nseindia.com/get-quotes/equity?symbol=RELIANCE", timeout=60000)
            for symbol in symbols:
                try:
                    with nse_request_gate():
                        payload = page.evaluate(
                            "u => fetch(u).then(r => r.json())", NSE_SYMBOL_DATA_PATH.format(symbol=symbol))
                    out[symbol] = parse_nse_symbol_data(payload)
                except Exception as exc:
                    print(f"{symbol}: NSE fallback failed {exc!r}", flush=True)
                    out[symbol] = None
        finally:
            page.close()
    return out


def collect(targets: pd.DataFrame, *, today: pd.Timestamp) -> dict[str, object]:
    session = requests.Session()
    fetch_date = today.tz_convert("Asia/Kolkata").date() if today.tzinfo else today.date()
    rows, pending, unlabelled, failed, nse_only = [], [], [], [], []
    consecutive = 0
    for t in targets.itertuples(index=False):
        code = t.bse_scrip_code
        try:
            if not code:
                hit = lookup_bse_scrip_code(t.isin)
                code = hit["scrip_code"] if hit else None
            if not code:
                nse_only.append(t)
                continue
            labels = fetch_bse_labels(str(code), session)
            consecutive = 0
        except Exception as exc:
            failed.append(t.symbol); consecutive += 1
            print(f"{t.symbol}: FAILED {exc!r}", flush=True)
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                break
            continue
        if labels is None:
            nse_only.append(t)  # BSE listing without a classification: try NSE
            continue
        row = {"isin": t.isin, "fetch_date": fetch_date, "symbol": t.symbol, "bse_scrip_code": str(code),
               "source": "bse", **labels}
        rows.append(row); pending.append(row)
        print(f"{t.symbol}: {labels['basic_industry']}", flush=True)
        if len(pending) >= 50:  # a long run keeps what it fetched if it dies midway
            _write(pending); pending = []

    if nse_only:
        nse = fetch_nse_labels([t.symbol for t in nse_only])
        for t in nse_only:
            labels = nse.get(t.symbol)
            if labels is None:
                unlabelled.append(t.symbol)
                continue
            row = {"isin": t.isin, "fetch_date": fetch_date, "symbol": t.symbol, "bse_scrip_code": None,
                   "source": "nse", **labels}
            rows.append(row); pending.append(row)
            print(f"{t.symbol}: {labels['basic_industry']} (nse)", flush=True)
    _write(pending)
    if failed or unlabelled:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME, source="bse", fallback_type="industry_label_missing", severity="warn",
            reason=f"{len(failed)} fetches failed, {len(unlabelled)} companies have no label on BSE or NSE",
            error="see metadata", metadata={"failed": failed[:50], "unlabelled": unlabelled[:50]},
        )
    return {"rows_written": len(rows), "failed": failed, "unlabelled": unlabelled,
            "from_nse": sum(r["source"] == "nse" for r in rows), "targets": len(targets)}


def _write(rows: list[dict]) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["load_ts"] = pd.Timestamp.now(tz="UTC")
    upsert_to_db(df, RESULTS_TABLE, unique_keys=["isin", "fetch_date"])


def load_latest_industry() -> pd.DataFrame:
    return sql_to_df(
        f"SELECT DISTINCT ON (isin) * FROM {RESULTS_TABLE} ORDER BY isin, fetch_date DESC"
    )


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Collect exchange industry labels per listed company.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="max companies this run; 0 = all due")
    args = parser.parse_args(argv)
    ensure_table()
    today = pd.Timestamp.now(tz="Asia/Kolkata")
    try:
        from fundamentals.screens.universe import apply_layer1_rules, load_layer1_inputs

        layer1 = apply_layer1_rules(load_layer1_inputs())
        in_layer1 = layer1.loc[layer1["layer1_pass"], ["symbol", "isin"]]
        group_deciding = sql_to_df(
            "SELECT isin FROM master_sharpely_equity WHERE sector_code IN ('IN0501', 'IN0205') AND isin IS NOT NULL"
        )
        priority = set(in_layer1.loc[in_layer1["isin"].isin(group_deciding["isin"]), "symbol"])
    except Exception as exc:  # ordering only; never block collection on it
        print(f"layer1 priority unavailable: {exc!r}", flush=True)
        priority = set()
    targets = load_targets(today=today, priority_symbols=priority)
    total_due = len(targets)
    if args.limit:
        targets = targets.head(args.limit)
    result = collect(targets, today=today)
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows_written"],
        "rows_written": result["rows_written"],
        "from_nse": result["from_nse"],
        "failed": result["failed"],
        "unlabelled": result["unlabelled"],
        "due": total_due,
        "fallback_used": bool(result["failed"] or result["unlabelled"]),
        "state_advanced": result["rows_written"] > 0,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
