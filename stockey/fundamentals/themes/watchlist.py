"""The ranked theme watchlist, daily (docs/THEMATIC_WATCHLIST_PRD.md section 4.4, build step 6).

Themes. The top SHOWN themes by strength that are not late; the next NEXT_UP are "next up".
A buffer keeps the list from flipping: a theme shown yesterday stays while it is in the top
KEEP_WITHIN and not late, and the free places go to the strongest others.

Companies, per theme (every theme is ranked, so a theme rising into the list is ready):
  ranked        a revenue share in the theme at or above its minimum, liquid (combined NSE+BSE
                60-day median traded value >= Rs 1 cr), passing the management gate and the
                business floor, with a price stage from systrader
  unquantified  in the theme but with no revenue share: shown greyed, never ranked
  excluded      failed the gate, the floor or liquidity, or has no price history; the reason
                is stored and shown
Company rank = mean of four percentile ranks within the theme's ranked members (equal
weight, PRD decision): exposure (revenue share); trend and breakout (systrader's score: early
Stage 2 above extended Stage 2 above Stage 1 above 3 and 4, then relative strength);
business (sales growth, the same against the company's own pace, margin change); capacity
(capital work in progress against fixed assets, depreciation growth). A missing measure
drops out of the mean; exposure and trend are always present. Rating A-E = the fifth of the
theme's ranked members the company falls in. The top TOP_N are the watchlist.

Management gate (hard, 5 years): a SEBI enforcement order AGAINST the company -- the order
names it in its parties ("In respect of ..."), read from the order itself, since a title
"in the matter of Titan Company" is as often an insider-trading order against ten employees
(takeover-exemption orders are not wrongdoing; an order in its matter against others, or about
trading in its shares, is a note; an order whose parties cannot be read counts against it); a statutory auditor resignation;
a rating cut to default (D); promoter pledge above 10%; a story flaw from the story score.
Not covered: promoter and director names (none held), fraud or forensic-audit disclosures
(no feed carries them). Business floor: ROCE >= ROCE_FLOOR and positive operating cash flow
over three years (screener.in snapshot). Operator, 2026-10-30: a loss-making company failing the
floor (or carrying the story flaws "cash burn with losses" / "loss-making both of the last 2
years") is kept when its quarterly losses are shrinking (`losses_shrinking`); the waiver is
noted on the row. It never waives the SEBI, auditor, default or pledge checks.

    python -m fundamentals.themes.watchlist [--as-of YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import date

import numpy as np
import pandas as pd

from fundamentals.collectors import sebi_orders
from fundamentals.collectors.company_profile import load_universe
from fundamentals.screens import results_reading
from fundamentals.themes import catalog, sebi_match
from fundamentals.themes.strength import (STRENGTH_VERSION, TABLE as STRENGTH_TABLE, fetch_theme_snapshot,
                                          load_members, member_numbers, stage_keys)
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

THEME_TABLE = "fundamentals_theme_watchlist_theme"
STOCK_TABLE = "fundamentals_theme_watchlist_stock"
WATCHLIST_VERSION = 1
SHOWN, NEXT_UP, KEEP_WITHIN = 5, 3, 8
TOP_N = 10
GATE_YEARS = 5
PLEDGE_MAX_PCT = 10.0
ROCE_FLOOR = 8.0
RATINGS = "ABCDE"

_DDL = [f"""
    CREATE TABLE IF NOT EXISTS {THEME_TABLE} (
        as_of_date DATE NOT NULL, theme_version INT NOT NULL, theme_id TEXT NOT NULL,
        watchlist_version INT NOT NULL,
        status TEXT NOT NULL,              -- shown / next / out / late / unranked
        position INT,                      -- order among shown + next
        strength DOUBLE PRECISION, strength_order INT, late BOOLEAN, late_reasons TEXT,
        kept_by_buffer BOOLEAN NOT NULL DEFAULT false,
        n_ranked INT, n_unquantified INT, n_excluded INT,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (as_of_date, theme_version, theme_id, watchlist_version))""",
    f"""
    CREATE TABLE IF NOT EXISTS {STOCK_TABLE} (
        as_of_date DATE NOT NULL, theme_version INT NOT NULL, theme_id TEXT NOT NULL,
        company_master_id TEXT NOT NULL, watchlist_version INT NOT NULL,
        ticker TEXT, company_name TEXT, stage_key TEXT,
        status TEXT NOT NULL,              -- ranked / unquantified / excluded
        reasons TEXT,                      -- why excluded; notes for the others
        share_pct DOUBLE PRECISION, share_source TEXT, median_tv_cr DOUBLE PRECISION,
        exposure_rank DOUBLE PRECISION, trend_rank DOUBLE PRECISION, business_rank DOUBLE PRECISION,
        capacity_rank DOUBLE PRECISION, combined DOUBLE PRECISION, position INT, rating TEXT,
        on_watchlist BOOLEAN NOT NULL DEFAULT false,
        stage INT, weeks_in_stage INT, tier INT, trend_score DOUBLE PRECISION, rs26_pct DOUBLE PRECISION,
        above_ma30_pct DOUBLE PRECISION, from_52w_high_pct DOUBLE PRECISION, breakout_level DOUBLE PRECISION,
        from_breakout_pct DOUBLE PRECISION, price_week DATE,
        sales_yoy_pct DOUBLE PRECISION, sales_vs_trend_pp DOUBLE PRECISION, margin_change_pp DOUBLE PRECISION,
        latest_quarter DATE, roce_pct DOUBLE PRECISION, ocf_3y_cr DOUBLE PRECISION,
        cwip_ratio DOUBLE PRECISION, dep_growth_pct DOUBLE PRECISION, pledge_pct DOUBLE PRECISION,
        load_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (as_of_date, theme_version, theme_id, company_master_id, watchlist_version))""",
]


def ensure_tables() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            for ddl in _DDL:
                cur.execute(ddl)
            cur.execute(f"ALTER TABLE {STOCK_TABLE} ADD COLUMN IF NOT EXISTS share_source TEXT")

    execute_db_operation(_op, operation_name=f"{STOCK_TABLE}:ensure_tables")


# --- themes ----------------------------------------------------------------------------------

def choose_themes(strength: pd.DataFrame, previously_shown: set[str]) -> pd.DataFrame:
    """strength: theme_id, strength, strength_order, late. Adds status, position, kept_by_buffer."""
    s = strength.copy()
    s["status"], s["position"], s["kept_by_buffer"] = "out", pd.NA, False
    s.loc[s["strength"].isna(), "status"] = "unranked"
    s.loc[s["strength"].notna() & s["late"].astype(bool), "status"] = "late"
    ok = s[(s["status"] == "out")].sort_values("strength_order")
    kept = [t for t in ok["theme_id"] if t in previously_shown
            and ok.loc[ok["theme_id"] == t, "strength_order"].iloc[0] <= KEEP_WITHIN]
    shown = list(kept)
    for t in ok["theme_id"]:
        if len(shown) >= SHOWN:
            break
        if t not in shown:
            shown.append(t)
    shown = sorted(shown, key=lambda t: int(ok.loc[ok["theme_id"] == t, "strength_order"].iloc[0]))
    nxt = [t for t in ok["theme_id"] if t not in shown][:NEXT_UP]
    for pos, t in enumerate(shown + nxt, 1):
        m = s["theme_id"] == t
        s.loc[m, "status"] = "shown" if t in shown else "next"
        s.loc[m, "position"] = pos
        s.loc[m, "kept_by_buffer"] = t in kept and t in shown and \
            int(ok.loc[ok["theme_id"] == t, "strength_order"].iloc[0]) > SHOWN
    return s


# --- the gate --------------------------------------------------------------------------------

def gate_reasons(companies: pd.DataFrame, as_of: date) -> dict[str, list[str]]:
    """company_master_id -> exclusion reasons (empty list = passes). companies needs
    company_master_id, company_name, pledge_pct, flaws."""
    since = pd.Timestamp(as_of) - pd.DateOffset(years=GATE_YEARS)
    out: dict[str, list[str]] = {c: [] for c in companies["company_master_id"]}
    notes: dict[str, list[str]] = {c: [] for c in companies["company_master_id"]}
    ids = list(out)
    orders = sql_to_df("SELECT url, order_date, kind, matter FROM fundamentals_sebi_order"
                       " WHERE order_date BETWEEN %(lo)s AND %(hi)s", params={"lo": since.date(), "hi": as_of})
    matched = sebi_match.match_orders(orders, companies[["company_master_id", "company_name"]])
    matched = matched[matched["kind"] != "exemption"]
    named = matched[matched["role"] != "scrip"]
    sebi_orders.fetch_parties(list(named["url"]))
    parties = sql_to_df(f"SELECT url, status, parties FROM {sebi_orders.PARTY_TABLE} WHERE url = ANY(%(u)s)",  # noqa: S608
                        params={"u": list(named["url"].unique())}).set_index("url")
    for m in matched.itertuples():
        when = pd.Timestamp(m.order_date).date()
        if m.role == "scrip":
            notes[m.company_master_id].append(f"SEBI {m.kind} order on trading in its shares ({when})")
            continue
        p = parties.loc[m.url] if m.url in parties.index else None
        if p is None or p["status"] != "ok":
            out[m.company_master_id].append(f"named in a SEBI {m.kind} order ({when}) whose parties could not be read")
        elif sebi_match.named_in(p["parties"], sebi_match.tokens(m.company_name)):
            out[m.company_master_id].append(f"SEBI {m.kind} order against the company ({when})")
        else:
            notes[m.company_master_id].append(f"SEBI {m.kind} order in its matter, against others ({when})")
    ev = sql_to_df("""
        SELECT company_master_id, filing_type, headline, rationale_headline, disclosure_date
          FROM fundamentals_events
         WHERE company_master_id = ANY(%(ids)s) AND left(disclosure_date, 10) BETWEEN %(lo)s AND %(hi)s
           AND (filing_type = 'auditor_change' OR rating_action_type = 'downgraded')""",
                   params={"ids": ids, "lo": str(since.date()), "hi": str(as_of)})
    for e in ev.itertuples():
        when = pd.Timestamp(e.disclosure_date).date()
        text = f"{e.headline or ''} {e.rationale_headline or ''}"
        if e.filing_type == "auditor_change":
            if auditor_resigned(text):
                out[e.company_master_id].append(f"statutory auditor resigned ({when})")
        elif cut_to_default(text):
            out[e.company_master_id].append(f"rating cut to default ({when})")
        else:
            notes[e.company_master_id].append(f"rating downgraded ({when})")
    for c in companies.itertuples():
        if pd.notna(c.pledge_pct) and c.pledge_pct > PLEDGE_MAX_PCT:
            out[c.company_master_id].append(f"promoter pledge {c.pledge_pct:.0f}%")
        if isinstance(c.flaws, str) and c.flaws.strip() and c.flaws.strip() != "[]":
            out[c.company_master_id].extend(f"story flaw: {f.strip()}" for f in c.flaws.split(";") if f.strip())
    return {c: [*dict.fromkeys(out[c])] + [f"note: {n}" for n in dict.fromkeys(notes[c])] for c in ids}


_SUBSIDIARY_AUDITOR = re.compile(r"auditors?\s+of\s+(the\s+)?((?!company\b)\w+\s+){0,3}subsidiar", re.I)
_CAG_ROTATION = re.compile(r"c\s*&\s*ag|comptroller", re.I)


def auditor_resigned(text: str) -> bool:
    """A resignation of the company's OWN statutory auditor -- not one of a subsidiary's
    ("Joint Statutory Auditor of the Unlisted Material Subsidiaries"), and not a C&AG
    reassignment of a government company's auditor (routine rotation)."""
    return bool(re.search(r"resign", text, re.I)) and not _SUBSIDIARY_AUDITOR.search(text) \
        and not _CAG_ROTATION.search(text)


