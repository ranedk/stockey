"""Order-win announcements for LEDGER row 84 (systrade/research/preregistrations/
2026-11-02_order_wins.md -- read it before changing anything: a change to the rule is a new test).

Step 1 here: fetch every universe company's NSE announcement list (2018-2025, one request per
company, through nse_request_gate / nse_goto, pausing in the production jobs' busy windows) and
keep only order-like candidates -- the full lists would be ~1.5M rows on the small cloud DB.
`fundamentals_order_fetch` logs each company so the run resumes. Classification and value
extraction (step 2) and the test (step 3) read these candidates; no return is computed here.

    python -m fundamentals.trigger_study.orders fetch [--limit N]
    python -m fundamentals.trigger_study.orders classify [--limit N] [--any-time]
    python -m fundamentals.trigger_study.orders attachments [--limit N]      # PDFs of wins with no value
    python -m fundamentals.trigger_study.orders classify-attachments [--limit N] [--any-time]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re

import pandas as pd
from environs import Env
from playwright.sync_api import sync_playwright
from sqlalchemy import text as sql_text

from fundamentals.trigger_study.events import EXCLUDED_SECTORS, _systrader_conn, load_membership
from fundamentals.trigger_study.filings import _JS, wait_until_quiet
from utils.cdp import connect_over_cdp
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.nse_rate_limiter import nse_goto, nse_request_gate

CANDIDATE_TABLE = "fundamentals_order_candidate"
CLASSIFIED_TABLE = "fundamentals_order_classified"
CLASSIFY_VERSION = 1
CLASSIFY_BATCH = 15
ATTACH_TABLE = "fundamentals_order_attachment"
ATTACH_PAGES = 3
ATTACH_CHARS = 6000
FETCH_TABLE = "fundamentals_order_fetch"
FROM_DATE, TO_DATE = "01-01-2018", "31-12-2025"
LIQ_MIN_RS = 5e7
# the pre-registration's prefilter words; "in order to" / "order of the court" style phrases
# are left to the classifier, which sees every candidate
_ORDER_RX = re.compile(r"\b(orders?|contracts?|letters? of (award|acceptance|intent)|lo[ai]s?|work orders?|"
                       r"purchase orders?|bagged|secured|awarded|award of)\b", re.I)


def ensure_tables(cand_table: str = CANDIDATE_TABLE, fetch_table: str = FETCH_TABLE) -> None:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {cand_table} (
                filing_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, announced_at TIMESTAMP NOT NULL,
                nse_category TEXT, text TEXT, attachment_url TEXT, company_name TEXT,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        cur.execute(f"CREATE INDEX IF NOT EXISTS {cand_table}_sym_dt ON {cand_table} (symbol, announced_at)")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {fetch_table} (
                symbol TEXT PRIMARY KEY, fetched_at TIMESTAMPTZ NOT NULL, filings INT, candidates INT, error TEXT)""")


def universe() -> list[str]:
    """NSE EQ symbols with a month whose median daily traded value met the floor in 2018-2025
    (a superset: the exact 60-session floor is applied at each event), outside the excluded
    sectors."""
    sql = """SELECT symbol FROM (
                SELECT symbol, date_trunc('month', date) AS m,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY adj_close * adj_volume) AS tv
                  FROM advisory_adjusted_ohlcv_daily
                 WHERE series = 'EQ' AND date >= '2018-01-01' AND date <= '2025-12-31'
                 GROUP BY 1, 2) x
              WHERE tv >= :floor GROUP BY symbol ORDER BY symbol"""
    with _systrader_conn() as conn:
        syms = pd.read_sql(sql_text(sql), conn, params={"floor": LIQ_MIN_RS})["symbol"].tolist()
    sector = load_membership()["sector_code"].to_dict()
    return [s for s in syms if sector.get(s) not in EXCLUDED_SECTORS]


# not order WINS (pre-registration section 6, parsing fix 2026-11-02): the exchange's own
# volume/price-movement queries, "in order to", and orders passed AGAINST the company
_BOILERPLATE = re.compile(r"^(the exchange has \w+ )?significant (increase in volume|movement in price) has been observed", re.I)
_NOT_RX = re.compile(r"\bin order to\b|\borders? passed\b|\b(court|tax|assessment|tribunal|nclt|sebi|demand|penalty)\s+orders?\b"
                     r"|\borders? (of|from|by) the (hon|court|tribunal|adjudicating)", re.I)


