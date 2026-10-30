"""Reads for the /themes page (docs/THEMATIC_WATCHLIST_PRD.md section 4.4, build step 6): the
latest day's theme list and top-10 stocks, and one theme's full membership, each with its
written reasons. Read-only."""
from __future__ import annotations

from fundamentals.api.queries import _clean_records
from utils.db import sql_to_df

_THEME_SQL = """
    WITH w AS (
        SELECT * FROM fundamentals_theme_watchlist_theme
         WHERE as_of_date = (SELECT max(as_of_date) FROM fundamentals_theme_watchlist_theme)),
    s AS (
        SELECT * FROM fundamentals_theme_strength
         WHERE strength_version = (SELECT max(strength_version) FROM fundamentals_theme_strength)
           AND as_of_date = (SELECT max(as_of_date) FROM fundamentals_theme_strength
                              WHERE strength_version = (SELECT max(strength_version) FROM fundamentals_theme_strength))),
    t AS (
        SELECT DISTINCT ON (theme_id) theme_id, reasoning, conclusion, written_at
          FROM fundamentals_theme_text WHERE kind = 'theme' AND status = 'ok'
         ORDER BY theme_id, written_at DESC)
    SELECT w.as_of_date, w.theme_id, w.status, w.position, w.kept_by_buffer, w.n_ranked, w.n_unquantified,
           w.n_excluded, s.strength, s.strength_order, s.late, s.late_reasons, s.policy_rank, s.capacity_rank,
           s.fundamentals_rank, s.breadth_rank, s.policy_score, s.members_dep_growth_pct, s.members_cwip_ratio,
           s.national_capacity_growth_pct, s.national_demand_growth_pct, s.sales_yoy_pct, s.sales_vs_trend_pp,
           s.margin_change_pp, s.breadth_stage2_pct, s.index_rs26_pct, s.breadth_week, s.n_members,
           t.reasoning, t.conclusion, t.written_at
      FROM w LEFT JOIN s USING (theme_id) LEFT JOIN t USING (theme_id)
     {where}
     ORDER BY w.position NULLS LAST, s.strength_order NULLS LAST, w.theme_id"""

_STOCK_SQL = """
    WITH x AS (
        SELECT DISTINCT ON (theme_id, company_master_id) theme_id, company_master_id, reasoning, conclusion, written_at
          FROM fundamentals_theme_text WHERE kind = 'stock' AND status = 'ok'
         ORDER BY theme_id, company_master_id, written_at DESC)
    SELECT w.*, x.reasoning, x.conclusion, x.written_at
      FROM fundamentals_theme_watchlist_stock w LEFT JOIN x USING (theme_id, company_master_id)
     WHERE w.as_of_date = (SELECT max(as_of_date) FROM fundamentals_theme_watchlist_stock) AND {where}
     ORDER BY w.theme_id, w.position NULLS LAST, w.status, w.share_pct DESC NULLS LAST"""


def _names() -> dict:
    from fundamentals.themes import catalog
    return {t.id: {"name": t.name, "area": t.area, "definition": t.definition, "min_exposure": t.min_exposure}
            for t in catalog.load(1)}


def _with_names(rows: list[dict]) -> list[dict]:
    names = _names()
    return [{**r, **names.get(r["theme_id"], {})} for r in rows]


def get_themes() -> dict:
    exists = sql_to_df("SELECT 1 FROM information_schema.tables WHERE table_name = 'fundamentals_theme_watchlist_stock'")
    if exists.empty:
        return {"as_of": None, "themes": [], "stocks": []}
    themes = _with_names(_clean_records(sql_to_df(_THEME_SQL.format(where=""))))
    shown = [t["theme_id"] for t in themes if t["status"] == "shown"]
    stocks = _clean_records(sql_to_df(_STOCK_SQL.format(where="w.on_watchlist AND w.theme_id = ANY(%(ids)s)"),
                                      params={"ids": shown})) if shown else []
    return {"as_of": themes[0]["as_of_date"] if themes else None, "themes": themes, "stocks": stocks}


def get_theme(theme_id: str) -> dict | None:
    rows = _with_names(_clean_records(sql_to_df(_THEME_SQL.format(where="WHERE w.theme_id = %(id)s"),
                                                params={"id": theme_id})))
    if not rows:
        return None
    stocks = _clean_records(sql_to_df(_STOCK_SQL.format(where="w.theme_id = %(id)s"), params={"id": theme_id}))
    policy = _clean_records(sql_to_df(
        "SELECT name, kind, direction, authority, announced, outlay_cr, status, latest_figure, source_url"
        " FROM fundamentals_theme_policy WHERE theme_id = %(id)s"
        " AND version = (SELECT max(version) FROM fundamentals_theme_policy)"
        " ORDER BY direction DESC, status, outlay_cr DESC NULLS LAST", params={"id": theme_id}))
    return {"theme": rows[0], "stocks": stocks, "policy": policy}