_DEFAULT_RX = re.compile(r"\bdefault|(?:crisil|\[icra\]|icra|ind|care|bwr|acuite|ivr)\s+d\b(?!\s*[+-])|['\u2018\"]\s*d\s*['\u2019\"]", re.I)


def cut_to_default(text: str) -> bool:
    """A rating cut to D (default): "CRISIL D", "[ICRA]D", "IND D", a quoted 'D', or the word."""
    return bool(_DEFAULT_RX.search(text))


LOSS_FLAWS = ("story flaw: cash burn with losses", "story flaw: loss-making both of the last 2 years")


def losses_shrinking(q: pd.DataFrame) -> str | None:
    """Operator, 2026-10-30: a loss-making company is kept when its quarterly losses are getting
    smaller. True when it made a loss in one of the last four quarters, its net result improved
    in each of the last two quarters, and the latest quarter beats the same quarter a year ago.
    Returns the figures as a note, or None."""
    q = q.dropna(subset=["period_end", "net_profit"]).assign(period_end=lambda d: pd.to_datetime(d["period_end"]))
    q = q.sort_values("period_end")
    by = dict(zip(q["period_end"], q["net_profit"]))
    if not by:
        return None
    end = q["period_end"].iloc[-1]
    seq = [by.get(end - pd.offsets.MonthEnd(3 * k)) for k in range(5)]   # latest first
    if any(v is None for v in seq[:3]):
        return None
    if not min(v for v in seq[:4] if v is not None) < 0:
        return None
    if not (seq[0] > seq[1] > seq[2]):
        return None
    if seq[4] is not None and not seq[0] > seq[4]:
        return None
    return ("results improving after a loss: net result Rs " + ", ".join(f"{v:.0f}" for v in reversed(seq[:3]))
            + f" cr over the last three quarters (to {end.date()})")


