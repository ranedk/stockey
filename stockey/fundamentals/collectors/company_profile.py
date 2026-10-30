"""Company business profiles from screener.in, for the theme map (docs/THEMATIC_WATCHLIST_PRD.md
section 4.2, step 2).

Per company, from its screener.in page (two requests, both through the shared screener.in
rate gate):
  about        the short "About" paragraph
  key_points   the full "Key Points" commentary (/wiki/company/<id>/commentary/v2/): business
               segments with revenue shares, capacities, capex plans -- the tagger's main
               evidence. `key_points_edited` keeps screener's "Last edited ..." note as-is.
  peers_path   screener's own 4-level industry path (a cross-check, not the theme map)
  documents    links to annual reports, concall transcripts / presentations and credit
               ratings, for the candidate-only document step

Stored dated (`fetched_on`) so a tag can say which profile it read. Universe: NSE and BSE
companies with a combined 60-day median traded value of Rs 1 crore or more. A profile is
re-fetched after REFRESH_DAYS; the first run is the backfill (~1,900 companies, ~10 hours at
the gate's 10 s spacing), resumable because finished companies are skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from urllib.parse import quote
from datetime import date, datetime, timezone

import pandas as pd
from bs4 import BeautifulSoup

from fundamentals.collectors.screenerin import build_authenticated_session
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "fundamentals_company_profile"
BASE = "https://www.screener.in"
REFRESH_DAYS = 90
MIN_MEDIAN_TRADED_VALUE = 1e7      # Rs 1 crore
LIQUIDITY_DAYS = 60
# The nightly screener pipeline (15:30 UTC, 65-109 min) shares the screener.in rate gate, and
# the 22:00 IST portfolio job waits on it with a 3h timeout. The backfill steps aside then.
QUIET_UTC = (15 * 60, 17 * 60 + 45)


def _wait_out_pipeline_window() -> None:
    now = datetime.now(timezone.utc)
    minute = now.hour * 60 + now.minute
    if QUIET_UTC[0] <= minute < QUIET_UTC[1]:
        time.sleep((QUIET_UTC[1] - minute) * 60)

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        company_master_id TEXT NOT NULL,
        fetched_on DATE NOT NULL,
        ticker TEXT NOT NULL,
        status TEXT NOT NULL,
        screener_company_id TEXT,
        about TEXT,
        key_points TEXT,
        key_points_edited TEXT,
        peers_path TEXT,
        documents JSONB,
        error TEXT,
        screener_slug TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (company_master_id, fetched_on)
    )
"""

_COMMENTARY_URL = re.compile(r"/wiki/company/(\d+)/commentary/v2/")


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)
            cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS screener_slug TEXT")

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


def _text(el) -> str | None:
    if el is None:
        return None
    t = el.get_text("\n", strip=True)
    return t or None


def parse_company_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    m = _COMMENTARY_URL.search(html)
    peers = [a.get_text(strip=True) for a in soup.select("#peers a[href*='/market/']")]
    documents: dict[str, list[dict]] = {}
    for sec in soup.select("#documents .documents"):
        h = sec.find("h3")
        if not h:
            continue
        items = []
        for li in sec.select("li"):
            links = [{"label": a.get_text(" ", strip=True), "url": a.get("href")}
                     for a in li.find_all("a") if a.get("href")]
            if links:
                items.append({"title": li.get_text(" ", strip=True)[:300], "links": links})
        documents[h.get_text(strip=True)] = items
    return {
        "screener_company_id": m.group(1) if m else None,
        "about": _text(soup.select_one(".company-profile .about")),
        "peers_path": " > ".join(peers[:4]) or None,
        "documents": documents,
    }


# <br> is NOT a block: screener's key points are pasted text with a <br> at every wrap.
_BLOCKS = ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "div"]


def _block_text(soup) -> list[str]:
    """Text one block per line: inline tags (bold numbers, links) stay inside their
    sentence instead of breaking it across lines."""
    for tag in soup.find_all(_BLOCKS):
        tag.append("\x00")
    raw = soup.get_text(" ")    # newlines inside the source HTML are just wrapping
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\x00")]
    return [re.sub(r"\s*\[\s*(edit|\d+)\s*\]", "", ln).strip() for ln in lines]


# A free screener.in account gets 20 key-point views per 30 days; after that the endpoint
# answers with this notice instead of the text (found 2026-10-27: 1,640 of 1,712 profiles).
_PAYWALL = "upgrade to premium to read more key insights"


class KeyPointsLocked(Exception):
    pass


