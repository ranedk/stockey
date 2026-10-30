"""Theme strength and the "late" flag, daily (docs/THEMATIC_WATCHLIST_PRD.md section 4.3, build
step 4).

Four parts, each a percentile rank across the themes (1.0 = strongest of the 26):
  policy        live government support minus headwinds, from the policy table: an active
                support item counts 1, +1 if its outlay is Rs 10,000 cr or more, +0.5 if
                announced in the last year; proposed support 0.5; an active headwind -1,
                proposed -0.5; ended items nothing
  capacity      money going into capacity: members' depreciation growth over the last four
                quarters (new plant coming into use), their capital work in progress against
                fixed assets (plant being built), and the yearly growth of the theme's national
                capacity and pipeline series
  fundamentals  members' sales growth in the latest quarter, the same against their own recent
                pace, and the change in operating margin from a year ago
  breadth       price breadth, from systrader's theme views (`GET /api/themes`, build step 5):
                the exposure-weighted share of members in Stage 2 and the theme index's
                26-week return against the market. Empty when the snapshot is missing or
                older than BREADTH_MAX_AGE_DAYS, or when run for a past date (it is not
                point-in-time)
Each part is the mean of the ranks of its measures that exist; strength is the mean of the
parts that exist, with at least two, one of them fundamentals: a theme without enough listed
members reporting results is not ranked, however strong its policy (PRD section 7: policy
alone never qualifies a theme).

Members are the theme's kept tags (prompt v3, not below the theme's minimum share). Member
figures are combined as an exposure-weighted median: weight = revenue share in the theme, or
UNKNOWN_SHARE_WEIGHT when no share is known (the company is in the theme but how much is not).

Late (the capital-cycle brake: themes marked late are not shown, PRD 4.4) when any of:
  members   members' depreciation growth beats their sales growth by more than LATE_GAP_PP,
            in the latest quarter and the one before (capacity running ahead of sales)
  national  the theme's national capacity series grows more than LATE_NATIONAL_GAP_PP faster
            than its demand series, now and a quarter ago
  margins   members' operating margin is more than MARGIN_FALL_PP below its year-ago level in
            the latest two quarters while capacity rises (members' depreciation growth or national capacity growth
            above CAPACITY_RISING_PCT)
Thresholds are a first cut, stored with each row's version; no backtest (PRD section 6).

    python -m fundamentals.themes.strength [--as-of YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date

import numpy as np
import pandas as pd

from fundamentals.screens import results_reading
from fundamentals.themes import catalog
from fundamentals.themes.policy import CAPACITY_TABLE, POLICY_TABLE
from fundamentals.themes.tagger import PROMPT_VERSION
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "fundamentals_theme_strength"
MEMBER_TABLE = "fundamentals_theme_member"     # today's members, synced to systrader in full
# 2 (2026-10-30): breadth from systrader joins the strength mean
STRENGTH_VERSION = 2
SYSTRADER_API = os.getenv("SYSTRADER_API_BASE", "http://127.0.0.1:8090")
BREADTH_MAX_AGE_DAYS = 4        # computed_at; the snapshot itself is the last completed week
UNKNOWN_SHARE_WEIGHT = 0.5
MIN_MEMBERS = 5                 # a member measure needs this many members with the number (PRD 4.1: a theme needs 5 listed companies)
LATE_GAP_PP = 15.0
LATE_NATIONAL_GAP_PP = 20.0
CAPACITY_RISING_PCT = 10.0
MARGIN_FALL_PP = 1.0            # a smaller fall in the median margin is noise
BIG_OUTLAY_CR = 10_000
SERIES_MAX_AGE_DAYS = 400       # a national series point older than this is not "now"

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        as_of_date DATE NOT NULL,
        theme_version INT NOT NULL,
        theme_id TEXT NOT NULL,
        strength_version INT NOT NULL,
        n_members INT, n_with_share INT, n_with_results INT,
        policy_score DOUBLE PRECISION, policy_rank DOUBLE PRECISION,
        members_dep_growth_pct DOUBLE PRECISION, members_cwip_ratio DOUBLE PRECISION,
        national_capacity_growth_pct DOUBLE PRECISION, national_demand_growth_pct DOUBLE PRECISION,
        capacity_rank DOUBLE PRECISION,
        sales_yoy_pct DOUBLE PRECISION, sales_vs_trend_pp DOUBLE PRECISION, margin_change_pp DOUBLE PRECISION,
        fundamentals_rank DOUBLE PRECISION,
        breadth_rank DOUBLE PRECISION,
        breadth_stage2_pct DOUBLE PRECISION, index_rs26_pct DOUBLE PRECISION, breadth_week DATE,
        strength DOUBLE PRECISION, strength_order INT,
        late BOOLEAN NOT NULL, late_reasons TEXT,
        inputs_json TEXT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (as_of_date, theme_version, theme_id, strength_version)
    )"""