def excluded(reasons: list[str]) -> list[str]:
    return [r for r in reasons if not r.startswith("note: ")]


# --- ranking ---------------------------------------------------------------------------------

def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def rank_theme(df: pd.DataFrame) -> pd.DataFrame:
    """One theme's ranked members -> the four ranks, combined rank, position and rating."""
    d = df.copy()
    d["exposure_rank"] = _pct(d["share_pct"])
    d["trend_rank"] = _pct(d["trend_score"])
    b = pd.concat([_pct(d[c]) for c in ("sales_yoy_pct", "sales_vs_trend_pp", "margin_change_pp")], axis=1)
    d["business_rank"] = b.mean(axis=1)
    c = pd.concat([_pct(d[c]) for c in ("cwip_ratio", "dep_growth_pct")], axis=1)
    d["capacity_rank"] = c.mean(axis=1)
    d["combined"] = d[["exposure_rank", "trend_rank", "business_rank", "capacity_rank"]].mean(axis=1)
    d = d.sort_values(["combined", "trend_score"], ascending=False)
    n = len(d)
    d["position"] = range(1, n + 1)
    d["rating"] = [RATINGS[min(4, math.ceil(5 * p / n) - 1)] for p in d["position"]]
    d["on_watchlist"] = d["position"] <= TOP_N
    return d


