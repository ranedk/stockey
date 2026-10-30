"""SEBI enforcement orders (sebi.gov.in > Enforcement > Orders), the new source for the theme
watchlist's management gate (docs/THEMATIC_WATCHLIST_PRD.md section 4.4).

Pure data: per order its date, title, link, the kind of order read off the title, and the
"in the matter of ..." name. Matching orders to companies and deciding what excludes a
company is the gate's job, not this collector's.

The listing is newest first, 25 a page, paged by a POST that needs the session cookie of the
listing page. Every request goes through exchange_request_gate(domain="sebi"). A run reads
pages until it passes the newest order already stored (less a week, for late uploads), or
BACKFILL_FROM on the first run.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from bs4 import BeautifulSoup

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "fundamentals_sebi_order"
BASE = "https://www.sebi.gov.in"
LISTING = f"{BASE}/sebiweb/home/HomeAction.do?doListing=yes&sid=2&ssid=9&smid=0"
PAGE_URL = f"{BASE}/sebiweb/ajax/home/getnewslistinfo.jsp"
PARTY_TABLE = "fundamentals_sebi_order_party"
BACKFILL_FROM = date(2021, 10, 1)    # the gate looks back 5 years
OVERLAP_DAYS = 7
MAX_PAGES = 2000

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        url TEXT PRIMARY KEY,
        order_date DATE NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,            -- see order_kind()
        matter TEXT,                   -- the "in the matter of ..." name, if the title has one
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now()
    )"""


_PARTY_DDL = f"""
    CREATE TABLE IF NOT EXISTS {PARTY_TABLE} (
        url TEXT PRIMARY KEY,
        pdf_url TEXT,
        status TEXT NOT NULL,          -- ok / no_parties (scan, or no "in respect of" block) / no_pdf / fetch_error
        parties TEXT,                  -- the order's "In respect of ..." block: whom the order is against
        fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)
            cur.execute(_PARTY_DDL)
            cur.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_date_idx ON {TABLE} (order_date)")

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


_KINDS = [
    ("appeal", r"^appeal\b"),
    ("settlement", r"settlement order"),
    ("revocation", r"revocation order|order of revocation"),
    ("confirmatory", r"confirmatory order"),
    ("interim", r"interim (ex[- ]parte )?order|ex[- ]parte"),
    ("adjudication", r"adjudication order|order of the adjudicating"),
    ("exemption", r"exemption order|order under regulation 11"),
    ("final", r"final order|^order\b|order in the matter"),
]


def order_kind(title: str) -> str:
    t = title.strip().lower()
    for kind, rx in _KINDS:
        if re.search(rx, t):
            return kind
    return "other"


def matter_of(title: str) -> str | None:
    """'Adjudication Order in the matter of Taurian MPS Ltd.' -> 'Taurian MPS Ltd.'"""
    m = re.search(r"\bin (?:the )?(?:matter|case) of\s+(.+)$", title, re.I)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip(" .-") or None


def parse_page(html: str) -> tuple[list[dict], int | None]:
    """The page's orders, and the total record count printed above them."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        a = tr.find("a", href=True)
        if len(tds) < 2 or a is None:
            continue
        try:
            d = datetime.strptime(tds[0].get_text(strip=True), "%b %d, %Y").date()
        except ValueError:
            continue
        # the title attribute can carry markup ("...<br><a href=...>[Corrigendum]</a>")
        raw = (a.get("title") or a.get_text(" ", strip=True)).split("<")[0]
        title = re.sub(r"\s+", " ", raw).strip()
        rows.append({"url": a["href"].strip(), "order_date": d, "title": title,
                     "kind": order_kind(title), "matter": matter_of(title)})
    m = re.search(r"of\s+([\d,]+)\s+records", soup.get_text(" ", strip=True))
    return rows, int(m.group(1).replace(",", "")) if m else None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
    with exchange_request_gate(domain="sebi"):
        s.get(LISTING, timeout=60).raise_for_status()
    return s


def fetch_page(s: requests.Session, page: int) -> str:
    data = {"nextValue": "1", "next": "n", "search": "", "fromDate": "", "toDate": "", "fromYear": "",
            "toYear": "", "deptId": "", "sid": "2", "ssid": "9", "smid": "0", "ssidhidden": "9", "intmid": "-1",
            "sText": "Enforcement", "ssText": "Orders", "smText": "", "doDirect": str(page)}
    with exchange_request_gate(domain="sebi"):
        r = s.post(PAGE_URL, data=data, timeout=60,
                   headers={"Referer": LISTING, "X-Requested-With": "XMLHttpRequest", "Origin": BASE})
    r.raise_for_status()
    if "Unauthorized Activity" in r.text:
        raise RuntimeError("sebi.gov.in refused the listing request (Unauthorized Activity)")
    return r.text


