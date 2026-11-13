"""Clean financial series from the XBRL store (PLATFORM_PRD.md tasks F3 and F4).

One wide row per company, basis, period type and period end -> `serving_financials`:

- `q`   quarterly profit and loss, as filed.
- `fy`  financial year: profit and loss, cash flow, and the balance sheet at year end.
- `ttm` trailing twelve months at each quarter end: the last four quarters summed (only when
        all four are present and consecutive); cash flow from the last two halves when the
        quarter end is a half-year end; the balance sheet filed at that date, if any.

Figures are AS FILED (PRD section 0): no restatement. `public_at` is the latest filing
broadcast time among the values a row uses, so "known on date D" is `public_at <= D`.
Limitation: built from `fundamentals_xbrl_latest`, so a revised filing replaces the
original instead of adding a version (TODO.md J, S4).

Ratios are stored on the same row so the screener and the company page read one table:
margins, growth (year on year; three-year CAGR on fy), ROE, ROCE, debt/equity, interest
cover, working-capital days, cash conversion, free cash flow.

    python -m serving.financials            # build and swap in
    python -m serving.financials --symbol KAYNES --dry-run
"""
from __future__ import annotations

import argparse
import io
import logging
from datetime import date

import numpy as np
import pandas as pd

from fundamentals.xbrl import mapping
from utils.db import db_session, sql_to_df

log = logging.getLogger(__name__)

TABLE = "serving_financials"
BUILD_VERSION = 1

PL_ITEMS = sorted(set(mapping.PL_COMPANY) | set(mapping.PL_NBFC) | set(mapping.PL_BANK)
                  | set(mapping.PL_INSURER) | {"revenue_net", "excise_duty"})
BS_ITEMS = sorted(set(mapping.BS_COMPANY) | set(mapping.BS_NBFC) | set(mapping.BS_BANK)
                  | set(mapping.BS_INSURER))
CF_ITEMS = sorted(mapping.CF)
RATIO_ITEMS = set(mapping.RATIO)            # filed as ratios: never summed
FLOW_PL = [i for i in PL_ITEMS if i not in RATIO_ITEMS]

DERIVED = ["sales", "profit", "ebitda", "ebit", "fcf", "opm", "npm", "sales_yoy", "profit_yoy",
           "sales_cagr3", "profit_cagr3", "roe", "roce", "debt_equity", "interest_cover",
           "receivable_days", "inventory_days", "payable_days", "cash_conversion", "tax_rate"]
COLUMNS = PL_ITEMS + BS_ITEMS + CF_ITEMS + DERIVED


def load_raw(symbols: list[str] | None = None) -> pd.DataFrame:
    where, params = "", None
    if symbols:
        where, params = "WHERE symbol = ANY(%s)", (symbols,)
    raw = sql_to_df(f"""SELECT symbol, basis, family, item, kind, period_start, period_end, value, public_at
                          FROM fundamentals_xbrl_latest {where}""", params)
    raw["period_end"] = pd.to_datetime(raw["period_end"])
    raw["period_start"] = pd.to_datetime(raw["period_start"])
    return raw