_MEMBER_DDL = f"""
    CREATE TABLE IF NOT EXISTS {MEMBER_TABLE} (
        theme_version INT NOT NULL,
        theme_id TEXT NOT NULL,
        company_master_id TEXT NOT NULL,
        stage_key TEXT,                -- systrader's stage key: NSE symbol or "BSE:<scrip>"
        share_pct DOUBLE PRECISION,    -- revenue share in the theme, when known
        weight DOUBLE PRECISION NOT NULL,
        as_of_date DATE NOT NULL,
        PRIMARY KEY (theme_version, theme_id, company_master_id)
    )"""


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)
            for col, typ in (("breadth_stage2_pct", "DOUBLE PRECISION"), ("index_rs26_pct", "DOUBLE PRECISION"),
                             ("breadth_week", "DATE")):
                cur.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {col} {typ}")
            cur.execute(_MEMBER_DDL)

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


# --- members ---------------------------------------------------------------------------------

def load_members(as_of: date, version: int = 1) -> pd.DataFrame:
    """One row per (company, theme) kept tag known by the end of as_of, with its share and weight."""
    cutoff = pd.Timestamp(as_of).tz_localize("Asia/Kolkata") + pd.Timedelta(days=1)
    df = sql_to_df("""
        WITH t AS (
            SELECT DISTINCT ON (company_master_id, theme_id) company_master_id, theme_id, status, exposure_pct
              FROM fundamentals_theme_tag
             WHERE theme_version = %(v)s AND prompt_version = %(p)s AND dropped IS NULL AND tagged_at < %(cut)s
             ORDER BY company_master_id, theme_id, tagged_at DESC),
        a AS (
            SELECT DISTINCT ON (company_master_id, theme_id) company_master_id, theme_id, status, exposure_pct
              FROM fundamentals_theme_exposure_ar
             WHERE theme_version = %(v)s AND dropped IS NULL AND read_at < %(cut)s
             ORDER BY company_master_id, theme_id, read_at DESC),
        p AS (
            SELECT DISTINCT ON (company_master_id) company_master_id, ticker, screener_slug, screener_company_id
              FROM fundamentals_company_profile WHERE status = 'ok'
             ORDER BY company_master_id, fetched_on DESC)
        SELECT t.company_master_id, t.theme_id, t.status AS tag_status, t.exposure_pct AS tag_pct,
               a.status AS ar_status, a.exposure_pct AS ar_pct, p.ticker,
               coalesce(p.screener_slug, p.ticker) AS slug, p.screener_company_id
          FROM t LEFT JOIN a USING (company_master_id, theme_id) LEFT JOIN p USING (company_master_id)
         WHERE t.status IN ('quantified', 'unquantified') AND coalesce(a.status, '') <> 'below_min'""",
                    params={"v": version, "p": PROMPT_VERSION, "cut": cutoff.to_pydatetime()})
    df["share"] = np.where(df["ar_status"].eq("quantified"), df["ar_pct"],
                           np.where(df["tag_status"].eq("quantified"), df["tag_pct"], np.nan)).astype(float)
    df["weight"] = (df["share"] / 100.0).fillna(UNKNOWN_SHARE_WEIGHT)
    df["screener_company_id"] = pd.to_numeric(df["screener_company_id"], errors="coerce").astype("Int64")
    return df


# --- per-member numbers ----------------------------------------------------------------------

def _ttm_yoy(q: pd.DataFrame, field: str, i: int, idx: dict) -> float:
    """Sum of the four quarters ending at row i against the four a year earlier, in %.
    NaN unless all eight quarters are present (by date, so a gap cannot stretch the window)."""
    end = q["period_end"].iloc[i]
    now, then = [], []
    for k in range(4):
        a, b = idx.get(end - pd.offsets.MonthEnd(3 * k)), idx.get(end - pd.offsets.MonthEnd(3 * k + 12))
        if a is None or b is None:
            return np.nan
        now.append(q[field].iloc[a])
        then.append(q[field].iloc[b])
    if any(pd.isna(x) for x in now + then) or sum(then) <= 0:
        return np.nan
    return (sum(now) / sum(then) - 1.0) * 100.0


