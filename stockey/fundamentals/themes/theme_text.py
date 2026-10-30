"""Short written reasons for the theme watchlist (docs/THEMATIC_WATCHLIST_PRD.md section 4.4, build
step 6): per shown theme, and per company on its top-10 list, a 3-5 sentence reasoning and a
one-line conclusion, in plain words.

Written by the model ONLY from the stored evidence behind the ranks (the evidence JSON is
stored beside the text), and checked in code: every number in the text must be one of the
evidence's numbers (rounded), else the text is rejected and retried on the next run, so the
text cannot contradict the numbers beside it. A text is rewritten only when its evidence
FINGERPRINT changes -- coarse buckets (rating, stage, revenue share to 5 points, the latest
quarter, sales growth to 10 points, ...) -- not nightly.

Calls go through the Claude CLI like the tagger, in the 22:00-08:00 IST night window unless
--any-time.

    python -m fundamentals.themes.theme_text [--any-time] [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from fundamentals.themes import catalog
from fundamentals.themes.tagger import MODEL, call_claude
from fundamentals.themes.watchlist import STOCK_TABLE, THEME_TABLE, WATCHLIST_VERSION
from fundamentals.trigger_study.llm_runner import in_night_window
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

TABLE = "fundamentals_theme_text"
# 2 (2026-10-30): capital work in progress as a percentage; never mention the evidence or a missing figure
TEXT_VERSION = 2
BATCH = 5
STAGE_NAMES = {1: "Stage 1 (basing)", 2: "Stage 2 (rising)", 3: "Stage 3 (topping)", 4: "Stage 4 (falling)"}

_DDL = f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        kind TEXT NOT NULL,                -- theme / stock
        theme_id TEXT NOT NULL,
        company_master_id TEXT NOT NULL,   -- '' for a theme
        fingerprint TEXT NOT NULL,
        text_version INT NOT NULL,
        status TEXT NOT NULL,              -- ok / rejected
        reasoning TEXT, conclusion TEXT, reject_reason TEXT,
        evidence_json TEXT NOT NULL,
        model TEXT, written_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (kind, theme_id, company_master_id, fingerprint, text_version)
    )"""

PROMPT = """You write short notes for an Indian investor's thematic stock watchlist. Each item is either a THEME (an end market such as power grid equipment) or a STOCK within a theme, with an "evidence" object holding every fact you may use.

For each item write:
- "reasoning": 3 to 5 plain sentences. For a stock: what the company does in the theme and its revenue share; what its latest results show (sales growth, against its own recent pace, margins); what it is building (capital work in progress, depreciation growth); what the price trend shows (stage, weeks in it, distance from the breakout level and 52-week high, strength against the market). For a theme: why it ranks where it does -- government support (name the schemes given), capacity being built, members' results, and how many members are rising in price; and why it is or is not marked late (too much capacity for demand).
- "conclusion": one short line, a plain judgement from the evidence (e.g. "Early breakout with growing sales; strongest pure play in the theme.").

Rules: use ONLY the evidence. Every number you write must appear in the evidence (you may round it to a whole number; write percentages as in the evidence). Do not add facts, targets, forecasts, prices you were not given, or advice to buy or sell. If a number is missing (null), leave that measure out entirely -- never write that it is missing or unavailable, and never refer to "the evidence" or the data. Plain words, no jargon; say "rising stage" or "Stage 2 (rising)" rather than bare labels. Keep the item "id" unchanged."""

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "reasoning": {"type": "string"},
                                     "conclusion": {"type": "string"}},
    "required": ["id", "reasoning", "conclusion"]}}}, "required": ["items"]}


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_DDL)

    execute_db_operation(_op, operation_name=f"{TABLE}:ensure_table")


# --- evidence and fingerprints ---------------------------------------------------------------

def _num(v, nd=1):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))) or pd.isna(v):
        return None
    return round(float(v), nd)


def _bucket(v, step):
    return None if v is None else int(round(v / step) * step)


def stock_evidence(r, theme_name: str, source: dict) -> dict:
    return {
        "company": r.company_name, "ticker": r.ticker, "theme": theme_name,
        "revenue_share_pct": _num(r.share_pct, 0), "share_source": source,
        "rating": r.rating, "position_in_theme": int(r.position), "ranked_in_theme": int(r.n_ranked),
        "latest_quarter": None if pd.isna(r.latest_quarter) else str(pd.Timestamp(r.latest_quarter).date()),
        "sales_growth_yoy_pct": _num(r.sales_yoy_pct, 0), "sales_growth_vs_own_pace_pp": _num(r.sales_vs_trend_pp, 0),
        "operating_margin_change_pp": _num(r.margin_change_pp, 1), "roce_pct": _num(r.roce_pct, 0),
        "capital_work_in_progress_pct_of_fixed_assets": _num(None if pd.isna(r.cwip_ratio) else 100 * r.cwip_ratio, 0),
        "depreciation_growth_4q_pct": _num(r.dep_growth_pct, 0),
        "stage": STAGE_NAMES.get(int(r.stage)) if pd.notna(r.stage) else None,
        "weeks_in_stage": None if pd.isna(r.weeks_in_stage) else int(r.weeks_in_stage),
        "early_breakout": bool(r.tier == 4) if pd.notna(r.tier) else None,
        "rs_vs_market_26w_pct": _num(r.rs26_pct, 0), "above_30w_average_pct": _num(r.above_ma30_pct, 0),
        "from_52w_high_pct": _num(r.from_52w_high_pct, 0), "from_breakout_level_pct": _num(r.from_breakout_pct, 0),
        "price_week": None if pd.isna(r.price_week) else str(pd.Timestamp(r.price_week).date()),
        "notes": [x.removeprefix("note: ") for x in (r.reasons or "").split("; ") if x.startswith("note: ")],
    }