def _wide(df: pd.DataFrame, period: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    keys = ["symbol", "basis", "period_end"]
    w = df.pivot_table(index=keys, columns="item", values="value", aggfunc="last")
    pub = df.groupby(keys)["public_at"].max()
    fam = df.groupby(keys)["family"].last()
    w = w.join(pub).join(fam).reset_index()
    w["period"] = period
    return w


SCALE_SUSPECT: list[tuple] = []


def _blank_misscaled(wbs: pd.DataFrame) -> pd.DataFrame:
    """A balance sheet filed in the wrong unit (Metro Brands, Sep 2025 consolidated: every line
    100x too small) shows up as total assets about a power of ten away from the company's own
    other balance sheets. Such rows are blanked, not rescaled -- we do not guess the filer's
    intent -- and listed in SCALE_SUSPECT for the build log (XBRL_FUNDAMENTALS.md check 5)."""
    if wbs.empty or "total_assets" not in wbs.columns:
        return wbs
    med = wbs.groupby(["symbol", "basis"])["total_assets"].transform("median")
    ratio = (wbs["total_assets"] / med).abs()
    lg = np.log10(ratio.where(ratio > 0))
    bad = (lg.abs() >= 1.5) & ((lg - lg.round()).abs() < 0.15)
    cols = [c for c in BS_ITEMS if c in wbs.columns]
    SCALE_SUSPECT.extend(wbs.loc[bad, ["symbol", "basis", "period_end"]].itertuples(index=False, name=None))
    wbs.loc[bad, cols] = np.nan
    return wbs


def _consecutive(ends: pd.Series, n: int, months: int) -> pd.Series:
    """True where the n periods ending here are consecutive (spaced `months` apart)."""
    span = (ends - ends.shift(n - 1)).dt.days
    expected = months * (n - 1) * 30.44
    return (span - expected).abs() < 20


def build(raw: pd.DataFrame) -> pd.DataFrame:
    raw = raw[raw["period_end"].notna()]
    q = raw[(raw["kind"] == "q") & raw["item"].isin(PL_ITEMS)]
    fy = raw[(raw["kind"] == "fy") & raw["item"].isin(PL_ITEMS + CF_ITEMS)]
    h1 = raw[(raw["kind"] == "h1") & raw["item"].isin(CF_ITEMS)]
    bs = raw[(raw["kind"] == "i") & raw["item"].isin(BS_ITEMS)]

    wq = _wide(q, "q")
    wfy = _wide(fy, "fy")
    wbs = _blank_misscaled(_wide(bs, "bs"))

    # fy rows take the balance sheet at the same date
    if not wfy.empty and not wbs.empty:
        bs_cols = [c for c in BS_ITEMS if c in wbs.columns]
        wfy = wfy.merge(wbs[["symbol", "basis", "period_end", *bs_cols, "public_at"]]
                        .rename(columns={"public_at": "_bs_pub"}),
                        on=["symbol", "basis", "period_end"], how="left")
        wfy["public_at"] = wfy[["public_at", "_bs_pub"]].max(axis=1)
        wfy = wfy.drop(columns="_bs_pub")

    # trailing twelve months from consecutive quarters
    ttm = pd.DataFrame()
    if not wq.empty:
        wq = wq.sort_values(["symbol", "basis", "period_end"])
        g = wq.groupby(["symbol", "basis"], sort=False)
        flow = [c for c in FLOW_PL if c in wq.columns]
        sums = g[flow].rolling(4, min_periods=4).sum().reset_index(level=[0, 1], drop=True)
        pub_ns = pd.to_datetime(wq["public_at"]).astype("int64").astype(float)
        pub = (pub_ns.groupby([wq["symbol"], wq["basis"]], sort=False).rolling(4, min_periods=4).max()
               .reset_index(level=[0, 1], drop=True))
        pub = pd.to_datetime(pub, unit="ns")
        ok = g["period_end"].transform(lambda s: _consecutive(s, 4, 3))
        ttm = wq[["symbol", "basis", "family", "period_end"]].copy()
        ttm[flow] = sums
        ttm["public_at"] = pub
        ttm = ttm[ok & sums.notna().any(axis=1)]
        # ratio items (bank NPA %, CET1) are point-in-time: take the quarter's own value
        for c in RATIO_ITEMS & set(wq.columns):
            ttm[c] = wq.loc[ttm.index, c]
        ttm["period"] = "ttm"

        # cash flow over the last two halves: fy at a year end; h1 + (previous fy - previous h1)
        if not ttm.empty and not wfy.empty:
            cf_fy = fy[fy["item"].isin(CF_ITEMS)]
            ttm = _attach_cf_ttm(ttm, cf_fy, h1)
        if not wbs.empty:
            bs_cols = [c for c in BS_ITEMS if c in wbs.columns]
            ttm = ttm.merge(wbs[["symbol", "basis", "period_end", *bs_cols, "public_at"]]
                            .rename(columns={"public_at": "_bs_pub"}),
                            on=["symbol", "basis", "period_end"], how="left")
            ttm["public_at"] = ttm[["public_at", "_bs_pub"]].max(axis=1)
            ttm = ttm.drop(columns="_bs_pub")
        # June/December quarter ends have no balance sheet or half-year cash flow: carry the
        # previous quarter's (already public, so still point in time) for one quarter
        if not ttm.empty:
            ttm = ttm.sort_values(["symbol", "basis", "period_end"])
            carry = [c for c in BS_ITEMS + CF_ITEMS if c in ttm.columns]
            ttm[carry] = ttm.groupby(["symbol", "basis"], sort=False)[carry].ffill(limit=1)

    out = pd.concat([x for x in (wq, wfy, ttm) if not x.empty], ignore_index=True)
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = np.nan
    out = _derive(out)
    return out[["symbol", "basis", "family", "period", "period_end", "public_at", *COLUMNS]]


def _attach_cf_ttm(ttm: pd.DataFrame, cf_fy: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    k = ["symbol", "basis", "period_end"]
    fyw = cf_fy.pivot_table(index=k, columns="item", values="value", aggfunc="last")
    fyw["_pub"] = cf_fy.groupby(k)["public_at"].max()
    fyw = fyw.reset_index()
    h1w = h1.pivot_table(index=k, columns="item", values="value", aggfunc="last")
    h1w["_pub"] = h1.groupby(k)["public_at"].max()
    h1w = h1w.reset_index()
    cols = [c for c in CF_ITEMS if c in fyw.columns and c in h1w.columns]

    # at a half-year end: this h1 + previous fy - previous h1 (one year earlier)
    prev_fy = fyw.copy()
    prev_fy["period_end"] = prev_fy["period_end"] + pd.DateOffset(months=6)
    prev_h1 = h1w.copy()
    prev_h1["period_end"] = prev_h1["period_end"] + pd.DateOffset(months=12)
    m = h1w.merge(prev_fy, on=k, suffixes=("", "_fy")).merge(prev_h1, on=k, suffixes=("", "_ph"))
    half = m[k].copy()
    for c in cols:
        half[c] = m[c] + m[f"{c}_fy"] - m[f"{c}_ph"]
    half["_pub"] = m["_pub"]
    full = fyw[k + cols + ["_pub"]]
    cf = pd.concat([full, half], ignore_index=True).drop_duplicates(k, keep="first")
    ttm = ttm.drop(columns=[c for c in cols if c in ttm.columns]).merge(cf, on=k, how="left")
    ttm["public_at"] = ttm[["public_at", "_pub"]].max(axis=1)
    return ttm.drop(columns="_pub")


def _safe_div(a, b):
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    with np.errstate(divide="ignore", invalid="ignore"):
        r = a / b.where(b.abs() > 0)
    return r.replace([np.inf, -np.inf], np.nan)


def _derive(df: pd.DataFrame) -> pd.DataFrame:
    fam = df["family"].fillna("company")
    sales_item = fam.map(mapping.SCREENER_SALES).fillna("revenue_net")
    df["sales"] = np.select([sales_item == i for i in ("revenue_net", "revenue", "total_income")],
                            [df["revenue_net"].fillna(df["revenue"]), df["revenue"], df["total_income"]],
                            default=np.nan)
    # profit attributable to owners when it is plausible; filers often leave it 0 or put a
    # different line (e.g. comprehensive income attributable) under the tag -- 13,488 rows on
    # 2026-11-13 had pat_owners = 0 against a non-zero pat. Minorities above half are rare.
    pat = df["pat"].fillna(df["pat_after_tax"])
    ratio = _safe_div(df["pat_owners"], pat)
    owners_ok = df["pat_owners"].notna() & ((pat.isna()) | ratio.between(0.5, 1.5))
    df["profit"] = np.where(owners_ok, df["pat_owners"], pat)
    is_co = fam.isin(["company", "nbfc"])
    df["ebit"] = np.where(is_co, df["pbt"] + df["finance_costs"].fillna(0) - df["exceptional"].fillna(0), np.nan)
    df["ebitda"] = np.where(is_co, df["ebit"] + df["depreciation"].fillna(0) - df["other_income"].fillna(0), np.nan)
    df["fcf"] = df["cfo"] - df["capex"].fillna(0)
    df["opm"] = _safe_div(df["ebitda"], df["sales"])
    df["npm"] = _safe_div(df["profit"], df["sales"])
    df["tax_rate"] = _safe_div(df["tax"], df["pbt"])
    df["interest_cover"] = _safe_div(df["ebit"], df["finance_costs"])
    equity = df["equity_owners"].fillna(df["total_equity"])
    df["debt_equity"] = _safe_div(df["borrowings"], equity)
    df["cash_conversion"] = _safe_div(df["cfo"], df["profit"])

    df = df.sort_values(["symbol", "basis", "period", "period_end"]).reset_index(drop=True)

    # same period one year earlier (and three years for fy CAGR), matched by date, not position
    def lag(years: int, cols: list[str]) -> pd.DataFrame:
        prev = df[["symbol", "basis", "period", "period_end", *cols]].copy()
        prev["period_end"] = prev["period_end"] + pd.DateOffset(years=years)
        return df[["symbol", "basis", "period", "period_end"]].merge(
            prev, on=["symbol", "basis", "period", "period_end"], how="left")[cols]

    y1 = lag(1, ["sales", "profit", "total_equity", "equity_owners", "borrowings"])
    df["sales_yoy"] = _safe_div(df["sales"] - y1["sales"], y1["sales"].abs())
    df["profit_yoy"] = _safe_div(df["profit"] - y1["profit"], y1["profit"].abs())
    y3 = lag(3, ["sales", "profit"])
    for c in ("sales", "profit"):
        ratio = _safe_div(df[c], y3[c])
        cagr = np.where((ratio > 0) & (y3[c] > 0), np.power(ratio.clip(lower=0), 1 / 3) - 1, np.nan)
        df[f"{c}_cagr3"] = np.where(df["period"] == "fy", cagr, np.nan)

    equity = df["equity_owners"].fillna(df["total_equity"])               # rows were re-sorted above
    eq_prev = y1["equity_owners"].fillna(y1["total_equity"])
    avg_eq = pd.concat([equity, eq_prev], axis=1).mean(axis=1)
    annual = df["period"].isin(["fy", "ttm"])
    df["roe"] = np.where(annual & (avg_eq > 0), _safe_div(df["profit"], avg_eq), np.nan)  # meaningless on negative equity
    cap = equity + df["borrowings"].fillna(0)
    cap_prev = eq_prev + y1["borrowings"].fillna(0)
    avg_cap = pd.concat([cap, cap_prev], axis=1).mean(axis=1)
    is_co = df["family"].fillna("company").isin(["company", "nbfc"])      # rows were re-sorted above
    df["roce"] = np.where(annual & is_co & (avg_cap > 0) & (avg_eq > 0), _safe_div(df["ebit"], avg_cap), np.nan)
    days = 365.0
    df["receivable_days"] = np.where(annual, _safe_div(df["receivables"], df["sales"]) * days, np.nan)
    df["inventory_days"] = np.where(annual, _safe_div(df["inventories"], df["sales"]) * days, np.nan)
    df["payable_days"] = np.where(annual, _safe_div(df["payables"], df["sales"]) * days, np.nan)
    return df


DDL = """CREATE TABLE {t} (
    symbol TEXT NOT NULL, basis TEXT NOT NULL, family TEXT, period TEXT NOT NULL,
    period_end DATE NOT NULL, public_at TIMESTAMP,
    {cols},
    PRIMARY KEY (symbol, basis, period, period_end))"""


def write(df: pd.DataFrame) -> int:
    tmp = f"{TABLE}_build"
    cols = ",\n    ".join(f"{c} DOUBLE PRECISION" for c in COLUMNS)
    out = df.copy()
    out["period_end"] = pd.to_datetime(out["period_end"]).dt.date
    buf = io.StringIO()
    out.to_csv(buf, index=False, header=False, na_rep="\\N")
    buf.seek(0)
    with db_session() as (conn, cur):
        cur.execute(f"DROP TABLE IF EXISTS {tmp}")
        cur.execute(DDL.format(t=tmp, cols=cols))
        cur.copy_expert(f"COPY {tmp} ({', '.join(out.columns)}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", buf)
        cur.execute(f"CREATE INDEX ON {tmp} (period, period_end)")
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute(f"ALTER TABLE {tmp} RENAME TO {TABLE}")
        cur.execute(f"COMMENT ON TABLE {TABLE} IS 'serving.financials v{BUILD_VERSION} built {date.today()}'")
        conn.commit()
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", action="append")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    raw = load_raw(a.symbol)
    log.info("raw values: %d", len(raw))
    df = build(raw)
    log.info("rows: %s", df.groupby("period").size().to_dict())
    log.info("balance sheets blanked as mis-scaled: %d %s", len(SCALE_SUSPECT), SCALE_SUSPECT[:10])
    if a.dry_run:
        pd.set_option("display.width", 250)
        show = ["symbol", "basis", "period", "period_end", "sales", "profit", "opm", "sales_yoy",
                "roe", "roce", "debt_equity", "cfo", "fcf"]
        print(df[show].tail(30).to_string())
        return
    log.info("written: %d", write(df))


if __name__ == "__main__":
    main()