def member_numbers(q: pd.DataFrame, as_of: date) -> dict | None:
    """One company's quarters -> its fundamentals reading and the capacity/margin figures for
    the latest quarter and the one before. None when results_reading finds no recent quarter."""
    reading = results_reading.read_company(q, as_of)
    if reading is None:
        return None
    q = q.dropna(subset=["period_end"]).assign(period_end=lambda d: pd.to_datetime(d["period_end"]))
    q = q.sort_values("period_end").reset_index(drop=True)
    for f in ("sales", "depreciation", "opm_pct"):
        if f not in q:
            q[f] = np.nan
    idx = {e: i for i, e in enumerate(q["period_end"])}
    out = {"sales_yoy_pct": reading["sales_yoy_pct"], "sales_vs_trend_pp": reading["sales_vs_trend_pp"],
           "margin_change_pp": reading["margin_change_pp"]}
    latest = len(q) - 1
    prev = idx.get(q["period_end"].iloc[latest] - pd.offsets.MonthEnd(3))
    for tag, i in (("", latest), ("_prev", prev)):
        if i is None:
            out.update({f"dep_ttm_yoy{tag}": np.nan, f"sales_ttm_yoy{tag}": np.nan, f"margin_chg{tag}": np.nan})
            continue
        out[f"dep_ttm_yoy{tag}"] = _ttm_yoy(q, "depreciation", i, idx)
        out[f"sales_ttm_yoy{tag}"] = _ttm_yoy(q, "sales", i, idx)
        j = idx.get(q["period_end"].iloc[i] - pd.offsets.MonthEnd(12))
        out[f"margin_chg{tag}"] = (q["opm_pct"].iloc[i] - q["opm_pct"].iloc[j]) if j is not None else np.nan
    return out


def weighted_median(values, weights) -> float:
    v, w = np.asarray(values, float), np.asarray(weights, float)
    ok = ~np.isnan(v) & ~np.isnan(w) & (w > 0)
    if not ok.any():
        return np.nan
    v, w = v[ok], w[ok]
    order = np.argsort(v)
    v, w = v[order], w[order]
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, c[-1] / 2.0)])


def _wmed(df: pd.DataFrame, col: str) -> float:
    """Exposure-weighted median of a member column; NaN with fewer than MIN_MEMBERS values."""
    have = df[df[col].notna()]
    return weighted_median(have[col], have["weight"]) if len(have) >= MIN_MEMBERS else np.nan


# --- national series -------------------------------------------------------------------------

_FY = re.compile(r"^FY(\d{4})-(\d{2})(?: ([HQ])([1-4]))?$")


def period_end(p: str) -> tuple[pd.Timestamp, str] | None:
    """A policy/capacity period string -> (its end date, its granularity)."""
    p = str(p).strip()
    if m := _FY.match(p):
        y = int(m.group(1))
        if not m.group(3):
            return pd.Timestamp(y + 1, 3, 31), "year"
        n = int(m.group(4))
        if m.group(3) == "H":
            return (pd.Timestamp(y, 9, 30) if n == 1 else pd.Timestamp(y + 1, 3, 31)), "half"
        return pd.Timestamp(y, 3, 31) + pd.offsets.QuarterEnd(n), "quarter"
    if re.fullmatch(r"\d{4}", p):
        return pd.Timestamp(int(p), 12, 31), "year"
    if m := re.fullmatch(r"(\d{4})-Q([1-4])", p):
        return pd.Timestamp(int(m.group(1)), 3 * int(m.group(2)), 1) + pd.offsets.MonthEnd(0), "quarter"
    if m := re.fullmatch(r"(\d{4})-H([12])", p):
        return pd.Timestamp(int(m.group(1)), 6 * int(m.group(2)), 1) + pd.offsets.MonthEnd(0), "half"
    if re.fullmatch(r"\d{4}-\d{2}", p):
        return pd.Timestamp(p + "-01") + pd.offsets.MonthEnd(0), "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p):
        return pd.Timestamp(p), "day"
    return None


_SPAN = {"year": pd.DateOffset(years=1), "half": pd.DateOffset(months=6), "quarter": pd.DateOffset(months=3),
         "month": pd.DateOffset(months=1), "day": pd.DateOffset(days=1)}


def period_start(p: str) -> pd.Timestamp | None:
    pe = period_end(p)
    return pe[0] - _SPAN[pe[1]] + pd.Timedelta(days=1) if pe else None


