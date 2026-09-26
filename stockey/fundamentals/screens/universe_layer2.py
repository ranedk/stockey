"""The rebuilt stock universe, Layer 2 (docs/UNIVERSE_PRD.md section 5, build step 4).

Layer 2 only EXCLUDES fundamentally bad businesses, with checks that depend on the
stock's group (assigned in Layer 1, fundamentals/screens/universe.py). Lenient on
purpose: a stock is excluded only on a positive finding, never for missing data; every
missing input is counted in the report.

Inputs come from screener.in: four market-wide queries (each reveals ~3 columns, in
query order) plus, for lenders only, the company page's quarterly Gross/Net NPA % rows
(NPA is not a query field). Every run's inputs are stored dated in
fundamentals_universe_layer2_inputs, so Layer 2 becomes testable on history as it
accumulates (TODO C4).
"""

from __future__ import annotations

import re

import pandas as pd

from utils.exchange_rate_limiter import exchange_request_gate

_ROW_RE = r'<tr[^>]*>\s*<td class="text">\s*{label}\s*</td>(?P<cells>.*?)</tr>'
_CELL_RE = re.compile(r"<td[^>]*>\s*([^<]*?)\s*</td>", re.S)


def parse_npa(html: str) -> dict[str, float | None]:
    """Latest reported Gross and Net NPA % from a screener.in company page. The first
    matching row is the quarterly table; many quarters are blank, so the rightmost
    filled cell is taken."""
    out: dict[str, float | None] = {}
    for key, label in (("gross_npa_pct", "Gross NPA %"), ("net_npa_pct", "Net NPA %")):
        m = re.search(_ROW_RE.format(label=re.escape(label)), html, re.S)
        value = None
        if m:
            for cell in reversed(_CELL_RE.findall(m.group("cells"))):
                cell = cell.replace("%", "").replace(",", "").strip()
                if cell:
                    try:
                        value = float(cell)
                    except ValueError:
                        value = None
                    break
        out[key] = value
    return out


def fetch_npa(session, ticker: str) -> dict[str, float | None]:
    """Consolidated page first (the group's book), standalone if that has no NPA rows."""
    result: dict[str, float | None] = {"gross_npa_pct": None, "net_npa_pct": None}
    for path in (f"/company/{ticker}/consolidated/", f"/company/{ticker}/"):
        with exchange_request_gate(domain="screenerin"):
            resp = session.get(f"https://www.screener.in{path}", timeout=30)
        if resp.status_code != 200:
            continue
        result = parse_npa(resp.text)
        if result["gross_npa_pct"] is not None or result["net_npa_pct"] is not None:
            break
    return result


# --- Inputs from screener.in queries ---------------------------------------------------
# Each query reveals the columns it filters on, in query order (~3 at most); the filters
# are always-true bounds so every company with a value comes back. A company missing
# from a query simply has no value for those fields (and is never excluded for it).
# Loss-maker-only queries ("Net Profit last year < 0") feed the turnaround and growth
# allow-rules; restricting them keeps each to a few pages. Latest-quarter profit and YoY
# quarterly sales growth are default screener.in columns, so they are read from the first
# of those queries only (a field is taken from one query, never merged from two).
_LOSS = "Net Profit last year < 0 AND Market Capitalization > 250"
QUERIES = {
    "ocf": ("Cash from operations last year > -10000000 AND Cash from operations preceding year > -10000000 AND "
            "Operating cash flow 3years > -10000000 AND Market Capitalization > 250",
            {"cf_operations_rscr": "ocf_y1", "cf_operations_py_rscr": "ocf_y2", "cf_opr_3yrs_rscr": "ocf_3y_total"}),
    "profit": ("Net Profit last year > -10000000 AND Net Profit preceding year > -10000000 AND "
               "Net worth > -10000000 AND Market Capitalization > 250",
               {"np_ann_rscr": "profit_y1", "np_prev_ann_rscr": "profit_y2", "net_worth_rscr": "net_worth"}),
    "debt": ("Debt to equity > -10000 AND Interest Coverage Ratio > -1000000 AND Market Capitalization > 250",
             {"debt___eq": "debt_to_equity", "int_coverage": "interest_cover"}),
    "misc": ("Return on assets > -1000 AND Pledged percentage > -1 AND Market Capitalization > 250",
             {"roa_12m_pct": "return_on_assets_pct", "pledged_pct": "promoter_pledged_pct"}),
    "loss_growth": (f"Sales growth > -1000 AND Price to Sales > -1 AND {_LOSS}",
                    {"sales_growth_pct": "sales_growth_pct", "cmp___sales": "price_to_sales",
                     "np_qtr_rscr": "profit_q", "qtr_sales_var_pct": "sales_growth_q_pct"}),
    "loss_quarters": (f"Net profit > -10000000 AND Net Profit preceding quarter > -10000000 AND "
                      f"Net Profit preceding year quarter > -10000000 AND {_LOSS}",
                      {"np_12m_rscr": "profit_ttm", "np_prev_qtr_rscr": "profit_prev_q", "np_py_qtr_rscr": "profit_q_year_ago"}),
    "loss_margin": (f"OPM latest quarter > -100000 AND OPM preceding year quarter > -100000 AND {_LOSS}",
                    {"opm_qtr_pct": "opm_q", "opm_py_qtr_pct": "opm_q_year_ago"}),
}
FIELDS = {k: v for _, fields in QUERIES.values() for k, v in fields.items()}