def parse_commentary(html: str) -> tuple[str | None, str | None, list[str]]:
    """Key points text, the 'Last edited ...' note, and the footnote source links (the
    filings screener cites, e.g. an annual-report page) for the tagger to quote. A response that is a whole company
    page (no XHR answer) has a #peers section; that is a failed fetch, not commentary."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#peers") is not None:
        raise ValueError("commentary endpoint returned a full page")
    sources = list(dict.fromkeys(a["href"] for a in soup.select("sup a[href]")))
    lines = [ln for ln in _block_text(soup) if ln]
    if any(_PAYWALL in ln.lower() for ln in lines):
        raise KeyPointsLocked("screener.in key points need a premium account")
    edited = next((ln for ln in lines if ln.startswith("Last edited")), None)
    keep = [ln for ln in lines if not ln.startswith(("Last edited", "Request an update", "©"))]
    return ("\n".join(keep) or None), edited, sources


def merge_renamed(u: pd.DataFrame) -> pd.DataFrame:
    """One row per company. After a rename company_master holds two rows with the same name
    (ignoring 'Ltd'): the old one keeps the BSE code and the BSE trading, the new one the NSE
    symbol (HBLPOWER/HBLENGINE, ZOMATO/ETERNAL). Keep the busier row that has a real NSE symbol,
    give it the BSE code and the summed turnover, and list the dropped ids in merged_ids."""
    u = u.assign(_k=u["company_name"].map(_name_key), _bom=u["ticker"].str.endswith("-BOM"))
    rows = []
    for _, g in u.groupby("_k", sort=False):
        g = g.sort_values(["_bom", "median_tv"], ascending=[True, False])
        keep = g.iloc[0].copy()
        keep["bse_code"] = g["bse_code"].dropna().iloc[0] if g["bse_code"].notna().any() else None
        keep["median_tv"] = g["median_tv"].sum()
        keep["merged_ids"] = list(g["company_master_id"].iloc[1:])
        rows.append(keep)
    out = pd.DataFrame(rows).drop(columns=["_k", "_bom"])
    return out.sort_values("median_tv", ascending=False).reset_index(drop=True)


def load_universe(as_of: date) -> pd.DataFrame:
    """Companies with combined NSE+BSE median daily traded value >= Rs 1 cr over the last
    LIQUIDITY_DAYS calendar-day window's sessions, largest first, with the screener.in ticker
    (NSE symbol, else BSE scrip code)."""
    lo = (pd.Timestamp(as_of) - pd.Timedelta(days=int(LIQUIDITY_DAYS * 1.5))).date()
    return merge_renamed(sql_to_df(
        """
        WITH d AS (
            SELECT company_master_id, date, sum(total_value) AS tv FROM (
                SELECT company_master_id, date, total_value FROM nseindia_ohlcv
                 WHERE date BETWEEN %(lo)s AND %(hi)s AND company_master_id IS NOT NULL
                   AND series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
                UNION ALL
                SELECT company_master_id, date, total_value FROM bseindia_ohlcv
                 WHERE date BETWEEN %(lo)s AND %(hi)s AND company_master_id IS NOT NULL
            ) x GROUP BY 1, 2
        ), m AS (
            SELECT company_master_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY tv) AS median_tv
              FROM d GROUP BY 1
        )
        SELECT m.company_master_id, m.median_tv, c.company_name, c.bse_scrip_code::text AS bse_code,
               coalesce(nullif(c.nse_ticker, ''), c.bse_scrip_code::text) AS ticker
          FROM m JOIN company_master c USING (company_master_id)
         WHERE m.median_tv >= %(floor)s
           AND c.company_name !~* '(\\metf|\\mtrust)$'
           AND coalesce(nullif(c.nse_ticker, ''), c.bse_scrip_code::text) IS NOT NULL
         ORDER BY m.median_tv DESC
        """,
        params={"lo": lo, "hi": as_of, "floor": MIN_MEDIAN_TRADED_VALUE},
    ))


def due(universe: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Companies with no OK profile in the last REFRESH_DAYS."""
    done = sql_to_df(
        f"SELECT DISTINCT company_master_id FROM {TABLE} WHERE status = 'ok' AND fetched_on > %(since)s",
        params={"since": (pd.Timestamp(as_of) - pd.Timedelta(days=REFRESH_DAYS)).date()},
    )
    return universe[~universe["company_master_id"].isin(set(done["company_master_id"]))]


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(ltd|limited)\b\.?", "", name.lower()))


