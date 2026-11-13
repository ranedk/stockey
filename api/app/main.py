"""Public API for the research platform (PLATFORM_PRD.md; stage 0 read surface).

Run: api/.venv/bin/uvicorn app.main:app --port 8110   (from api/)
"""
from __future__ import annotations

import math
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query

from . import db
from .catalog import FIELDS, NUMERIC, TAG_RULES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(title="Research platform API", version="0.1", lifespan=lifespan)

COMPANY_COLS = ", ".join(FIELDS)
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-_.]{1,20}$")


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _row(r: dict) -> dict:
    return {k: _clean(v) for k, v in r.items()}


def _symbol(s: str) -> str:
    s = s.upper()
    if not SYMBOL_RE.match(s):
        raise HTTPException(400, "bad symbol")
    return s


@app.get("/api/v1/health")
def health():
    r = db.one("SELECT max(built_at) AS built_at, count(*) AS companies FROM serving_company")
    return {"ok": True, **r}


@app.get("/api/v1/catalog")
def catalog():
    """Every field a list or screen can use, with its meaning (task S2, first cut)."""
    return {"fields": [{"key": k, **v} for k, v in FIELDS.items()], "tags": TAG_RULES}


@app.get("/api/v1/search")
def search(q: str = Query(..., min_length=1, max_length=40), limit: int = 8):
    like = f"%{q.strip()}%"
    return db.rows("""SELECT symbol, name, industry, size_band, mcap, price, ret_1d
                        FROM serving_company
                       WHERE symbol ILIKE %(like)s OR name ILIKE %(like)s
                       ORDER BY (upper(symbol) = upper(%(q)s)) DESC, (symbol ILIKE %(pre)s) DESC,
                                mcap DESC NULLS LAST LIMIT %(limit)s""",
                   {"like": like, "q": q.strip(), "pre": f"{q.strip()}%", "limit": min(limit, 20)})


OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "="}