# --- Thresholds (docs/UNIVERSE_PRD.md section 5) -----------------------------------------
# Operator decisions 2026-09-25, after the PRD's values removed 377 of 1,395 (27%):
# contingent liabilities at 25% hit every large bank and L&T/BHEL (guarantees are their
# business) -> not for lenders, and 100% of net worth for the rest (removes 27, was 203);
# cash flow "2 of 3 negative" hit Grasim/Kirloskar Oil (lending arms consolidated) and
# order-book growers (Kaynes, Cochin Shipyard) -> also requires a loss last year (25, was
# 139); debt/equity 1.5 hit Bajaj Finserv, AB Capital, Chola Holdings (their lending
# subsidiaries' borrowings) -> developers only (6, was 14).
# Contingent liabilities check DROPPED (operator, 2026-09-25): even at 100% of net worth it
# removed Colgate, Gillette, P&G Hygiene (tax disputes against a net worth kept small by
# full payouts) and Mazagon Dock, RVNL, GRSE (government-contract guarantees).
#
# Two allow-rules lift ONLY the loss / cash-burn exclusions (never pledge, debt, negative
# net worth): systrader research/LEDGER.md row 45, operator 2026-09-25.
#  - Turnaround: profitable over the trailing 12 months and in each of the last 2
#    quarters -- the annual loss is stale (JSW Cement, India Cements, Centum, ...).
#  - Scaling growth: sales up >= 20% both for the year and the latest quarter (YoY); the
#    latest quarter's profit AND operating margin better than a year earlier; net worth
#    > 0 and debt/equity <= 0.5; market cap >= Rs 2,000 cr, >= Rs 5 cr/day traded;
#    price/sales >= 2 (Swiggy, Ather, ideaForge).
GROWTH_SALES_PCT = 20.0
GROWTH_MAX_DEBT_TO_EQUITY = 0.5
GROWTH_MIN_MCAP_RS = 2000e7
GROWTH_MIN_TRADED_VALUE_RS = 5e7
GROWTH_MIN_PRICE_TO_SALES = 2.0
PLEDGE_PCT_CEILING = 50.0             # moved here from Layer 1 rule 6 (operator, 2026-09-25)
LENDER_NET_NPA_CEILING_PCT = 6.0      # RBI prompt-corrective-action risk threshold 1 (banks and NBFCs)
OPERATING_DEBT_TO_EQUITY = 2.0        # together with interest cover below the floor
OPERATING_INTEREST_COVER_FLOOR = 1.5
REALTY_DEBT_TO_EQUITY = 1.5
DEVELOPER_BASIC_INDUSTRIES = frozenset({"Residential, Commercial Projects", "Real Estate related services"})


def fetch_screener_inputs(session=None) -> pd.DataFrame:
    from fundamentals.collectors.screenerin import build_authenticated_session, run_query

    session = session or build_authenticated_session()
    frames = []
    for name, (query, fields) in QUERIES.items():
        _, companies = run_query(session, query)
        frames.append(companies_to_frame(companies, fields, keep_identity=(name == "profit")))
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on=["screener_company_id", "screener_ticker"], how="outer")
    return out


def companies_to_frame(companies: list[dict], fields: dict[str, str], *, keep_identity: bool = False) -> pd.DataFrame:
    """keep_identity: also keep screener.in's name, URL and the full default metrics dict
    (price, market cap, quarterly figures) -- taken from ONE query only, since every
    query returns the same defaults."""
    rows = []
    for c in companies:
        row = {"screener_company_id": c.get("company_id"), "screener_ticker": c.get("ticker")}
        if keep_identity:
            row.update({"screener_name": c.get("name"), "screener_url": c.get("url"),
                        "screener_metrics": c.get("metrics", {})})
        for key, name in fields.items():
            if key in c.get("metrics", {}):
                row[name] = c["metrics"][key]
        rows.append(row)
    return pd.DataFrame(rows).drop_duplicates("screener_company_id")