# --- whom an order is against ----------------------------------------------------------------
# A title names only the matter ("Adjudication order in the matter of Titan Company Limited");
# the order itself says whom it is against ("In respect of: 1 A Rathinappan ... 10 C Santhosh
# Kumar" -- insiders, not Titan). Read on demand, only for orders matched to a company the
# theme watchlist checks, from the PDF's text layer (first PARTY_PAGES pages).
PARTY_PAGES = 3
_PDF_RX = re.compile(r"(https://www\.sebi\.gov\.in/sebi_data/attachdocs/[^\"'\s]+?\.pdf)", re.I)


def parties_of(text: str) -> str | None:
    """The block between "In respect of" and "In the matter of" (or the order's opening, up to
    "In the matter of", when there is no "In respect of"); None when neither is found."""
    flat = re.sub(r"\s+", " ", text)
    m = re.search(r"in\s+respect\s+of\s*:?(.{3,4000}?)\bin\s+the\s+matter\s+of\b", flat, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"^(.{20,3000}?)\bin\s+the\s+matter\s+of\b", flat, re.I)
    return m.group(1).strip() if m else None


def fetch_parties(urls: list[str]) -> int:
    """Read and store the parties of each order not read yet. Returns orders read."""
    ensure_table()
    done = set(sql_to_df(f"SELECT url FROM {PARTY_TABLE} WHERE status <> 'fetch_error'")["url"])  # noqa: S608
    todo = [u for u in dict.fromkeys(urls) if u not in done]
    if not todo:
        return 0
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
    for url in todo:
        row = {"url": url, "pdf_url": None, "status": "fetch_error", "parties": None}
        try:
            with exchange_request_gate(domain="sebi"):
                page = s.get(url, timeout=60)
            page.raise_for_status()
            m = _PDF_RX.search(page.text)
            if not m:
                row["status"] = "no_pdf"
            else:
                row["pdf_url"] = m.group(1)
                with exchange_request_gate(domain="sebi"):
                    pdf = s.get(m.group(1), timeout=120)
                pdf.raise_for_status()
                text = "\n".join(_pdf_text(pdf.content)[:PARTY_PAGES])
                row["parties"] = parties_of(text)
                row["status"] = "ok" if row["parties"] else "no_parties"
        except requests.RequestException as exc:
            record_local_fallback_event(module="fundamentals.collectors.sebi_orders", fallback_type="order_fetch_failed",
                                        source="sebi", symbol=None, severity="warn", reason=f"{url}: {exc}"[:500])
        upsert_to_db(pd.DataFrame([row]).assign(fetched_at=pd.Timestamp.now(tz="UTC")), PARTY_TABLE, ["url"])
    return len(todo)


def _pdf_text(pdf_bytes: bytes) -> list[str]:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(pdf_bytes)
        fh.flush()
        out = subprocess.run(["pdftotext", "-l", str(PARTY_PAGES), "-layout", fh.name, "-"],
                             capture_output=True, text=True, timeout=120)
    return out.stdout.split("\f") if out.returncode == 0 else []


def since_date() -> date:
    got = sql_to_df(f"SELECT max(order_date) AS d FROM {TABLE}")
    d = got["d"].iloc[0] if len(got) else None
    return BACKFILL_FROM if d is None or pd.isna(d) else pd.Timestamp(d).date() - timedelta(days=OVERLAP_DAYS)


def run(since: date | None = None, max_pages: int = MAX_PAGES) -> dict:
    ensure_table()
    since = since or since_date()
    s = _session()
    counts = {"since": str(since), "pages": 0, "orders": 0}
    for page in range(max_pages):
        try:
            rows, total = parse_page(fetch_page(s, page))
        except (requests.RequestException, RuntimeError) as exc:
            record_local_fallback_event(module="fundamentals.collectors.sebi_orders", fallback_type="listing_fetch_failed",
                                        source="sebi", symbol=None, severity="warn", reason=f"page {page}: {exc}"[:500])
            counts["stopped"] = f"page {page}: {exc}"[:300]
            break
        if not rows:
            counts["stopped"] = f"page {page} had no orders"
            break
        keep = [r for r in rows if r["order_date"] >= since]
        if keep:
            upsert_to_db(pd.DataFrame(keep).drop_duplicates("url"), TABLE, ["url"])
        counts["pages"] += 1
        counts["orders"] += len(keep)
        counts["total_listed"] = total
        if min(r["order_date"] for r in rows) < since:
            break
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Collect SEBI enforcement orders (date, title, link).")
    ap.add_argument("--since", type=date.fromisoformat, default=None, help="default: newest stored less a week")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES)
    a = ap.parse_args(argv)
    print(json.dumps(run(since=a.since, max_pages=a.max_pages), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
