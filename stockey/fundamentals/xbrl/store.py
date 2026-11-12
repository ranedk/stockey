"""XBRL result filings: tables, the orchestrator's ingest of the two task kinds, re-parsing,
and the cross-check against screener.in (docs/XBRL_FUNDAMENTALS.md).

  nse_results_listing (one per company) -> a row per filing in FILING_TABLE, a fetch_xbrl task
                                           for each new one
  fetch_xbrl (one per filing)           -> the raw file in S3, its values in VALUE_TABLE

Every filing is kept -- both bases, every revision -- with the time it became public, so a
reader can ask what was known on any date. Values are re-derived from S3 when the mapping
changes (`reparse`), never re-downloaded.

    python -m fundamentals.xbrl.store enqueue --symbols KAYNES TCS | --universe [--limit N]
    python -m fundamentals.xbrl.store reparse [--all]
    python -m fundamentals.xbrl.store check
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime

import pandas as pd

from fundamentals.xbrl import mapping
from fundamentals.xbrl.parse import parse
from utils.db import db_session, sql_to_df

FILING_TABLE = "fundamentals_xbrl_filing"
VALUE_TABLE = "fundamentals_xbrl_value"
LATEST_VIEW = "fundamentals_xbrl_latest"
FIRST_PERIOD = date(2020, 1, 1)          # operator, 2026-11-10: results from 2020 on
PRIORITY = 60                            # after announcement history (50)

_DDL = [
    f"""CREATE TABLE IF NOT EXISTS {FILING_TABLE} (
        filing_key TEXT PRIMARY KEY,           -- sha1 of the XBRL url
        symbol TEXT NOT NULL, listing TEXT NOT NULL,   -- old (to 2024) / int (integrated, 2025 on)
        period_end DATE NOT NULL, basis TEXT NOT NULL, -- consolidated / standalone
        revision TEXT, audited TEXT,
        public_at TIMESTAMP,                    -- NSE broadcast time (IST)
        url TEXT NOT NULL, s3_key TEXT,
        family TEXT, taxonomy TEXT, taxonomy_version TEXT,
        status TEXT NOT NULL DEFAULT 'pending', -- pending / parsed / not_found / not_xbrl / failed
        checks JSONB, checks_failed INT, mapping_version INT, error TEXT,
        listed_at TIMESTAMPTZ NOT NULL DEFAULT now(), parsed_at TIMESTAMPTZ)""",
    f"CREATE INDEX IF NOT EXISTS {FILING_TABLE}_sym ON {FILING_TABLE} (symbol, period_end)",
    f"""CREATE TABLE IF NOT EXISTS {VALUE_TABLE} (
        filing_key TEXT NOT NULL REFERENCES {FILING_TABLE}(filing_key) ON DELETE CASCADE,
        item TEXT NOT NULL,
        kind TEXT NOT NULL,                     -- q / h1 / 9m / fy / i (instant)
        period_start DATE, period_end DATE NOT NULL,
        value DOUBLE PRECISION NOT NULL,        -- rupees (EPS per share, ratios as filed)
        tag TEXT,
        PRIMARY KEY (filing_key, item, kind, period_end))""",
    # the latest-known value per company, basis, item and period (a reader that needs a past
    # date filters public_at itself)
    f"""CREATE OR REPLACE VIEW {LATEST_VIEW} AS
        SELECT DISTINCT ON (f.symbol, f.basis, v.item, v.kind, v.period_end)
               f.symbol, f.basis, f.family, v.item, v.kind, v.period_start, v.period_end, v.value,
               f.public_at, f.filing_key
          FROM {VALUE_TABLE} v JOIN {FILING_TABLE} f USING (filing_key)
         WHERE f.status = 'parsed'
         ORDER BY f.symbol, f.basis, v.item, v.kind, v.period_end, f.public_at DESC NULLS LAST""",
]


def ensure() -> None:
    with db_session() as (_, cur):
        for ddl in _DDL:
            cur.execute(ddl)


def key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def s3_key(url: str) -> str:
    return f"fundamentals/xbrl/{key(url)}.xml"


# --- listing ---------------------------------------------------------------------------------

def _ts(s: str | None, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), fmt) if s else None
    except ValueError:
        return None


def listing_rows(symbol: str, result: dict) -> list[dict]:
    """One row per XBRL filing from 2020 on. "Non-Consolidated" (old listing) = standalone."""
    out = []
    for r in result.get("old") or []:
        url, end = str(r.get("xbrl") or ""), _ts(r.get("toDate"), "%d-%b-%Y")
        if not url.endswith(".xml") or end is None or end.date() < FIRST_PERIOD:
            continue
        out.append({"filing_key": key(url), "symbol": symbol, "listing": "old", "period_end": end.date(),
                    "basis": "consolidated" if r.get("consolidated") == "Consolidated" else "standalone",
                    "revision": "revised" if r.get("reInd") == "Y" else "original", "audited": r.get("audited"),
                    "public_at": _ts(r.get("broadCastDate"), "%d-%b-%Y %H:%M:%S"), "url": url})
    for r in result.get("int") or []:
        url, end = str(r.get("xbrl") or ""), _ts(r.get("qe_Date"), "%d-%b-%Y")
        if not url.endswith(".xml") or end is None or end.date() < FIRST_PERIOD:
            continue
        out.append({"filing_key": key(url), "symbol": symbol, "listing": "int", "period_end": end.date(),
                    "basis": "consolidated" if r.get("consolidated") == "Consolidated" else "standalone",
                    "revision": (r.get("type_Sub") or "original").lower(), "audited": r.get("audited"),
                    # a revision has no broadcast date; NSE gives when it was revised (or created)
                    "public_at": _ts(r.get("broadcast_Date"), "%d-%b-%Y %H:%M:%S")
                    or _ts(r.get("revised_Date"), "%d-%b-%Y %H:%M:%S")
                    or _ts(r.get("creation_Date"), "%d-%b-%Y %H:%M:%S"), "url": url})
    return list({r["filing_key"]: r for r in out}.values())


_COLS = ["filing_key", "symbol", "listing", "period_end", "basis", "revision", "audited", "public_at", "url"]


def ingest_listing(task: dict, result: dict, _file: bytes | None) -> list[dict]:
    from infra import tasks
    rows = listing_rows(task["payload"]["symbol"], result)
    new = []
    with db_session() as (_, cur):
        for r in rows:
            cur.execute(f"""INSERT INTO {FILING_TABLE} ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))})
                            ON CONFLICT (filing_key) DO NOTHING""", [r[c] for c in _COLS])
            if cur.rowcount:
                new.append(r)
    return [tasks.new_task("fetch_xbrl", {"url": r["url"], "symbol": r["symbol"]}, dedup_key=f"xbrl:{r['url']}",
                           priority=task.get("priority") or PRIORITY, parent_id=task["id"], run_tag=task.get("run_tag"))
            for r in new]


# --- one filing ------------------------------------------------------------------------------

def store_values(filing_key: str, xml: bytes) -> dict:
    """Parse, map, check and store one filing's values. Returns the filing row's new fields."""
    f = parse(xml)
    fam, rows = mapping.extract(f)
    ch = mapping.checks(fam, rows)
    with db_session() as (_, cur):
        cur.execute(f"DELETE FROM {VALUE_TABLE} WHERE filing_key = %s", (filing_key,))
        for r in rows:
            cur.execute(f"""INSERT INTO {VALUE_TABLE} (filing_key, item, kind, period_start, period_end, value, tag)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (filing_key, r.item, r.kind, r.start, r.end, r.value, r.tag))
        upd = {"status": "parsed" if rows else "failed", "family": fam, "taxonomy": f.taxonomy,
               "taxonomy_version": f.taxonomy_version, "checks": json.dumps(ch),
               "checks_failed": sum(not c["ok"] for c in ch), "mapping_version": mapping.MAPPING_VERSION,
               "error": None if rows else "no values mapped"}
        cur.execute(f"""UPDATE {FILING_TABLE} SET {', '.join(f'{k} = %s' for k in upd)}, parsed_at = now()
                         WHERE filing_key = %s""", [*upd.values(), filing_key])
    return upd


