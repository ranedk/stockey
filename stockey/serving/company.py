"""One row per listed company for the research platform -> `serving_company` (PLATFORM_PRD.md
tasks S1/S3): identity, industry, price and returns, liquidity, size band, latest financials,
valuation, systrader's stage, and short tags that explain the company at a glance.

Universe: every NSE equity in the latest market-cap file (main board and SME); nothing is
dropped for size (small caps stay in). Financials prefer consolidated where filed.

    python -m serving.company            # build and swap in (after serving.financials)
    python -m serving.company --dry-run
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import urllib.request
from datetime import date, timedelta

import numpy as np
import pandas as pd

from utils.db import db_session, sql_to_df

log = logging.getLogger(__name__)

TABLE = "serving_company"
SERIES = ("EQ", "BE", "BZ", "SM", "ST")
STAGE_URL = "http://127.0.0.1:8090/api/stage"
STAGE_MAX_AGE_DAYS = 5
FIN_COLS = ["sales", "profit", "ebitda", "opm", "npm", "sales_yoy", "profit_yoy", "roe", "roce",
            "debt_equity", "interest_cover", "cfo", "fcf", "cash_conversion", "borrowings", "cash",
            "total_equity", "equity_owners", "dividends_paid"]
RETURNS = {"ret_1d": 1, "ret_1w": 5, "ret_1m": 21, "ret_3m": 63, "ret_6m": 126, "ret_1y": 252}


def _universe() -> pd.DataFrame:
    u = sql_to_df(f"""
        WITH d AS (SELECT max(date) d FROM nseindia_mcap WHERE date > now() - interval '15 days')
        SELECT m.symbol, m.series, m.market_cap_rs AS mcap, m.issue_size AS shares, m.date AS mcap_date
          FROM nseindia_mcap m, d WHERE m.date = d.d AND m.series = ANY(%s)""", (list(SERIES),))
    names = sql_to_df("""SELECT DISTINCT ON (symbol) symbol, isin,
                                coalesce(proper_name, company_name, display_name) AS name, bse_ticker
                           FROM dim_security ORDER BY symbol, is_active_recent DESC NULLS LAST,
                                last_trade_date DESC NULLS LAST""")
    ind = sql_to_df("""SELECT DISTINCT ON (symbol) symbol, macro_sector, sector, industry, basic_industry
                         FROM fundamentals_industry_classification
                        WHERE symbol IS NOT NULL ORDER BY symbol, fetch_date DESC""")
    u = u.sort_values("mcap", ascending=False).drop_duplicates("symbol")
    u = u.merge(names, on="symbol", how="left").merge(ind, on="symbol", how="left")
    u["name"] = u["name"].fillna(u["symbol"])
    u["name"] = u["name"].str.replace(r"\s+(Ltd\.?|Limited)$", "", regex=True)
    return u


def _prices(asof_floor: date) -> pd.DataFrame:
    p = sql_to_df("""SELECT symbol, date, adj_close, close FROM advisory_adjusted_ohlcv_daily
                      WHERE date >= %s AND series = ANY(%s)""", (asof_floor, list(SERIES)))
    p = p.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    liq = sql_to_df("""SELECT symbol, avg(total_value) AS traded_value_20d FROM (
                          SELECT symbol, total_value, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) rn
                            FROM nseindia_ohlcv WHERE date >= %s AND series = ANY(%s)) x
                        WHERE rn <= 20 GROUP BY symbol""", (date.today() - timedelta(days=40), list(SERIES)))
    rows = []
    for sym, g in p.groupby("symbol", sort=False):
        c = g["adj_close"].to_numpy()
        last = g.iloc[-1]
        r = {"symbol": sym, "price": last["close"], "price_date": last["date"]}
        for k, n in RETURNS.items():
            r[k] = c[-1] / c[-1 - n] - 1 if len(c) > n and c[-1 - n] > 0 else np.nan
        yr = c[-252:]
        r["high_52w"], r["low_52w"] = np.nanmax(yr), np.nanmin(yr)
        r["from_high_52w"] = c[-1] / r["high_52w"] - 1
        r["dma50_gap"] = c[-1] / np.mean(c[-50:]) - 1 if len(c) >= 50 else np.nan
        r["dma200_gap"] = c[-1] / np.mean(c[-200:]) - 1 if len(c) >= 200 else np.nan
        daily = np.diff(np.log(c[-64:])) if len(c) > 20 else np.array([])
        r["volatility_3m"] = float(np.std(daily) * np.sqrt(252)) if len(daily) > 10 else np.nan
        r["spark"] = json.dumps([round(float(x), 2) for x in c[-60:]])
        rows.append(r)
    return pd.DataFrame(rows).merge(liq, on="symbol", how="left")


def _financials() -> pd.DataFrame:
    f = sql_to_df(f"""SELECT DISTINCT ON (symbol, basis) symbol, basis, family, period_end AS fin_period,
                             public_at AS fin_public_at, {', '.join(FIN_COLS)}
                        FROM serving_financials WHERE period = 'ttm'
                       ORDER BY symbol, basis, period_end DESC""")
    q = sql_to_df("""SELECT DISTINCT ON (symbol, basis) symbol, basis, sales_yoy AS q_sales_yoy,
                            profit_yoy AS q_profit_yoy, period_end AS q_period
                       FROM serving_financials WHERE period = 'q'
                      ORDER BY symbol, basis, period_end DESC""")
    fy = sql_to_df("""SELECT DISTINCT ON (symbol, basis) symbol, basis, sales_cagr3, profit_cagr3
                        FROM serving_financials WHERE period = 'fy'
                       ORDER BY symbol, basis, period_end DESC""")
    f = f.merge(q, on=["symbol", "basis"], how="left").merge(fy, on=["symbol", "basis"], how="left")
    # consolidated where filed, unless it is older than the standalone series
    f["_rank"] = (f["basis"] == "consolidated").astype(int)
    f = f.sort_values(["symbol", "fin_period", "_rank"], ascending=[True, False, False])
    return f.drop_duplicates("symbol").drop(columns="_rank")


def _stages() -> pd.DataFrame:
    try:
        with urllib.request.urlopen(STAGE_URL, timeout=30) as r:
            rows = json.load(r).get("rows", [])
    except Exception as e:                          # visible, not fatal: the page shows "no stage"
        log.warning("stage API unavailable: %s", e)
        return pd.DataFrame(columns=["symbol", "stage", "stage_label", "stage_as_of"])
    s = pd.DataFrame(rows).rename(columns={"ticker": "symbol", "as_of": "stage_as_of"})
    s["stage_as_of"] = pd.to_datetime(s["stage_as_of"])
    stale = s["stage_as_of"] < pd.Timestamp(date.today() - timedelta(days=STAGE_MAX_AGE_DAYS))
    if stale.any():
        log.warning("stage rows older than %d days dropped: %d", STAGE_MAX_AGE_DAYS, int(stale.sum()))
    return s.loc[~stale, ["symbol", "stage", "stage_label", "stage_as_of"]]


def _size_band(rank: pd.Series) -> pd.Series:
    # SEBI's categories: top 100 large, 101-250 mid, 251-500 small; the rest micro
    return pd.cut(rank, [0, 100, 250, 500, np.inf], labels=["Large", "Mid", "Small", "Micro"]).astype(str)


def tags(r: pd.Series) -> list[str]:
    """Short plain labels; each is a fixed rule (shown with its rule in the app)."""
    t = []
    if r["series"] in ("SM", "ST"):
        t.append("SME")
    if pd.notna(r.get("profit")) and r["profit"] < 0:
        t.append("Loss-making")
    if r.get("family") in ("company",) and pd.notna(r.get("borrowings")) and r.get("borrowings", 1) <= 0.05 * max(r.get("total_equity") or 0, 1):
        t.append("Debt-free")
    elif pd.notna(r.get("debt_equity")) and r["debt_equity"] > 1.5 and r.get("family") == "company":
        t.append("High debt")
    if pd.notna(r.get("roce")) and r["roce"] >= 0.20:
        t.append("High ROCE")
    if pd.notna(r.get("sales_yoy")) and r["sales_yoy"] >= 0.25 and pd.notna(r.get("profit_yoy")) and r["profit_yoy"] >= 0.25:
        t.append("Fast growth")
    if pd.notna(r.get("q_sales_yoy")) and r["q_sales_yoy"] < -0.10:
        t.append("Sales falling")
    if (r.get("family") == "company" and pd.notna(r.get("cfo")) and r["cfo"] < 0
            and pd.notna(r.get("profit")) and r["profit"] > 0):     # lenders' cash flow is negative by nature
        t.append("Cash burn")
    if pd.notna(r.get("from_high_52w")) and r["from_high_52w"] >= -0.05:
        t.append("Near 52w high")
    if pd.notna(r.get("from_high_52w")) and r["from_high_52w"] <= -0.40:
        t.append("Far below high")
    if r.get("stage") == 2:
        t.append("Uptrend")
    elif r.get("stage") == 4:
        t.append("Downtrend")
    if pd.notna(r.get("dividend_yield")) and r["dividend_yield"] >= 0.03:
        t.append("High dividend")
    return t


def build() -> pd.DataFrame:
    u = _universe()
    p = _prices(date.today() - timedelta(days=400))
    f = _financials()
    s = _stages()
    df = u.merge(p, on="symbol", how="left").merge(f, on="symbol", how="left").merge(s, on="symbol", how="left")
    df["mcap_rank"] = df["mcap"].rank(ascending=False, method="first")
    df["size_band"] = _size_band(df["mcap_rank"])
    eq = df["equity_owners"].fillna(df["total_equity"])
    pos = lambda x: x.where(x > 0)
    df["pe"] = df["mcap"] / pos(df["profit"])
    df["pb"] = df["mcap"] / pos(eq)
    df["ps"] = df["mcap"] / pos(df["sales"])
    ev = df["mcap"] + df["borrowings"].fillna(0) - df["cash"].fillna(0)
    df["ev_ebitda"] = ev / pos(df["ebitda"])
    df["dividend_yield"] = df["dividends_paid"].abs() / df["mcap"]
    df["tags"] = df.apply(lambda r: json.dumps(tags(r)), axis=1)
    df["built_at"] = pd.Timestamp.now()
    return df


def write(df: pd.DataFrame) -> int:
    tmp = f"{TABLE}_build"
    text = {"symbol", "series", "isin", "name", "bse_ticker", "macro_sector", "sector", "industry",
            "basic_industry", "basis", "family", "stage_label", "size_band"}
    jsonb = {"tags", "spark"}
    stamp = {"mcap_date", "price_date", "fin_period", "fin_public_at", "q_period", "stage_as_of", "built_at"}
    cols = [c for c in df.columns]
    def typ(c):
        if c in text: return "TEXT"
        if c in jsonb: return "JSONB"
        if c in stamp: return "TIMESTAMP"
        return "DOUBLE PRECISION"
    ddl = ", ".join(f"{c} {typ(c)}" for c in cols)
    out = df[cols].copy()
    for c in stamp & set(cols):
        out[c] = pd.to_datetime(out[c], utc=True, errors="coerce").dt.tz_localize(None)
    buf = io.StringIO()
    out.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with db_session() as (conn, cur):
        cur.execute(f"DROP TABLE IF EXISTS {tmp}")
        cur.execute(f"CREATE TABLE {tmp} ({ddl}, PRIMARY KEY (symbol))")
        cur.copy_expert(f"COPY {tmp} ({', '.join(cols)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(f"ALTER TABLE {tmp} RENAME TO {TABLE}")
        conn.commit()
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    df = build()
    log.info("companies: %d; with financials: %d; with stage: %d", len(df), df["sales"].notna().sum(),
             df["stage"].notna().sum())
    if a.dry_run:
        pd.set_option("display.width", 250)
        print(df[["symbol", "name", "size_band", "industry", "price", "ret_1y", "pe", "roce", "stage", "tags"]]
              .head(25).to_string())
        return
    log.info("written: %d", write(df))


if __name__ == "__main__":
    main()