def series_yoy(points: pd.DataFrame, at: pd.Timestamp) -> float:
    """Growth of one series over the year to its latest point on or before `at`, in %: the
    value against the point of the same granularity a year earlier (+-20 days; +-45 for points
    dated to the day, which are irregular). Works for stocks (installed MW), year-to-date
    totals (FYTD to September vs FYTD to September) and flows of one period alike.
    NaN when the latest point is stale or there is no year-ago point."""
    pts = [(e, g, v) for p, v in zip(points["period"], points["value"]) if (pe := period_end(p))
           for e, g in [pe] if e <= at]
    if not pts:
        return np.nan
    e, g, v = max(pts, key=lambda x: x[0])
    if (at - e).days > SERIES_MAX_AGE_DAYS:
        return np.nan
    tol = 45 if g == "day" else 20
    back = [(abs((e - pe - pd.Timedelta(days=365)).days), pv) for pe, pg, pv in pts
            if pg == g and abs((e - pe - pd.Timedelta(days=365)).days) <= tol]
    if not back:
        return np.nan
    base = min(back)[1]
    return (v / base - 1.0) * 100.0 if base > 0 else np.nan


def national_growth(cap: pd.DataFrame, theme_id: str, kinds: tuple[str, ...], at: pd.Timestamp) -> float:
    rows = cap[(cap["theme_id"] == theme_id) & cap["kind"].isin(kinds)]
    g = [series_yoy(s, at) for _, s in rows.groupby("series")]
    g = [x for x in g if pd.notna(x)]
    return float(np.median(g)) if g else np.nan


# --- policy ----------------------------------------------------------------------------------

def policy_score(items: pd.DataFrame, as_of: date) -> float:
    at = pd.Timestamp(as_of)
    score = 0.0
    for it in items.itertuples(index=False):
        start = period_start(it.announced) if it.announced else None
        if start is not None and start > at:
            continue      # not announced yet on as_of
        sign = 1.0 if it.direction == "support" else -1.0
        if it.status == "active":
            pts = 1.0
            if sign > 0 and pd.notna(it.outlay_cr) and it.outlay_cr >= BIG_OUTLAY_CR:
                pts += 1.0
            if sign > 0 and start is not None and (at - start).days <= 365:
                pts += 0.5
        elif it.status == "proposed":
            pts = 0.5
        else:
            pts = 0.0
        score += sign * pts
    return score


# --- the run ---------------------------------------------------------------------------------

def _pct_rank(s: pd.Series) -> pd.Series:
    """Percentile rank across themes, 1.0 = highest; NaN stays NaN."""
    return s.rank(pct=True, method="average")


def _mean_ranks(frame: pd.DataFrame, cols: list[str], min_n: int = 1) -> pd.Series:
    r = pd.concat([_pct_rank(frame[c]) for c in cols], axis=1)
    return r.mean(axis=1).where(r.notna().sum(axis=1) >= min_n)


def stage_keys(members: pd.DataFrame) -> pd.Series:
    """systrader's stage key per member: the NSE symbol, or "BSE:<scrip>" for a BSE-only company
    (company_master holds those as "<scrip>-BOM")."""
    cm = sql_to_df("SELECT company_master_id, nse_ticker, bse_scrip_code FROM company_master"
                   " WHERE company_master_id = ANY(%(ids)s)",
                   params={"ids": list(members["company_master_id"].unique())}).set_index("company_master_id")

    def key(cmid):
        if cmid not in cm.index:
            return None
        nse, bse = cm.at[cmid, "nse_ticker"], cm.at[cmid, "bse_scrip_code"]
        if isinstance(nse, str) and nse and not nse.endswith("-BOM"):
            return nse
        return f"BSE:{bse}" if isinstance(bse, str) and bse else None

    return members["company_master_id"].map(key)


def publish_members(members: pd.DataFrame, as_of: date, version: int) -> int:
    """Replace today's member list (one transaction, so systrader's sync never sees it half-written)."""
    rows = [(version, r.theme_id, r.company_master_id, r.stage_key, None if pd.isna(r.share) else float(r.share),
             float(r.weight), as_of) for r in members.itertuples(index=False)]

    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(f"DELETE FROM {MEMBER_TABLE} WHERE theme_version = %s", (version,))
            cur.executemany(f"INSERT INTO {MEMBER_TABLE} (theme_version, theme_id, company_master_id, stage_key,"
                            f" share_pct, weight, as_of_date) VALUES (%s, %s, %s, %s, %s, %s, %s)", rows)

    execute_db_operation(_op, operation_name=f"{MEMBER_TABLE}:publish")
    return len(rows)