def is_candidate(category: str | None, text: str | None) -> bool:
    t = (text or "").strip()
    if _BOILERPLATE.match(t):
        return False
    return bool(_ORDER_RX.search(_NOT_RX.sub(" ", f"{category or ''} {t}")))


def _rows(symbol: str, payload: list[dict], keep=None) -> pd.DataFrame:
    keep = keep or is_candidate
    out = []
    for r in payload:
        when = pd.to_datetime(r.get("an_dt") or r.get("sort_date"), format="mixed", dayfirst=True, errors="coerce")
        text = (r.get("attchmntText") or "").strip()
        if pd.isna(when) or not keep(r.get("desc"), text):
            continue
        key = "|".join([symbol, str(when), r.get("desc") or "", r.get("attchmntFile") or "", text])
        out.append({"filing_id": hashlib.sha1(key.encode()).hexdigest(), "symbol": symbol,
                    "announced_at": when.to_pydatetime(), "nse_category": r.get("desc"), "text": text,
                    "attachment_url": r.get("attchmntFile"), "company_name": r.get("sm_name")})
    return pd.DataFrame(out).drop_duplicates("filing_id") if out else pd.DataFrame()


def fetch(limit: int | None = None, keep=None, cand_table: str = CANDIDATE_TABLE, fetch_table: str = FETCH_TABLE) -> dict:
    """`keep` / the tables default to row 84's; row 89 (l1_bids) passes its L1 prefilter and tables."""
    ensure_tables(cand_table, fetch_table)
    done = set(sql_to_df(f"SELECT symbol FROM {fetch_table} WHERE error IS NULL")["symbol"])  # noqa: S608
    todo = [s for s in universe() if s not in done]
    todo = todo[:limit] if limit else todo
    if not todo:
        return {"todo": 0}
    env = Env()
    env.read_env()
    counts = {"todo": len(todo), "fetched": 0, "failed": 0, "candidates": 0}
    with sync_playwright() as p:
        browser = connect_over_cdp(p, env("CDP_ENDPOINT"), caller="fundamentals.trigger_study.orders")
        page = (browser.contexts[0] if browser.contexts else browser.new_context()).new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            page.wait_for_timeout(1500)
            for sym in todo:
                if wait_until_quiet():
                    nse_goto(page, "https://www.nseindia.com")
                url = (f"https://www.nseindia.com/api/corporate-announcements?index=equities"
                       f"&symbol={sym}&from_date={FROM_DATE}&to_date={TO_DATE}")
                err, n_all, n_cand = None, 0, 0
                try:
                    with nse_request_gate():
                        payload = page.evaluate(_JS, url)
                    if isinstance(payload, dict):
                        err = f"HTTP {payload.get('__error')}"
                    else:
                        n_all = len(payload or [])
                        df = _rows(sym, payload or [], keep)
                        n_cand = len(df)
                        if n_cand:
                            upsert_to_db(df, cand_table, ["filing_id"])
                except Exception as exc:  # noqa: BLE001 -- one company's failure must not stop the rest
                    err = str(exc)[:200]
                counts["failed" if err else "fetched"] += 1
                counts["candidates"] += n_cand
                upsert_to_db(pd.DataFrame([{"symbol": sym, "fetched_at": pd.Timestamp.now(tz="UTC"), "filings": n_all,
                                            "candidates": n_cand, "error": err}]), fetch_table, ["symbol"])
        finally:
            page.close()
    return counts


# --- step 2: classification and value (outcome-blind) ------------------------------------

CLASSIFY_PROMPT = """You read short Indian stock-exchange announcements. For each, decide whether it reports that the listed company ITSELF has WON a NEW order or contract (an order, contract, letter of award / acceptance / intent, work order or purchase order received from a customer).

Answer "order_win": false for: an order the company PLACED with a supplier; a tender it only bid for or was declared L1 in without an award; a repeat mention or clarification of an order already announced (say "repeat"); a court, tax, regulatory or tribunal order; an MoU or agreement with no order value; a subsidiary's order unless the text says it is the group's or consolidated.
When it is an order win, copy the order's stated value EXACTLY as printed ("value") with its unit ("crore", "lakh", "million", "billion", "thousand" or "" if the number is written in full), the currency ("INR", "USD", "EUR", "GBP", "AED", "JPY" or other code) and the sentence that states it ("quote", verbatim). If several orders are listed with a total, use the total. If no value is stated, leave value empty. Keep the "id" unchanged."""

CLASSIFY_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {
        "id": {"type": "string"}, "order_win": {"type": "boolean"}, "repeat": {"type": "boolean"},
        "value": {"type": ["string", "null"]}, "unit": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]}, "quote": {"type": ["string", "null"]}},
    "required": ["id", "order_win"]}}}, "required": ["items"]}