def attach_inputs(layer1: pd.DataFrame, inputs: pd.DataFrame, bse_code_by_cmid: dict) -> pd.DataFrame:
    """screener.in's ticker is the NSE symbol, or the BSE scrip code for some listings."""
    by_ticker = inputs.dropna(subset=["screener_ticker"]).drop_duplicates("screener_ticker", keep=False)
    by_ticker = by_ticker.set_index("screener_ticker", drop=False)
    df = layer1.copy()
    key = df["symbol"].where(df["symbol"].isin(by_ticker.index),
                             df["company_master_id"].map(bse_code_by_cmid).astype("string"))
    joined = by_ticker.reindex(key.values)
    joined.index = df.index
    return pd.concat([df, joined], axis=1)


def _v(row, col):
    v = row.get(col)
    return None if v is None or pd.isna(v) else float(v)


def is_turnaround(row) -> bool:
    ttm, q, prev_q = _v(row, "profit_ttm"), _v(row, "profit_q"), _v(row, "profit_prev_q")
    return all(v is not None and v > 0 for v in (ttm, q, prev_q))


def is_scaling_growth(row) -> bool:
    g, gq = _v(row, "sales_growth_pct"), _v(row, "sales_growth_q_pct")
    q, q_ago = _v(row, "profit_q"), _v(row, "profit_q_year_ago")
    m, m_ago = _v(row, "opm_q"), _v(row, "opm_q_year_ago")
    nw, de = _v(row, "net_worth"), _v(row, "debt_to_equity")
    mcap, traded, ps = _v(row, "market_cap_rs"), _v(row, "median_value_rs"), _v(row, "price_to_sales")
    if None in (g, gq, q, q_ago, m, m_ago, nw, mcap, traded, ps):
        return False
    return (g >= GROWTH_SALES_PCT and gq >= GROWTH_SALES_PCT and q > q_ago and m > m_ago
            and nw > 0 and (de or 0.0) <= GROWTH_MAX_DEBT_TO_EQUITY
            and mcap >= GROWTH_MIN_MCAP_RS and traded >= GROWTH_MIN_TRADED_VALUE_RS
            and ps >= GROWTH_MIN_PRICE_TO_SALES)


def allowed_by(row) -> str | None:
    """Which allow-rule lifts this stock's loss / cash-burn exclusions, if any."""
    if is_turnaround(row):
        return "turnaround"
    if row.get("group") == "operating" and is_scaling_growth(row):
        return "scaling_growth"
    return None


_LOSS_REASONS = ("loss-making both of the last 2 years",
                 "operating cash flow negative in 2 of the last 3 years and a loss last year")


def layer2_reasons(row) -> list[str]:
    """Every check a stock fails, for its group, after the allow-rules. Missing inputs
    never exclude."""
    out = _raw_reasons(row)
    if any(r in _LOSS_REASONS for r in out) and allowed_by(row):
        out = [r for r in out if r not in _LOSS_REASONS]
    return out


def _raw_reasons(row) -> list[str]:
    group = row.get("group")
    out: list[str] = []
    nw, de, ic = _v(row, "net_worth"), _v(row, "debt_to_equity"), _v(row, "interest_cover")
    p1, p2 = _v(row, "profit_y1"), _v(row, "profit_y2")
    pledge = _v(row, "promoter_pledged_pct")

    if pledge is not None and pledge >= PLEDGE_PCT_CEILING:
        out.append("promoter pledge >= 50%")
    negative_nw = nw is not None and nw < 0

    if group == "lender":
        nnpa, roa = _v(row, "net_npa_pct"), _v(row, "return_on_assets_pct")
        if nnpa is not None and nnpa >= LENDER_NET_NPA_CEILING_PCT:
            out.append("net NPA >= 6%")
        if roa is not None and roa <= 0:
            out.append("return on assets <= 0")
        if negative_nw:
            out.append("negative net worth")
    elif group == "other_financial":
        if p1 is not None and p2 is not None and p1 < 0 and p2 < 0:
            out.append("loss-making both of the last 2 years")
        if negative_nw:
            out.append("negative net worth")
    elif group == "realty_holding":
        if row.get("basic_industry") in DEVELOPER_BASIC_INDUSTRIES and de is not None and de >= REALTY_DEBT_TO_EQUITY:
            out.append("developer debt/equity >= 1.5")
        if negative_nw:
            out.append("negative net worth")
    elif group == "operating":
        y1, y2, total = _v(row, "ocf_y1"), _v(row, "ocf_y2"), _v(row, "ocf_3y_total")
        if y1 is not None and y2 is not None and total is not None:
            years = [y1, y2, total - y1 - y2]  # third year derived from the 3-year total
            if sum(y < 0 for y in years) >= 2 and p1 is not None and p1 < 0:
                out.append("operating cash flow negative in 2 of the last 3 years and a loss last year")
        if p1 is not None and p2 is not None and p1 < 0 and p2 < 0:
            out.append("loss-making both of the last 2 years")
        if de is not None and ic is not None and de >= OPERATING_DEBT_TO_EQUITY and ic < OPERATING_INTEREST_COVER_FLOOR:
            out.append("debt/equity >= 2 with interest cover < 1.5")
    return out