def slug_candidates(ticker: str, bse_code: str | None) -> list[str]:
    """screener.in page names to try, in order: the NSE symbol (URL-quoted: GET&D), then the
    BSE code -- our NSE symbol goes stale when a company renames (ZOMATO -> ETERNAL), and
    BSE-only names carry a '-BOM' pseudo-symbol screener does not know."""
    out = []
    if ticker and not ticker.endswith("-BOM"):
        out.append(quote(ticker, safe=""))
    if bse_code:
        out.append(str(bse_code))
    return list(dict.fromkeys(out))


def search_slug(session, company_name: str) -> str | None:
    """The page of the search hit whose name matches ours exactly (ignoring 'Ltd' and
    punctuation); a near miss is not taken."""
    with exchange_request_gate(domain="screenerin"):
        r = session.get(f"{BASE}/api/company/search/", params={"q": company_name, "v": 3}, timeout=60)
    r.raise_for_status()
    want = _name_key(company_name)
    for hit in r.json():
        m = re.match(r"/company/([^/]+)/", hit.get("url") or "")
        if m and _name_key(hit.get("name") or "") == want:
            return m.group(1)
    return None


def fetch_profile(session, ticker: str, bse_code: str | None = None, company_name: str | None = None) -> dict:
    r, slug = None, None
    for slug in slug_candidates(ticker, bse_code):
        with exchange_request_gate(domain="screenerin"):
            r = session.get(f"{BASE}/company/{slug}/", timeout=60)
        if r.status_code != 404:
            break
    if (r is None or r.status_code == 404) and company_name:
        slug = search_slug(session, company_name)
        if slug:
            with exchange_request_gate(domain="screenerin"):
                r = session.get(f"{BASE}/company/{slug}/", timeout=60)
    if r is None or r.status_code == 404:
        return {"status": "not_found"}
    r.raise_for_status()
    out = parse_company_page(r.text)
    out["status"], out["screener_slug"] = "ok", slug
    if out["screener_company_id"]:
        with exchange_request_gate(domain="screenerin"):
            c = session.get(
                f"{BASE}/wiki/company/{out['screener_company_id']}/commentary/v2/", timeout=60,
                headers={"X-Requested-With": "XMLHttpRequest", "HX-Request": "true",
                         "Referer": f"{BASE}/company/{slug}/"},
            )
        c.raise_for_status()
        try:
            out["key_points"], out["key_points_edited"], sources = parse_commentary(c.text)
        except KeyPointsLocked as exc:
            # the rest of the page is still good; keep it, and say plainly what is missing
            out["key_points"], out["error"], sources = None, str(exc), []
            record_local_fallback_event(
                module="fundamentals.collectors.company_profile", fallback_type="key_points_locked",
                source="screener.in", symbol=slug, severity="warn", reason=str(exc),
            )
        out["documents"]["Key point sources"] = [{"title": u, "links": [{"label": "source", "url": u}]} for u in sources]
    return out


def _store(row: dict) -> None:
    df = pd.DataFrame([row])
    df["documents"] = df["documents"].map(lambda d: json.dumps(d) if d is not None else None)
    upsert_to_db(df, TABLE, ["company_master_id", "fetched_on"])


def run(limit: int | None = None, as_of: date | None = None) -> dict:
    as_of = as_of or pd.Timestamp.now(tz="Asia/Kolkata").date()
    ensure_table()
    todo = due(load_universe(as_of), as_of)
    if limit:
        todo = todo.head(limit)
    session = build_authenticated_session()
    counts = {"due": len(todo), "ok": 0, "not_found": 0, "error": 0}
    for row in todo.itertuples(index=False):
        _wait_out_pipeline_window()
        base = {"company_master_id": row.company_master_id, "fetched_on": as_of, "ticker": row.ticker,
                "screener_company_id": None, "about": None, "key_points": None,
                "key_points_edited": None, "peers_path": None, "documents": None, "error": None,
                "screener_slug": None}
        try:
            got = fetch_profile(session, row.ticker, row.bse_code, row.company_name)
        except Exception as exc:  # one company failing must not stop the backfill
            got = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}
            record_local_fallback_event(
                module="fundamentals.collectors.company_profile", fallback_type="profile_fetch_failed",
                source="screener.in", symbol=row.ticker, severity="warn", reason=got["error"],
            )
        _store({**base, **got})
        counts[got["status"]] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch screener.in business profiles for the theme map.")
    ap.add_argument("--limit", type=int, default=None, help="at most this many companies this run")
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