_UNIT_CR = {"crore": 1.0, "crores": 1.0, "cr": 1.0, "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
            "million": 0.1, "mn": 0.1, "billion": 100.0, "bn": 100.0, "thousand": 1e-4, "": 1e-7}
_PER_100 = {"JPY", "IDR"}       # RBI quotes these per 100 units


def to_crore(value: str | None, unit: str | None, currency: str | None, fx: dict[str, float]) -> float | None:
    """'1,250.5' crore INR -> 1250.5; '45' million USD -> 45 x 0.1 x the RBI rate. None if unknown."""
    from fundamentals.themes.segment_reader import parse_amount
    raw = (value or "").replace("Rs.", "").replace("Rs", "").replace("INR", "").strip()
    raw = re.split(r"\s*(?:-|–|to)\s*", raw)[0].rstrip("+ ")    # a range: its lower bound (conservative)
    u = (unit or "").strip().lower().rstrip(".")
    if u == "thousand":
        raw = re.sub(r"[kK]$", "", raw)                          # "96K" with the unit already thousand
    v = parse_amount(raw)
    if v is None or v <= 0 or u not in _UNIT_CR:
        return None
    cur = (currency or "INR").strip().upper()
    rate = 1.0 if cur in ("INR", "RS", "") else fx.get(cur.lower())
    if rate is None or pd.isna(rate):
        return None
    if cur in _PER_100:
        rate = rate / 100.0
    return v * _UNIT_CR[u] * rate


def validate_classified(x: dict, text: str, fx: dict[str, float]) -> dict:
    from fundamentals.themes.tagger import quote_in
    win = bool(x.get("order_win")) and not x.get("repeat")
    out = {"order_win": win, "repeat": bool(x.get("repeat")), "value_raw": x.get("value"), "unit": x.get("unit"),
           "currency": x.get("currency"), "quote": x.get("quote"), "value_cr": None, "value_note": None}
    if not win:
        return out
    if not x.get("value"):
        out["value_note"] = "no value stated"
        return out
    q = x.get("quote") or ""
    digits = re.sub(r"[^0-9.]", "", x["value"])
    if not (q and quote_in(q, text) and digits and digits.rstrip(".") in re.sub(r"[^0-9.]", "", q)):
        out["value_note"] = "value or its sentence not found in the text"
        return out
    out["value_cr"] = to_crore(x["value"], x.get("unit"), x.get("currency"), fx)
    if out["value_cr"] is None:
        out["value_note"] = f"unit/currency not converted ({x.get('unit')!r}, {x.get('currency')!r})"
    return out


def _fx(dates: list) -> dict:
    df = sql_to_df("SELECT date::date AS d, usd, gbp, eur, jpy, aed, idr FROM rbi_currency_rates"
                   " WHERE date >= '2017-12-01' ORDER BY date")
    df["d"] = pd.to_datetime(df["d"])
    df = df.set_index("d").sort_index().ffill()
    out = {}
    for d in dates:
        row = df[df.index <= pd.Timestamp(d)].tail(1)
        out[d] = row.iloc[0].to_dict() if len(row) else {}
    return out