def stock_fingerprint(e: dict) -> dict:
    return {"rating": e["rating"], "stage": e["stage"], "early": e["early_breakout"],
            "share": _bucket(e["revenue_share_pct"], 5), "quarter": e["latest_quarter"],
            "sales": _bucket(e["sales_growth_yoy_pct"], 10), "margin": _bucket(e["operating_margin_change_pp"], 2),
            "notes": e["notes"]}


def theme_evidence(t, theme: catalog.Theme, policy: list[str], s: pd.Series) -> dict:
    return {
        "theme": theme.name, "definition": theme.definition, "status": t.status,
        "strength_order": None if pd.isna(t.strength_order) else int(t.strength_order),
        "ranks_out_of_1": {k: _num(s.get(k), 2) for k in ("policy_rank", "capacity_rank", "fundamentals_rank", "breadth_rank")},
        "government_support": policy,
        "members": int(s.get("n_members") or 0), "members_with_results": int(s.get("n_with_results") or 0),
        "members_depreciation_growth_pct": _num(s.get("members_dep_growth_pct"), 0),
        "national_capacity_growth_pct": _num(s.get("national_capacity_growth_pct"), 0),
        "national_demand_growth_pct": _num(s.get("national_demand_growth_pct"), 0),
        "members_sales_growth_yoy_pct": _num(s.get("sales_yoy_pct"), 0),
        "members_margin_change_pp": _num(s.get("margin_change_pp"), 1),
        "members_in_rising_stage_pct": _num(s.get("breadth_stage2_pct"), 0),
        "theme_index_vs_market_26w_pct": _num(s.get("index_rs26_pct"), 0),
        "late": bool(t.late), "late_reasons": t.late_reasons,
        "ranked_companies": int(t.n_ranked),
    }


def theme_fingerprint(e: dict) -> dict:
    return {"status": e["status"], "order": e["strength_order"], "late": e["late"],
            "support": sorted(e["government_support"]),
            "sales": _bucket(e["members_sales_growth_yoy_pct"], 10), "rising": _bucket(e["members_in_rising_stage_pct"], 10)}


def _fp(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --- the number check ------------------------------------------------------------------------

_TEXT_NUM = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


def allowed_numbers(evidence) -> set[float]:
    out: set[float] = set(float(i) for i in range(11))
    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(k)          # a field name carries its window ("rs_vs_market_26w_pct")
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, bool) or v is None:
            return
        elif isinstance(v, (int, float)):
            for nd in (0, 1, 2):
                out.add(abs(round(float(v), nd)))
            out.add(abs(round(float(v) * 100)))      # a 0..1 ratio written as a percentage
        elif isinstance(v, str):
            for m in _TEXT_NUM.findall(v.replace("_", " ")):
                out.add(float(m.replace(",", "")))
    walk(evidence)
    return out


def unsupported_numbers(text: str, evidence) -> list[str]:
    ok = allowed_numbers(evidence)
    bad = []
    for m in _TEXT_NUM.findall(text):
        v = float(m.replace(",", ""))
        if not any(abs(v - a) < 0.051 for a in ok) and not any(abs(v - round(a)) < 0.51 for a in ok if a >= 10):
            bad.append(m)
    return bad


# --- the run ---------------------------------------------------------------------------------