def ingest_xbrl(task: dict, result: dict, file: bytes | None) -> list[dict]:
    from utils.store import save_file_content
    url = task["payload"]["url"]
    k = key(url)
    if file is None or result.get("status") != "fetched":
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {FILING_TABLE} SET status = %s, error = %s WHERE filing_key = %s",
                        (result.get("status") or "failed", result.get("error"), k))
        return []
    save_file_content(s3_key(url), file)
    with db_session() as (_, cur):
        cur.execute(f"UPDATE {FILING_TABLE} SET s3_key = %s WHERE filing_key = %s", (s3_key(url), k))
    try:
        store_values(k, file)
    except Exception as exc:  # noqa: BLE001 -- a malformed file is recorded, not retried
        with db_session() as (_, cur):
            cur.execute(f"UPDATE {FILING_TABLE} SET status = 'failed', error = %s WHERE filing_key = %s",
                        (f"parse: {exc!r}"[:500], k))
    return []


# --- operator commands -----------------------------------------------------------------------

def enqueue(symbols: list[str], run_tag: str | None = None) -> int:
    from infra import tasks
    ensure()
    return tasks.enqueue([tasks.new_task("nse_results_listing", {"symbol": s}, dedup_key=f"xbrl-list:{s}:{run_tag}",
                                         priority=PRIORITY, run_tag=run_tag) for s in symbols])