@app.get("/api/v1/companies")
def companies(f: list[str] = Query(default=[]), sort: str = "mcap", order: str = "desc",
              limit: int = 50, offset: int = 0, q: str | None = None):
    """List/screen. Filters: f=field:op:value (op gt/gte/lt/lte/eq/in/has). Fields are
    whitelisted from the catalogue; values are always bound parameters."""
    where, params = ["true"], {}
    for i, raw in enumerate(f):
        try:
            field, op, val = raw.split(":", 2)
        except ValueError:
            raise HTTPException(400, f"filter must be field:op:value, got {raw!r}")
        if field not in FIELDS:
            raise HTTPException(400, f"unknown field {field}")
        p = f"p{i}"
        if op == "has" and field == "tags":
            where.append(f"tags ? %({p})s")
            params[p] = val
        elif op == "in":
            where.append(f"{field} = ANY(%({p})s)")
            params[p] = val.split(",")
        elif op in OPS:
            where.append(f"{field} {OPS[op]} %({p})s")
            params[p] = float(val) if field in NUMERIC else val
        else:
            raise HTTPException(400, f"unknown op {op}")
    if q:
        where.append("(symbol ILIKE %(q)s OR name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if sort not in FIELDS:
        raise HTTPException(400, f"unknown sort field {sort}")
    direction = "ASC" if order == "asc" else "DESC"
    params.update(limit=max(1, min(limit, 500)), offset=max(0, offset))
    w = " AND ".join(where)
    total = db.one(f"SELECT count(*) AS n FROM serving_company WHERE {w}", params)["n"]
    out = db.rows(f"""SELECT {COMPANY_COLS} FROM serving_company WHERE {w}
                      ORDER BY {sort} {direction} NULLS LAST, symbol
                      LIMIT %(limit)s OFFSET %(offset)s""", params)
    return {"total": total, "rows": [_row(r) for r in out]}


@app.get("/api/v1/companies/{symbol}")
def company(symbol: str):
    s = _symbol(symbol)
    r = db.one("SELECT * FROM serving_company WHERE symbol = %s", (s,))
    if not r:
        raise HTTPException(404, "company not found")
    peers = []
    if r.get("basic_industry"):
        peers = db.rows(f"""SELECT {COMPANY_COLS} FROM serving_company
                             WHERE basic_industry = %s ORDER BY mcap DESC NULLS LAST LIMIT 12""",
                        (r["basic_industry"],))
    rank = None
    if r.get("industry"):
        rank = db.one("""SELECT count(*) FILTER (WHERE mcap > %s) + 1 AS rank, count(*) AS of
                           FROM serving_company WHERE industry = %s""", (r["mcap"], r["industry"]))
    medians = None
    if r.get("industry"):
        cols = ["pe", "pb", "roce", "roe", "opm", "npm", "sales_yoy", "profit_yoy", "debt_equity",
                "ret_1y", "ret_3m", "sales_cagr3", "dividend_yield", "ev_ebitda"]
        medians = db.one("SELECT count(*) AS n, " + ", ".join(
            f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {c}) AS {c}" for c in cols)
            + " FROM serving_company WHERE industry = %s AND sales IS NOT NULL", (r["industry"],))
    profile = db.one("""SELECT p.about, p.key_points FROM fundamentals_company_profile p
                         JOIN dim_security d ON d.company_master_id = p.company_master_id
                        WHERE d.symbol = %s AND p.about IS NOT NULL
                        ORDER BY p.fetched_on DESC LIMIT 1""", (s,))
    return {"company": _row(r), "peers": [_row(p) for p in peers], "industry_rank": rank,
            "industry_median": _row(medians) if medians else None,
            "profile": profile, "tag_rules": TAG_RULES}


RANGES = {"6m": 183, "1y": 366, "3y": 3 * 366, "5y": 5 * 366, "max": 20 * 366}


@app.get("/api/v1/companies/{symbol}/prices")
def prices(symbol: str, range: str = "1y"):
    s = _symbol(symbol)
    days = RANGES.get(range)
    if days is None:
        raise HTTPException(400, "range must be one of " + ", ".join(RANGES))
    # 200 extra sessions so the 200-day average is defined from the first visible bar
    since = date.today() - timedelta(days=days + 300)
    out = db.rows("""SELECT date::date AS d, adj_open AS o, adj_high AS h, adj_low AS l, adj_close AS c,
                            adj_volume AS v
                       FROM advisory_adjusted_ohlcv_daily
                      WHERE symbol = %s AND date >= %s AND series IN ('EQ','BE','BZ','SM','ST')
                      ORDER BY date""", (s, since))
    seen, bars = set(), []
    for b in out:
        if b["d"] in seen:
            continue
        seen.add(b["d"])
        bars.append(b)
    return {"symbol": s, "range": range, "visible_from": (date.today() - timedelta(days=days)).isoformat(),
            "bars": bars}


@app.get("/api/v1/companies/{symbol}/financials")
def financials(symbol: str, period: str = "q", basis: str | None = None):
    s = _symbol(symbol)
    if period not in ("q", "fy", "ttm"):
        raise HTTPException(400, "period must be q, fy or ttm")
    bases = [b["basis"] for b in db.rows(
        "SELECT DISTINCT basis FROM serving_financials WHERE symbol = %s", (s,))]
    if not bases:
        return {"symbol": s, "bases": [], "basis": None, "rows": []}
    if basis not in bases:
        basis = "consolidated" if "consolidated" in bases else bases[0]
    out = db.rows("""SELECT * FROM serving_financials WHERE symbol = %s AND basis = %s AND period = %s
                     ORDER BY period_end""", (s, basis, period))
    return {"symbol": s, "bases": bases, "basis": basis, "period": period,
            "rows": [_row({k: v for k, v in r.items() if v is not None}) for r in out]}


DASH_INDICES = ["Nifty 50", "Nifty Bank", "Nifty Midcap 150", "Nifty Smallcap 250", "Nifty 500", "India VIX"]
SECTOR_INDICES = ["Nifty IT", "Nifty Bank", "Nifty Auto", "Nifty Pharma", "Nifty FMCG", "Nifty Metal",
                  "Nifty Realty", "Nifty Energy", "Nifty Media", "Nifty PSU Bank", "Nifty India Defence",
                  "Nifty Capital Markets", "Nifty Chemicals", "Nifty Healthcare Index",
                  "Nifty Consumer Durables", "Nifty Oil & Gas", "Nifty Infrastructure", "Nifty Private Bank"]


def _index_series(names: list[str], days: int) -> dict[str, list[dict]]:
    since = date.today() - timedelta(days=days)
    out = db.rows("""SELECT index_name, date::date AS d, close FROM nseindia_indices
                      WHERE index_name = ANY(%s) AND date >= %s ORDER BY index_name, date""", (names, since))
    by: dict[str, list] = {}
    for r in out:
        by.setdefault(r["index_name"], []).append({"d": r["d"], "c": r["close"]})
    return by


def _chg(series: list[dict], n: int):
    if len(series) <= n or not series[-1 - n]["c"]:
        return None
    return series[-1]["c"] / series[-1 - n]["c"] - 1


@app.get("/api/v1/market/overview")
def market_overview():
    ser = _index_series(DASH_INDICES + SECTOR_INDICES, 400)
    indices = []
    for n in DASH_INDICES:
        s = ser.get(n, [])
        if s:
            indices.append({"name": n, "close": s[-1]["c"], "date": s[-1]["d"], "chg_1d": _chg(s, 1),
                            "chg_1m": _chg(s, 21), "chg_1y": _chg(s, 250), "spark": [x["c"] for x in s[-120:]]})
    sectors = []
    for n in SECTOR_INDICES:
        s = ser.get(n, [])
        if s:
            sectors.append({"name": n.replace("Nifty ", "").replace(" Index", ""), "index": n,
                            "chg_1d": _chg(s, 1), "chg_1w": _chg(s, 5), "chg_1m": _chg(s, 21),
                            "chg_3m": _chg(s, 63), "chg_1y": _chg(s, 250)})
    breadth = db.one("""SELECT count(*) FILTER (WHERE ret_1d > 0) AS adv, count(*) FILTER (WHERE ret_1d < 0) AS dec,
                               count(*) FILTER (WHERE ret_1d = 0) AS unch,
                               count(*) FILTER (WHERE from_high_52w >= -0.02) AS near_high,
                               count(*) FILTER (WHERE price <= low_52w * 1.02) AS near_low,
                               count(*) FILTER (WHERE dma200_gap > 0) AS above_200,
                               count(*) FILTER (WHERE dma200_gap IS NOT NULL) AS with_200,
                               max(price_date) AS as_of
                          FROM serving_company WHERE traded_value_20d > 1e7""")
    stages = db.rows("""SELECT stage, count(*) AS n FROM serving_company
                         WHERE stage IS NOT NULL AND traded_value_20d > 1e7 GROUP BY stage ORDER BY stage""")
    cols = "symbol, name, industry, size_band, price, ret_1d, ret_1m, mcap, spark"
    liquid = "traded_value_20d > 5e7 AND ret_1d IS NOT NULL"
    gainers = db.rows(f"SELECT {cols} FROM serving_company WHERE {liquid} ORDER BY ret_1d DESC LIMIT 8")
    losers = db.rows(f"SELECT {cols} FROM serving_company WHERE {liquid} ORDER BY ret_1d ASC LIMIT 8")
    industries = db.rows("""SELECT sector, count(*) AS n, sum(mcap) AS mcap,
                                   percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1d) AS ret_1d,
                                   percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1m) AS ret_1m,
                                   percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1y) AS ret_1y,
                                   avg((stage = 2)::int) AS share_uptrend
                              FROM serving_company WHERE sector IS NOT NULL AND traded_value_20d > 1e7
                             GROUP BY sector HAVING count(*) >= 5 ORDER BY sum(mcap) DESC""")
    sizes = db.rows("""SELECT size_band, count(*) AS n,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1d) AS ret_1d,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1m) AS ret_1m,
                              percentile_cont(0.5) WITHIN GROUP (ORDER BY ret_1y) AS ret_1y
                         FROM serving_company WHERE traded_value_20d > 1e7 GROUP BY size_band""")
    order = {"Large": 0, "Mid": 1, "Small": 2, "Micro": 3}
    sizes.sort(key=lambda x: order.get(x["size_band"], 9))
    return {"indices": indices, "sectors": sectors, "breadth": breadth, "stages": stages, "sizes": sizes,
            "gainers": [_row(r) for r in gainers], "losers": [_row(r) for r in losers],
            "industries": [_row(r) for r in industries]}


@app.get("/api/v1/market/index")
def index_prices(name: str, range: str = "1y"):
    days = RANGES.get(range, 366)
    return {"name": name, "bars": _index_series([name], days).get(name, [])}


@app.get("/api/v1/meta/freshness")
def freshness():
    return {
        "prices": db.one("SELECT max(price_date) AS as_of FROM serving_company"),
        "financials": db.one("SELECT max(public_at) AS as_of FROM serving_financials"),
        "stage": db.one("SELECT max(stage_as_of) AS as_of FROM serving_company"),
        "built_at": db.one("SELECT max(built_at) AS at FROM serving_company"),
        "now": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/v1/meta/classes")
def classes():
    """Sector and industry names with company counts, for filters."""
    sectors = db.rows("""SELECT sector AS name, count(*) AS n FROM serving_company
                          WHERE sector IS NOT NULL GROUP BY sector ORDER BY sector""")
    industries = db.rows("""SELECT industry AS name, sector, count(*) AS n FROM serving_company
                             WHERE industry IS NOT NULL GROUP BY industry, sector ORDER BY industry""")
    return {"sectors": sectors, "industries": industries}