def due(limit: int | None = None) -> list[dict]:
    """Items whose current evidence fingerprint has no accepted text yet."""
    themes = {t.id: t for t in catalog.load(1)}
    th = sql_to_df(f"SELECT * FROM {THEME_TABLE} WHERE watchlist_version = %(w)s"  # noqa: S608
                   f" AND as_of_date = (SELECT max(as_of_date) FROM {THEME_TABLE})", params={"w": WATCHLIST_VERSION})
    if th.empty:
        return []
    as_of = th["as_of_date"].iloc[0]
    strength = sql_to_df("SELECT * FROM fundamentals_theme_strength WHERE as_of_date = (SELECT max(as_of_date)"
                         " FROM fundamentals_theme_strength WHERE as_of_date <= %(d)s) AND strength_version ="
                         " (SELECT max(strength_version) FROM fundamentals_theme_strength)", params={"d": as_of}
                         ).set_index("theme_id")
    policy = sql_to_df("SELECT theme_id, name, outlay_cr FROM fundamentals_theme_policy WHERE direction = 'support'"
                       " AND status = 'active' AND version = (SELECT max(version) FROM fundamentals_theme_policy)")
    st = sql_to_df(f"SELECT * FROM {STOCK_TABLE} WHERE watchlist_version = %(w)s AND as_of_date = %(d)s"  # noqa: S608
                   " AND on_watchlist", params={"w": WATCHLIST_VERSION, "d": as_of})
    src = sql_to_df("""SELECT DISTINCT ON (company_master_id, theme_id) company_master_id, theme_id, segment_name,
                              fiscal_year, report_label FROM fundamentals_theme_exposure_ar
                        WHERE status = 'quantified' ORDER BY company_master_id, theme_id, read_at DESC""")
    src = {(r.company_master_id, r.theme_id): {"segment": r.segment_name, "fiscal_year": r.fiscal_year,
                                               "report": r.report_label} for r in src.itertuples()}
    have = set(map(tuple, sql_to_df(f"SELECT kind, theme_id, company_master_id, fingerprint FROM {TABLE}"  # noqa: S608
                                    " WHERE status = 'ok' AND text_version = %(v)s", params={"v": TEXT_VERSION}
                                    ).itertuples(index=False))) if _table_exists() else set()
    items = []
    shown = th[th["status"].isin(["shown", "next"])].sort_values("position")
    for t in shown.itertuples():
        top = policy[policy["theme_id"] == t.theme_id].sort_values("outlay_cr", ascending=False, na_position="last")
        e = theme_evidence(t, themes[t.theme_id], list(top["name"].head(4)),
                           strength.loc[t.theme_id] if t.theme_id in strength.index else pd.Series(dtype=object))
        fp = _fp(theme_fingerprint(e))
        if ("theme", t.theme_id, "", fp) not in have:
            items.append({"kind": "theme", "theme_id": t.theme_id, "company_master_id": "", "fingerprint": fp, "evidence": e})
    n_ranked = dict(zip(th["theme_id"], th["n_ranked"]))
    on_shown = st[st["theme_id"].isin(shown.loc[shown["status"] == "shown", "theme_id"])]
    for row in on_shown.sort_values(["theme_id", "position"]).to_dict("records"):
        r = SimpleNamespace(**row, n_ranked=n_ranked[row["theme_id"]])
        e = stock_evidence(r, themes[r.theme_id].name,
                           {"from": getattr(r, "share_source", None), **src.get((r.company_master_id, r.theme_id), {})})
        fp = _fp(stock_fingerprint(e))
        if ("stock", r.theme_id, r.company_master_id, fp) not in have:
            items.append({"kind": "stock", "theme_id": r.theme_id, "company_master_id": r.company_master_id,
                          "fingerprint": fp, "evidence": e})
    return items[:limit] if limit else items


def _table_exists() -> bool:
    return not sql_to_df(f"SELECT 1 FROM information_schema.tables WHERE table_name = '{TABLE}'").empty  # noqa: S608


def run(limit: int | None = None, any_time: bool = False) -> dict:
    if not any_time and not in_night_window(datetime.now(timezone.utc)):
        return {"skipped": "outside the 22:00-08:00 IST window (use --any-time)"}
    ensure_table()
    items = due(limit)
    counts = {"due": len(items), "ok": 0, "rejected": 0, "failed_calls": 0}
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        ask = [{"id": str(k), "kind": it["kind"], "evidence": it["evidence"]} for k, it in enumerate(batch)]
        res = call_claude(PROMPT, SCHEMA, ask)
        if not res["ok"]:
            counts["failed_calls"] += 1
            if res["kind"] == "limit":
                counts["stopped"] = res["message"][:200]
                break
            continue
        got = {str(x.get("id")): x for x in res["items"]}
        rows = []
        for k, it in enumerate(batch):
            x = got.get(str(k)) or {}
            text = f"{x.get('reasoning', '')} {x.get('conclusion', '')}"
            bad = unsupported_numbers(text, it["evidence"])
            status = "ok" if x.get("reasoning") and x.get("conclusion") and not bad else "rejected"
            counts[status] += 1
            rows.append({"kind": it["kind"], "theme_id": it["theme_id"], "company_master_id": it["company_master_id"],
                         "fingerprint": it["fingerprint"], "text_version": TEXT_VERSION, "status": status,
                         "reasoning": x.get("reasoning"), "conclusion": x.get("conclusion"),
                         "reject_reason": f"numbers not in the evidence: {bad}" if bad else (None if status == "ok" else "empty"),
                         "evidence_json": json.dumps(it["evidence"], default=str), "model": MODEL,
                         "written_at": pd.Timestamp.now(tz="UTC")})
        upsert_to_db(pd.DataFrame(rows), TABLE, ["kind", "theme_id", "company_master_id", "fingerprint", "text_version"])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the theme watchlist's reasons.")
    ap.add_argument("--any-time", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    print(json.dumps(run(limit=a.limit, any_time=a.any_time), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