def classify(limit: int | None = None, any_time: bool = False) -> dict:
    from datetime import datetime, timezone

    from fundamentals.themes.tagger import call_claude
    from fundamentals.trigger_study.llm_runner import in_night_window
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {CLASSIFIED_TABLE} (
                filing_id TEXT NOT NULL, classify_version INT NOT NULL, symbol TEXT NOT NULL,
                announced_at TIMESTAMP NOT NULL, order_win BOOLEAN NOT NULL, repeat BOOLEAN,
                value_raw TEXT, unit TEXT, currency TEXT, quote TEXT, value_cr DOUBLE PRECISION,
                value_note TEXT, classified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (filing_id, classify_version))""")
    todo = sql_to_df(f"""SELECT c.filing_id, c.symbol, c.announced_at, c.nse_category, c.text
                           FROM {CANDIDATE_TABLE} c
                          WHERE NOT EXISTS (SELECT 1 FROM {CLASSIFIED_TABLE} k
                                             WHERE k.filing_id = c.filing_id AND k.classify_version = %(v)s)
                          ORDER BY c.filing_id""", params={"v": CLASSIFY_VERSION})  # noqa: S608
    if todo.empty:
        return {"todo": 0}
    todo = todo[[is_candidate(a, b) for a, b in zip(todo["nse_category"], todo["text"])]]
    if todo.empty:
        return {"todo": 0}
    todo = todo.head(limit) if limit else todo
    fx = _fx(sorted(set(pd.to_datetime(todo["announced_at"]).dt.normalize())))
    counts = {"todo": len(todo), "done": 0, "wins": 0, "with_value": 0, "failed_calls": 0}
    rows = list(todo.itertuples(index=False))
    for i in range(0, len(rows), CLASSIFY_BATCH):
        batch = rows[i:i + CLASSIFY_BATCH]
        res = call_claude(CLASSIFY_PROMPT, CLASSIFY_SCHEMA,
                          [{"id": str(k), "category": r.nse_category, "text": (r.text or "")[:3000]} for k, r in enumerate(batch)])
        if not res["ok"]:
            counts["failed_calls"] += 1
            if res["kind"] == "limit":
                counts["stopped"] = res["message"][:200]
                break
            continue
        got = {str(x.get("id")): x for x in res["items"]}
        out = []
        for k, r in enumerate(batch):
            if str(k) not in got:
                continue
            v = validate_classified(got[str(k)], r.text or "", fx.get(pd.Timestamp(r.announced_at).normalize(), {}))
            out.append({"filing_id": r.filing_id, "classify_version": CLASSIFY_VERSION, "symbol": r.symbol,
                        "announced_at": r.announced_at, **v, "classified_at": pd.Timestamp.now(tz="UTC")})
            counts["wins"] += v["order_win"]
            counts["with_value"] += v["value_cr"] is not None
        if out:
            upsert_to_db(pd.DataFrame(out), CLASSIFIED_TABLE, ["filing_id", "classify_version"])
        counts["done"] += len(out)
    return counts


# --- step 2b: the attached PDF, for wins whose summary text states no value ------------------
# Pre-registration change log 2026-11-03: NSE's summary text carries the order value for only
# 37% of wins (49% in 2018-22, 21-25% in 2024-25), so the check years would rest on a thin and
# differently-selected sample. The filing's own PDF is read for every win without a value, before
# any return is computed.

def _pdf_text(pdf: bytes) -> str:
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(pdf)
        fh.flush()
        out = subprocess.run(["pdftotext", "-l", str(ATTACH_PAGES), "-layout", fh.name, "-"],
                             capture_output=True, text=True, timeout=120)
    return re.sub(r"[ \t]+", " ", out.stdout)[:ATTACH_CHARS] if out.returncode == 0 else ""


def _pdf_from_zip(data: bytes) -> bytes:
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".pdf")]
            return z.read(names[0]) if names else b""
    except zipfile.BadZipFile:
        return b""


def attachments(limit: int | None = None, classified_table: str = CLASSIFIED_TABLE, cand_table: str = CANDIDATE_TABLE,
                attach_table: str = ATTACH_TABLE, flag: str = "order_win") -> dict:
    with db_session() as (_, cur):
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {attach_table} (
                filing_id TEXT PRIMARY KEY, status TEXT NOT NULL, text TEXT, error TEXT,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    todo = sql_to_df(f"""SELECT k.filing_id, c.attachment_url FROM {classified_table} k
                           JOIN {cand_table} c USING (filing_id)
                          WHERE k.classify_version = %(v)s AND k.{flag} AND k.value_cr IS NULL
                            AND c.attachment_url LIKE 'http%%'
                            AND NOT EXISTS (SELECT 1 FROM {attach_table} a WHERE a.filing_id = k.filing_id
                                             AND a.status IN ('ok', 'no_text', 'not_pdf'))
                          ORDER BY k.announced_at""", params={"v": CLASSIFY_VERSION})  # noqa: S608
    todo = todo.head(limit) if limit else todo
    if todo.empty:
        return {"todo": 0}
    env = Env()
    env.read_env()
    counts = {"todo": len(todo), "ok": 0, "no_text": 0, "failed": 0}
    with sync_playwright() as p:
        browser = connect_over_cdp(p, env("CDP_ENDPOINT"), caller="fundamentals.trigger_study.orders.attachments")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            nse_goto(page, "https://www.nseindia.com")
            page.wait_for_timeout(1500)
            for r in todo.itertuples(index=False):
                if wait_until_quiet():
                    nse_goto(page, "https://www.nseindia.com")
                row = {"filing_id": r.filing_id, "status": "fetch_error", "text": None, "error": None}
                try:
                    with nse_request_gate():
                        resp = ctx.request.get(r.attachment_url, timeout=60_000)
                    body = resp.body() if resp.ok else b""
                    if body.startswith(b"PK"):            # older filings: a zip holding the PDF
                        body = _pdf_from_zip(body)
                    if not resp.ok:
                        row["error"] = f"HTTP {resp.status}"
                    elif not body.startswith(b"%PDF"):
                        row["status"] = "not_pdf"
                    else:
                        t = _pdf_text(body)
                        row.update(status="ok" if len(t.strip()) > 50 else "no_text", text=t or None)
                except Exception as exc:  # noqa: BLE001 -- one file must not stop the rest
                    row["error"] = str(exc)[:200]
                counts["ok" if row["status"] == "ok" else "no_text" if row["status"] in ("no_text", "not_pdf") else "failed"] += 1
                upsert_to_db(pd.DataFrame([{**row, "fetched_at": pd.Timestamp.now(tz="UTC")}]), attach_table, ["filing_id"])
        finally:
            page.close()
    return counts


def classify_attachments(limit: int | None = None, any_time: bool = False, classified_table: str = CLASSIFIED_TABLE,
                         cand_table: str = CANDIDATE_TABLE, attach_table: str = ATTACH_TABLE, flag: str = "order_win",
                         prompt: str | None = None, schema: dict | None = None, validate=None) -> dict:
    """Re-read the wins without a value with the PDF's text added; same prompt and checks. The
    value (and its sentence) may now come from the PDF; the row records value_source."""
    from datetime import datetime, timezone

    from fundamentals.themes.tagger import call_claude
    from fundamentals.trigger_study.llm_runner import in_night_window
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    with db_session() as (_, cur):
        cur.execute(f"ALTER TABLE {classified_table} ADD COLUMN IF NOT EXISTS value_source TEXT")
    todo = sql_to_df(f"""SELECT k.filing_id, k.symbol, k.announced_at, c.nse_category, c.text, a.text AS pdf
                           FROM {classified_table} k JOIN {cand_table} c USING (filing_id)
                           JOIN {attach_table} a USING (filing_id)
                          WHERE k.classify_version = %(v)s AND k.{flag} AND k.value_cr IS NULL
                            AND a.status = 'ok' AND k.value_source IS DISTINCT FROM 'attachment'
                          ORDER BY k.filing_id""", params={"v": CLASSIFY_VERSION})  # noqa: S608
    todo = todo.head(limit) if limit else todo
    if todo.empty:
        return {"todo": 0}
    fx = _fx(sorted(set(pd.to_datetime(todo["announced_at"]).dt.normalize())))
    counts = {"todo": len(todo), "done": 0, "with_value": 0, "failed_calls": 0}
    rows = list(todo.itertuples(index=False))
    for i in range(0, len(rows), 8):
        batch = rows[i:i + 8]
        texts = [f"{r.text or ''}\n\nATTACHED LETTER:\n{r.pdf}" for r in batch]
        res = call_claude(prompt or CLASSIFY_PROMPT, schema or CLASSIFY_SCHEMA,
                          [{"id": str(k), "category": r.nse_category, "text": t} for k, (r, t) in enumerate(zip(batch, texts))])
        if not res["ok"]:
            counts["failed_calls"] += 1
            if res["kind"] == "limit":
                counts["stopped"] = res["message"][:200]
                break
            continue
        got = {str(x.get("id")): x for x in res["items"]}
        out = []
        for k, (r, t) in enumerate(zip(batch, texts)):
            if str(k) not in got:
                continue
            v = (validate or validate_classified)(got[str(k)], t, fx.get(pd.Timestamp(r.announced_at).normalize(), {}))
            out.append({"filing_id": r.filing_id, "classify_version": CLASSIFY_VERSION, "symbol": r.symbol,
                        "announced_at": r.announced_at, **v, "value_source": "attachment",
                        "classified_at": pd.Timestamp.now(tz="UTC")})
            counts["with_value"] += v["value_cr"] is not None
        if out:
            upsert_to_db(pd.DataFrame(out), classified_table, ["filing_id", "classify_version"])
        counts["done"] += len(out)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LEDGER row 84: order-win announcements.")
    ap.add_argument("step", choices=["fetch", "classify", "attachments", "classify-attachments"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--any-time", action="store_true")
    a = ap.parse_args(argv)
    out = {"fetch": lambda: fetch(limit=a.limit), "classify": lambda: classify(limit=a.limit, any_time=a.any_time),
           "attachments": lambda: attachments(limit=a.limit),
           "classify-attachments": lambda: classify_attachments(limit=a.limit, any_time=a.any_time)}[a.step]()
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