def fetch_breadth() -> dict[str, dict]:
    """theme_id -> {stage2_pct, index_rs26_pct, week} from systrader's latest theme snapshot;
    empty (and recorded) when it cannot be read or is stale."""
    def miss(kind, reason):
        record_local_fallback_event(module="fundamentals.themes.strength", fallback_type=kind, source="systrader",
                                    symbol=None, severity="warn", reason=reason[:500])
        return {}
    try:
        with urllib.request.urlopen(f"{SYSTRADER_API}/api/themes", timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return miss("breadth_unavailable", f"GET /api/themes failed: {exc!r}")
    computed = pd.Timestamp(payload.get("computed_at"))
    if pd.Timestamp.now(tz="UTC") - computed > pd.Timedelta(days=BREADTH_MAX_AGE_DAYS):
        return miss("breadth_stale", f"theme snapshot computed {computed}, older than {BREADTH_MAX_AGE_DAYS} days")
    snap = payload.get("snapshot") or {}
    return {t["theme_id"]: {"stage2_pct": t.get("stage2_pct"), "index_rs26_pct": t.get("index_rs26_pct"),
                            "week": snap.get("as_of")} for t in snap.get("themes", [])}


def compute(as_of: date, version: int = 1, breadth: dict[str, dict] | None = None) -> pd.DataFrame:
    themes = catalog.load(version)
    members = load_members(as_of, version)
    breadth = breadth or {}
    ids = [int(x) for x in members["screener_company_id"].dropna().unique()]
    quarters = results_reading.load_quarters(as_of)
    quarters = quarters[quarters["company_id"].isin(ids)]
    per_co = {int(cid): member_numbers(q, as_of) for cid, q in quarters.groupby("company_id")}
    per_co = {k: v for k, v in per_co.items() if v}
    nums = pd.DataFrame.from_dict(per_co, orient="index")
    nums.index.name = "screener_company_id"
    members = members.merge(nums, left_on="screener_company_id", right_index=True, how="left")
    cwip = sql_to_df("""SELECT DISTINCT ON (company_id) company_id, cwip_ratio FROM fundamentals_l2_state
                        WHERE run_date < %(cut)s AND company_id = ANY(%(ids)s) ORDER BY company_id, run_date DESC""",
                     params={"cut": pd.Timestamp(as_of) + pd.Timedelta(days=1), "ids": ids})
    members = members.merge(cwip.rename(columns={"company_id": "screener_company_id"}), on="screener_company_id", how="left")
    if "sales_yoy_pct" not in members:      # no member had quarters (tests, an empty table)
        for c in ("sales_yoy_pct", "sales_vs_trend_pp", "margin_change_pp", "dep_ttm_yoy", "sales_ttm_yoy",
                  "margin_chg", "dep_ttm_yoy_prev", "sales_ttm_yoy_prev", "margin_chg_prev"):
            members[c] = np.nan
    members["gap"] = members["dep_ttm_yoy"] - members["sales_ttm_yoy"]
    members["gap_prev"] = members["dep_ttm_yoy_prev"] - members["sales_ttm_yoy_prev"]

    policy = sql_to_df(f"SELECT * FROM {POLICY_TABLE} WHERE version = (SELECT max(version) FROM {POLICY_TABLE})")  # noqa: S608
    cap = sql_to_df(f"SELECT * FROM {CAPACITY_TABLE} WHERE version = (SELECT max(version) FROM {CAPACITY_TABLE})")  # noqa: S608
    at, at_prev = pd.Timestamp(as_of), pd.Timestamp(as_of) - pd.Timedelta(days=91)

    rows = []
    for t in themes:
        m = members[members["theme_id"] == t.id]
        nat_cap = national_growth(cap, t.id, ("capacity",), at)
        nat_dem = national_growth(cap, t.id, ("demand",), at)
        nat_gap_prev = national_growth(cap, t.id, ("capacity",), at_prev) - national_growth(cap, t.id, ("demand",), at_prev)
        r = {
            "theme_id": t.id, "n_members": len(m), "n_with_share": int(m["share"].notna().sum()),
            "n_with_results": int(m["sales_yoy_pct"].notna().sum()),
            "policy_score": policy_score(policy[policy["theme_id"] == t.id], as_of),
            "members_dep_growth_pct": _wmed(m, "dep_ttm_yoy"), "members_cwip_ratio": _wmed(m, "cwip_ratio"),
            "national_capacity_growth_pct": national_growth(cap, t.id, ("capacity", "pipeline"), at),
            "national_demand_growth_pct": nat_dem,
            "sales_yoy_pct": _wmed(m, "sales_yoy_pct"), "sales_vs_trend_pp": _wmed(m, "sales_vs_trend_pp"),
            "margin_change_pp": _wmed(m, "margin_change_pp"),
            "breadth_stage2_pct": (breadth.get(t.id) or {}).get("stage2_pct"),
            "index_rs26_pct": (breadth.get(t.id) or {}).get("index_rs26_pct"),
            "breadth_week": (breadth.get(t.id) or {}).get("week"),
        }
        gap, gap_prev = _wmed(m, "gap"), _wmed(m, "gap_prev")
        mchg, mchg_prev = _wmed(m, "margin_chg"), _wmed(m, "margin_chg_prev")
        reasons = []
        if gap > LATE_GAP_PP and gap_prev > LATE_GAP_PP:
            reasons.append(f"members' depreciation grew {gap:.0f} pp faster than sales (and {gap_prev:.0f} pp a quarter before)")
        nat_gap = nat_cap - nat_dem
        if nat_gap > LATE_NATIONAL_GAP_PP and nat_gap_prev > LATE_NATIONAL_GAP_PP:
            reasons.append(f"national capacity grew {nat_gap:.0f} pp faster than demand (and {nat_gap_prev:.0f} pp a quarter before)")
        rising = r["members_dep_growth_pct"] > CAPACITY_RISING_PCT or nat_cap > CAPACITY_RISING_PCT
        if mchg < -MARGIN_FALL_PP and mchg_prev < -MARGIN_FALL_PP and rising:
            reasons.append(f"members' margins below a year ago two quarters running ({mchg:+.1f} pp, {mchg_prev:+.1f} pp) while capacity rises")
        r.update(late=bool(reasons), late_reasons="; ".join(reasons) or None,
                 inputs_json=json.dumps({k: None if pd.isna(v) else round(float(v), 2) for k, v in {
                     "gap_pp": gap, "gap_prev_pp": gap_prev, "national_gap_pp": nat_gap,
                     "national_gap_prev_pp": nat_gap_prev, "margin_chg_pp": mchg, "margin_chg_prev_pp": mchg_prev}.items()}))
        rows.append(r)

    out = pd.DataFrame(rows)
    out["policy_rank"] = _pct_rank(out["policy_score"])
    out["capacity_rank"] = _mean_ranks(out, ["members_dep_growth_pct", "members_cwip_ratio", "national_capacity_growth_pct"])
    out["fundamentals_rank"] = _mean_ranks(out, ["sales_yoy_pct", "sales_vs_trend_pp", "margin_change_pp"])
    for c in ("breadth_stage2_pct", "index_rs26_pct"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["breadth_rank"] = _mean_ranks(out, ["breadth_stage2_pct", "index_rs26_pct"]).where(out["breadth_stage2_pct"].notna())
    parts = out[["policy_rank", "capacity_rank", "fundamentals_rank", "breadth_rank"]]
    out["strength"] = parts.mean(axis=1).where((parts.notna().sum(axis=1) >= 2) & out["fundamentals_rank"].notna())
    out["strength_order"] = out["strength"].rank(ascending=False, method="first").astype("Int64")
    out["as_of_date"], out["theme_version"], out["strength_version"] = as_of, version, STRENGTH_VERSION
    return out.sort_values("strength_order")


def run(as_of: date | None = None, version: int = 1, dry_run: bool = False) -> pd.DataFrame:
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    as_of = as_of or today
    out = compute(as_of, version, breadth=fetch_breadth() if as_of == today else None)
    if not dry_run:
        ensure_table()
        if as_of == today:
            members = load_members(as_of, version)
            publish_members(members.assign(stage_key=stage_keys(members)), as_of, version)
        upsert_to_db(out.assign(load_ts=pd.Timestamp.now(tz="UTC")), TABLE,
                     ["as_of_date", "theme_version", "theme_id", "strength_version"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Theme strength and the late flag.")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    out = run(a.as_of, dry_run=a.dry_run)
    cols = ["strength_order", "theme_id", "strength", "policy_rank", "capacity_rank", "fundamentals_rank",
            "breadth_rank", "n_members", "n_with_results", "late", "late_reasons"]
    print(out[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