# --- the run ---------------------------------------------------------------------------------

def _latest(table: str, cols: str, ids: list, key: str, date_col: str, as_of: date) -> pd.DataFrame:
    return sql_to_df(f"SELECT DISTINCT ON ({key}) {key}, {cols} FROM {table}"  # noqa: S608 -- constants
                     f" WHERE {key} = ANY(%(ids)s) AND {date_col} <= %(d)s ORDER BY {key}, {date_col} DESC",
                     params={"ids": ids, "d": as_of})


def compute(as_of: date, version: int = 1, snapshot: dict | None = None,
            previously_shown: set[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    themes = {t.id: t for t in catalog.load(version)}
    m = load_members(as_of, version)
    m["stage_key"] = stage_keys(m)
    cmids = list(m["company_master_id"].unique())
    names = sql_to_df("SELECT company_master_id, company_name FROM company_master WHERE company_master_id = ANY(%(ids)s)",
                      params={"ids": cmids})
    uni = load_universe(as_of)[["company_master_id", "median_tv", "merged_ids"]]
    tv = {}
    for r in uni.itertuples():
        for cid in [r.company_master_id, *(r.merged_ids if isinstance(r.merged_ids, list) else [])]:
            tv[cid] = r.median_tv
    snap = _latest("fundamentals_snapshot_daily", "roce_pct, ocf_3y_total, promoter_pledged_pct",
                   cmids, "company_master_id", "as_of_date", as_of)
    story = _latest("fundamentals_story_score", "flaws", cmids, "company_master_id", "as_of_date", as_of)
    ids = [int(x) for x in m["screener_company_id"].dropna().unique()]
    cwip = _latest("fundamentals_l2_state", "cwip_ratio", ids, "company_id", "run_date", as_of)
    quarters = results_reading.load_quarters(as_of)
    quarters = quarters[quarters["company_id"].isin(ids)]
    nums = {int(cid): member_numbers(q, as_of) for cid, q in quarters.groupby("company_id")}
    latest_q = quarters.groupby("company_id")["period_end"].max()
    shrink = {int(cid): losses_shrinking(q) for cid, q in quarters.groupby("company_id")}

    co = (names.merge(snap, on="company_master_id", how="left").merge(story, on="company_master_id", how="left")
          .rename(columns={"promoter_pledged_pct": "pledge_pct", "ocf_3y_total": "ocf_3y_cr"}))
    gate = gate_reasons(co.assign(flaws=co.get("flaws")), as_of)
    co = co.set_index("company_master_id")

    stocks = {s["key"]: s for s in (snapshot or {}).get("stocks", [])}
    week = (snapshot or {}).get("as_of")
    rows = []
    for r in m.itertuples(index=False):
        t = themes[r.theme_id]
        c = co.loc[r.company_master_id] if r.company_master_id in co.index else None
        n = nums.get(int(r.screener_company_id)) if pd.notna(r.screener_company_id) else None
        n = n or {}
        st = stocks.get(r.stage_key) or {}
        cw = cwip.loc[cwip["company_id"] == r.screener_company_id, "cwip_ratio"] if pd.notna(r.screener_company_id) else []
        row = {
            "theme_id": r.theme_id, "company_master_id": r.company_master_id, "ticker": r.ticker,
            "company_name": None if c is None else c["company_name"], "stage_key": r.stage_key,
            "share_pct": r.share, "share_source": r.share_source, "median_tv_cr": tv.get(r.company_master_id, np.nan) / 1e7,
            "stage": st.get("stage"), "weeks_in_stage": st.get("weeks_in_stage"), "tier": st.get("tier"),
            "trend_score": st.get("trend_score"), "rs26_pct": st.get("rs26_pct"),
            "above_ma30_pct": st.get("above_ma30_pct"), "from_52w_high_pct": st.get("from_52w_high_pct"),
            "breakout_level": st.get("breakout_level"), "from_breakout_pct": st.get("from_breakout_pct"),
            "price_week": week if st else None,
            "sales_yoy_pct": n.get("sales_yoy_pct"), "sales_vs_trend_pp": n.get("sales_vs_trend_pp"),
            "margin_change_pp": n.get("margin_change_pp"), "dep_growth_pct": n.get("dep_ttm_yoy"),
            "latest_quarter": latest_q.get(int(r.screener_company_id)) if n else None,
            "roce_pct": None if c is None else c["roce_pct"], "ocf_3y_cr": None if c is None else c["ocf_3y_cr"],
            "pledge_pct": None if c is None else c["pledge_pct"],
            "cwip_ratio": float(cw.iloc[0]) if len(cw) else np.nan,
        }
        why = list(gate.get(r.company_master_id, []))
        hard = excluded(why)
        if not (row["median_tv_cr"] >= 1.0):
            hard.append("traded value under Rs 1 cr a day")
        if row["roce_pct"] is not None and pd.notna(row["roce_pct"]) and row["roce_pct"] < ROCE_FLOOR:
            hard.append(f"business floor: ROCE {row['roce_pct']:.0f}%")
        if row["ocf_3y_cr"] is not None and pd.notna(row["ocf_3y_cr"]) and row["ocf_3y_cr"] <= 0:
            hard.append(f"business floor: 3-year operating cash flow Rs {row['ocf_3y_cr']:.0f} cr")
        waivable = [h for h in hard if h.startswith("business floor") or h in LOSS_FLAWS]
        improving = shrink.get(int(r.screener_company_id)) if pd.notna(r.screener_company_id) else None
        if waivable and improving:
            hard = [h for h in hard if h not in waivable]
            why.append(f"note: business floor waived -- {improving} ({'; '.join(waivable)})")
        if row["trend_score"] is None:
            hard.append("no price stage yet (short history or no series)")
        notes = [x for x in why if x.startswith("note: ")]
        if hard:
            row["status"] = "excluded"
        elif pd.isna(r.share):
            row["status"] = "unquantified"
        elif r.share < t.min_exposure:
            row["status"], hard = "excluded", [f"revenue share {r.share:.0f}% under the theme's {t.min_exposure:.0f}%"]
        else:
            row["status"] = "ranked"
        row["reasons"] = "; ".join(hard + notes) or None
        rows.append(row)
    st_df = pd.DataFrame(rows)
    for col in ("trend_score", "rs26_pct", "stage", "weeks_in_stage", "tier", "above_ma30_pct", "from_52w_high_pct",
                "breakout_level", "from_breakout_pct", "sales_yoy_pct", "sales_vs_trend_pp", "margin_change_pp",
                "dep_growth_pct", "roce_pct", "ocf_3y_cr", "pledge_pct", "cwip_ratio", "share_pct"):
        st_df[col] = pd.to_numeric(st_df[col], errors="coerce")
    ranked = [rank_theme(g) for _, g in st_df[st_df["status"] == "ranked"].groupby("theme_id")]
    st_df = pd.concat([st_df[st_df["status"] != "ranked"], *ranked], ignore_index=True)
    st_df["on_watchlist"] = st_df["on_watchlist"].astype("boolean").fillna(False).astype(bool) \
        if "on_watchlist" in st_df else False

    strength = sql_to_df(f"SELECT theme_id, strength, strength_order, late, late_reasons FROM {STRENGTH_TABLE}"  # noqa: S608
                         " WHERE strength_version = %(v)s AND theme_version = %(tv)s AND as_of_date ="
                         f" (SELECT max(as_of_date) FROM {STRENGTH_TABLE} WHERE strength_version = %(v)s AND as_of_date <= %(d)s)",
                         params={"v": STRENGTH_VERSION, "tv": version, "d": as_of})
    th = choose_themes(strength, previously_shown or set())
    counts = st_df.groupby(["theme_id", "status"]).size().unstack(fill_value=0)
    for col, status in (("n_ranked", "ranked"), ("n_unquantified", "unquantified"), ("n_excluded", "excluded")):
        th[col] = th["theme_id"].map(counts[status] if status in counts else {}).fillna(0).astype(int)
    for d in (th, st_df):
        d["as_of_date"], d["theme_version"], d["watchlist_version"] = as_of, version, WATCHLIST_VERSION
    return th, st_df


def previously_shown(as_of: date, version: int) -> set[str]:
    try:
        df = sql_to_df(f"SELECT theme_id FROM {THEME_TABLE} WHERE status = 'shown' AND theme_version = %(v)s"  # noqa: S608
                       f" AND as_of_date = (SELECT max(as_of_date) FROM {THEME_TABLE} WHERE as_of_date < %(d)s)",
                       params={"v": version, "d": as_of})
    except Exception:  # first run: no table yet
        return set()
    return set(df["theme_id"])


def run(as_of: date | None = None, version: int = 1, dry_run: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    as_of = as_of or today
    snapshot = fetch_theme_snapshot("fundamentals.themes.watchlist") if as_of == today else None
    th, st = compute(as_of, version, snapshot, previously_shown(as_of, version))
    if not dry_run:
        ensure_tables()
        now = pd.Timestamp.now(tz="UTC")
        upsert_to_db(th.assign(load_ts=now), THEME_TABLE, ["as_of_date", "theme_version", "theme_id", "watchlist_version"])
        upsert_to_db(st.assign(load_ts=now), STOCK_TABLE,
                     ["as_of_date", "theme_version", "theme_id", "company_master_id", "watchlist_version"])
    return th, st


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The ranked theme watchlist.")
    ap.add_argument("--as-of", type=date.fromisoformat, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    th, st = run(a.as_of, dry_run=a.dry_run)
    shown = th[th["status"].isin(["shown", "next"])].sort_values("position")
    out = {"themes": shown[["position", "theme_id", "status", "strength_order", "n_ranked", "n_unquantified",
                             "n_excluded"]].to_dict("records"),
           "stocks": int(len(st)), "on_watchlist": int(st["on_watchlist"].sum()),
           "by_status": st["status"].value_counts().to_dict()}
    print(json.dumps(out, default=str, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