def reparse(all_: bool = False) -> dict:
    """Re-derive values from the stored files: every filing parsed under an older mapping, or
    all of them."""
    from utils.store import get_file_content
    todo = sql_to_df(f"""SELECT filing_key, s3_key FROM {FILING_TABLE} WHERE s3_key IS NOT NULL
                           AND (%(all)s OR mapping_version IS DISTINCT FROM %(v)s)""",
                     params={"all": all_, "v": mapping.MAPPING_VERSION})
    n = failed = 0
    for r in todo.itertuples():
        try:
            store_values(r.filing_key, get_file_content(r.s3_key))
            n += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            with db_session() as (_, cur):
                cur.execute(f"UPDATE {FILING_TABLE} SET status = 'failed', error = %s WHERE filing_key = %s",
                            (f"parse: {exc!r}"[:500], r.filing_key))
    return {"reparsed": n, "failed": failed}


def compare(ours: pd.DataFrame, screener: pd.DataFrame) -> pd.DataFrame:
    """Standalone quarters side by side with screener.in (Rs crore) and the % gap. Pure."""
    m = ours.merge(screener, on=["symbol", "period_end"])
    m["sales_gap_pct"] = 100 * (m["sales_ours"] - m["sales"]) / m["sales"].abs()
    m["profit_gap_pct"] = 100 * (m["profit_ours"] - m["net_profit"]) / m["net_profit"].abs()
    return m


def check() -> dict:
    """How far our standalone quarters are from screener.in's. screener.in has its own errors
    (operator, 2026-11-10): 2-5% is fine; larger gaps are listed for a look, not trusted blindly."""
    ours = sql_to_df(f"""SELECT symbol, family, period_end, item, value / 1e7 AS v FROM {LATEST_VIEW}
                          WHERE basis = 'standalone' AND kind = 'q' AND item IN ('revenue', 'revenue_net', 'total_income', 'pat')""")
    if ours.empty:
        return {"quarters": 0}
    ours["item"] = [("sales_ours" if mapping.SCREENER_SALES[f] == i else "profit_ours" if i == "pat" else None)
                    for f, i in zip(ours["family"], ours["item"])]
    ours = ours.dropna(subset=["item"]).pivot_table(index=["symbol", "period_end"], columns="item", values="v").reset_index()
    scr = sql_to_df("""SELECT ticker AS symbol, period_end, sales, net_profit FROM fundamentals_quarterly_results
                        WHERE ticker = ANY(%(s)s)""", params={"s": list(ours["symbol"].unique())})
    m = compare(ours, scr)
    out = {"quarters": len(m)}
    for col in ("sales_gap_pct", "profit_gap_pct"):
        g = m[col].abs().dropna()
        out[col] = {"n": len(g), "within_2": int((g <= 2).sum()), "within_5": int((g <= 5).sum())}
    out["worst"] = m.assign(w=m[["sales_gap_pct", "profit_gap_pct"]].abs().max(axis=1)).nlargest(15, "w")[
        ["symbol", "period_end", "sales_ours", "sales", "profit_ours", "net_profit"]].round(1).to_dict("records")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enqueue")
    e.add_argument("--symbols", nargs="+")
    e.add_argument("--universe", action="store_true")
    e.add_argument("--limit", type=int)
    e.add_argument("--tag", default=f"xbrl-{date.today():%Y-%m-%d}")
    r = sub.add_parser("reparse")
    r.add_argument("--all", action="store_true")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    ensure()
    if a.cmd == "enqueue":
        if a.universe:
            from utils.universe import get_equity_universe
            syms = sorted(get_equity_universe())
        else:
            syms = a.symbols or []
        syms = syms[:a.limit] if a.limit else syms
        print(json.dumps({"queued": enqueue(syms, a.tag), "symbols": len(syms), "tag": a.tag}))
    elif a.cmd == "reparse":
        print(json.dumps(reparse(a.all)))
    else:
        print(json.dumps(check(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