def apply_layer2(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["layer2_reasons"] = out.apply(layer2_reasons, axis=1)
    out["layer2_pass"] = out["layer2_reasons"].map(len).eq(0)
    rescued = out.apply(lambda r: any(x in _LOSS_REASONS for x in _raw_reasons(r)) and allowed_by(r), axis=1)
    out["layer2_allowed_by"] = [allowed_by(r) if flag else None for flag, (_, r) in zip(rescued, out.iterrows())]
    return out


# --- Run ------------------------------------------------------------------------------
INPUTS_TABLE = "fundamentals_universe_layer2_inputs"
_STORED_COLUMNS = ["symbol", "isin", "company_master_id", "group", "screener_company_id", "screener_ticker",
                   *FIELDS.values(), "gross_npa_pct", "net_npa_pct", "layer2_allowed_by"]


def layer2_report(df: pd.DataFrame) -> dict[str, object]:
    reasons: dict[str, int] = {}
    for rs in df["layer2_reasons"]:
        for r in rs:
            reasons[r] = reasons.get(r, 0) + 1
    by_group = {
        str(g): {"layer1": int(len(sub)), "excluded": int((~sub["layer2_pass"]).sum()), "pass": int(sub["layer2_pass"].sum())}
        for g, sub in df.groupby("group")
    }
    return {
        "layer1": int(len(df)),
        "pass_layer2": int(df["layer2_pass"].sum()),
        "by_group": by_group,
        "exclusions_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "no_screener_match": int(df["screener_company_id"].isna().sum()),
        "lenders_without_npa": int((df["group"].eq("lender") & df["net_npa_pct"].isna()).sum()),
        "allowed_by": {str(k): list(v) for k, v in df.dropna(subset=["layer2_allowed_by"])
                       .groupby("layer2_allowed_by")["symbol"]},
    }


def run(*, store: bool = True) -> tuple[pd.DataFrame, dict[str, object]]:
    from fundamentals.collectors.screenerin import build_authenticated_session
    from fundamentals.screens import universe
    from utils.db import sql_to_df, upsert_to_db

    layer1 = universe.apply_layer1_rules(universe.load_layer1_inputs())
    labels, rbi = universe.load_group_inputs()
    layer1 = universe.assign_groups(layer1, labels, rbi)
    layer1 = layer1[layer1["layer1_pass"]].reset_index(drop=True)

    session = build_authenticated_session()
    cm = sql_to_df("SELECT company_master_id, bse_scrip_code FROM company_master WHERE bse_scrip_code IS NOT NULL")
    bse_code = dict(zip(cm["company_master_id"], cm["bse_scrip_code"].astype(str).str.replace(r"\.0$", "", regex=True)))
    df = attach_inputs(layer1, fetch_screener_inputs(session), bse_code)

    df["gross_npa_pct"], df["net_npa_pct"] = None, None
    for i in df.index[df["group"].eq("lender")]:
        ticker = df.at[i, "screener_ticker"] if pd.notna(df.at[i, "screener_ticker"]) else df.at[i, "symbol"]
        npa = fetch_npa(session, str(ticker))
        df.at[i, "gross_npa_pct"], df.at[i, "net_npa_pct"] = npa["gross_npa_pct"], npa["net_npa_pct"]

    df = apply_layer2(df)
    if store:
        snap = df[_STORED_COLUMNS].copy()
        snap["run_date"] = pd.Timestamp.now(tz="Asia/Kolkata").date()
        snap["layer2_reasons"] = df["layer2_reasons"].map("; ".join)
        snap["load_ts"] = pd.Timestamp.now(tz="UTC")
        upsert_to_db(snap, INPUTS_TABLE, unique_keys=["run_date", "isin"])
    return df, layer2_report(df)


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Report Layer 2 of the rebuilt universe (docs/UNIVERSE_PRD.md).")
    parser.add_argument("--csv", help="also write every Layer 1 stock with its Layer 2 result to this path")
    parser.add_argument("--no-store", action="store_true", help="do not write the dated inputs snapshot")
    args = parser.parse_args()
    df, report = run(store=not args.no_store)
    if args.csv:
        out = df.copy()
        out["layer2_reasons"] = out["layer2_reasons"].map("; ".join)
        out["fail_reasons"] = out["fail_reasons"].map("; ".join)
        out.to_csv(args.csv, index=False)
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
