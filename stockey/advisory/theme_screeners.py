"""Theme screeners: each active news theme materializes its sector universe as a screener.

The news-theme engine already derives sector-level context overlays from matched market
news, but those only create watchlist "pressure" -- theme names never entered the candidate
funnel as first-class constituents. This module finishes the lane: for every ACTIVE theme
with a POSITIVE-direction overlay, resolve symbols through the same sector join the theme
context watchlist uses (overlay sector name/code + alias map vs the top-context market
universe) and emit constituents rows (`screener_slug=theme-<theme_id>`), capped per theme.

Rules:
- Positive overlays only. A negative theme must never ADMIT names -- negative pressure
  stays in the existing watchlist/de-risk lane.
- Theme decay is enforced here for the first time: a theme whose latest overlay is older
  than its YAML `decay.max_active_days` stops emitting (the config field was previously
  parsed but dead).
- Expectation honesty: news themes are consensus by construction -- this is the
  lowest-conviction admission lane. It is bounded (per-theme cap) and the regret ledger
  grades it per slug like every other source.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df

THEME_SCREENERS_ENABLED = os.getenv("THEME_SCREENERS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
MAX_SYMBOLS_PER_THEME = int(os.getenv("THEME_SCREENER_MAX_SYMBOLS", "25"))
DEFAULT_MAX_ACTIVE_DAYS = int(os.getenv("THEME_SCREENER_DEFAULT_MAX_ACTIVE_DAYS", "21"))
SLUG_PREFIX = "theme-"

THEME_CONTEXT_OVERLAYS_TABLE = "advisory_news_theme_context_overlays"
MARKET_CONTEXT_UNIVERSE_TABLE = "advisory_market_context_universe_daily"


def _theme_max_active_days() -> dict[str, int]:
    """Per-theme decay.max_active_days from the YAML config; {} on any failure."""
    try:
        from advisory.news_theme_engine import load_theme_config

        limits: dict[str, int] = {}
        for theme in load_theme_config():
            theme_id = str(theme.get("theme_id") or "").strip()
            decay = theme.get("decay") if isinstance(theme.get("decay"), dict) else {}
            value = pd.to_numeric((decay or {}).get("max_active_days"), errors="coerce")
            if theme_id:
                limits[theme_id] = int(value) if pd.notna(value) and int(value) > 0 else DEFAULT_MAX_ACTIVE_DAYS
        return limits
    except Exception:
        return {}


def _load_theme_symbols(asof_date: pd.Timestamp) -> pd.DataFrame:
    """Positive-overlay themes joined to the top-context universe by sector (same join shape
    as the theme context watchlist), deduped per (theme, symbol), capped per theme."""
    from advisory.news_theme_engine import theme_sector_alias_values_sql

    return sql_to_df(
        f"""
        WITH theme_sector_alias(overlay_sector_key, universe_sector_code) AS (
            VALUES
            {theme_sector_alias_values_sql()}
        ),
        latest_overlays AS (
            SELECT *
            FROM {THEME_CONTEXT_OVERLAYS_TABLE}
            WHERE asof_date = (
                SELECT MAX(asof_date)
                FROM {THEME_CONTEXT_OVERLAYS_TABLE}
                WHERE asof_date <= %(asof_date)s
            )
              AND production_status = 'active'
              AND direction = 'positive'
              AND NULLIF(TRIM(COALESCE(sector_name, sector_code, '')), '') IS NOT NULL
        ),
        latest_universe AS (
            SELECT *
            FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
            WHERE asof_date = (
                SELECT MAX(asof_date)
                FROM {MARKET_CONTEXT_UNIVERSE_TABLE}
                WHERE asof_date <= %(asof_date)s
            )
              AND in_top_context = TRUE
              AND NULLIF(TRIM(symbol), '') IS NOT NULL
              AND NULLIF(TRIM(company_master_id), '') IS NOT NULL
        ),
        joined AS (
            SELECT
                o.theme_id,
                o.theme_name,
                o.asof_date AS overlay_asof_date,
                o.pressure_score,
                o.hit_score,
                o.sector_name AS overlay_sector_name,
                UPPER(TRIM(u.symbol)) AS symbol,
                u.company_master_id,
                u.context_rank,
                u.sector_name AS universe_sector_name,
                ROW_NUMBER() OVER (
                    PARTITION BY o.theme_id, UPPER(TRIM(u.symbol))
                    ORDER BY u.context_rank ASC NULLS LAST
                ) AS symbol_rn
            FROM latest_universe u
            JOIN latest_overlays o
              ON TRUE
            LEFT JOIN theme_sector_alias tsa
              ON tsa.overlay_sector_key = regexp_replace(upper(coalesce(o.sector_name, o.sector_code, '')), '[^A-Z0-9]', '', 'g')
            WHERE (
                regexp_replace(upper(coalesce(u.sector_name, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_name, '')), '[^A-Z0-9]', '', 'g')
                OR regexp_replace(upper(coalesce(u.sector_code, '')), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(coalesce(o.sector_code, '')), '[^A-Z0-9]', '', 'g')
                OR upper(coalesce(u.sector_code, '')) = tsa.universe_sector_code
            )
        ),
        deduped AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY theme_id
                       ORDER BY context_rank ASC NULLS LAST, symbol
                   ) AS theme_rank
            FROM joined
            WHERE symbol_rn = 1
        )
        SELECT theme_id, theme_name, overlay_asof_date, pressure_score, hit_score,
               overlay_sector_name, symbol, company_master_id, context_rank,
               universe_sector_name, theme_rank
        FROM deduped
        WHERE theme_rank <= %(cap)s
        ORDER BY theme_id, theme_rank
        """,
        params={"asof_date": asof_date, "cap": max(1, MAX_SYMBOLS_PER_THEME)},
    )


def build_theme_constituents(*, asof_date: Any | None = None) -> pd.DataFrame:
    from advisory.market_action_scan import _latest_bhavcopy_date

    scan_date = _latest_bhavcopy_date(asof_date)
    if scan_date is None:
        return pd.DataFrame()
    effective_asof = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    joined = _load_theme_symbols(effective_asof)
    if joined.empty:
        return pd.DataFrame()
    max_active = _theme_max_active_days()
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    skipped_decayed: set[str] = set()
    for row in joined.itertuples(index=False):
        theme_id = str(row.theme_id or "").strip()
        if not theme_id:
            continue
        overlay_age_days = None
        overlay_asof = pd.to_datetime(row.overlay_asof_date, utc=True, errors="coerce")
        if not pd.isna(overlay_asof):
            overlay_age_days = int((effective_asof - overlay_asof.normalize()).days)
        limit_days = max_active.get(theme_id, DEFAULT_MAX_ACTIVE_DAYS)
        if overlay_age_days is not None and overlay_age_days > limit_days:
            skipped_decayed.add(theme_id)
            continue
        slug = f"{SLUG_PREFIX}{theme_id.lower().replace('_', '-')}"
        rows.append(
            {
                "date": scan_date,
                "screener_slug": slug,
                "screener_name": f"Theme: {row.theme_name}"[:120],
                "screener_url": None,
                "ticker": str(row.symbol).strip().upper(),
                "exchange": "NSE",
                "company_master_id": row.company_master_id,
                "security_id": None,
                "instrument": None,
                "isin": None,
                "display_name": str(row.symbol).strip().upper(),
                "rank": int(row.theme_rank),
                "raw_item_json": json.dumps(
                    {
                        "source": "theme_screener",
                        "theme_id": theme_id,
                        "theme_name": row.theme_name,
                        "pressure_score": None if pd.isna(row.pressure_score) else float(row.pressure_score),
                        "hit_score": None if pd.isna(row.hit_score) else float(row.hit_score),
                        "overlay_sector_name": row.overlay_sector_name,
                        "universe_sector_name": row.universe_sector_name,
                        "overlay_asof_date": None if pd.isna(overlay_asof) else overlay_asof.date().isoformat(),
                    },
                    ensure_ascii=False,
                ),
                "load_ts": now,
            }
        )
    if skipped_decayed:
        print(f"[theme_screeners] decayed_themes_skipped={sorted(skipped_decayed)}", flush=True)
    return pd.DataFrame(rows)


def safe_build_theme_constituents(*, asof_date: Any | None = None) -> pd.DataFrame:
    """Constituents-path wrapper: any failure degrades to an empty frame with telemetry."""
    try:
        return build_theme_constituents(asof_date=asof_date)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.theme_screeners",
            source=THEME_CONTEXT_OVERLAYS_TABLE,
            fallback_type="theme_screeners_failed",
            severity="warn",
            reason="Theme screener materialization failed; constituents continue without theme sources.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return pd.DataFrame()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize active news themes as screener constituents.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)
    frame = build_theme_constituents(asof_date=args.date)
    if not args.dry_run and not frame.empty:
        from advisory.screener_parser import persist_constituents

        persist_constituents(frame)
    if args.format == "json":
        print(json.dumps({"rows": len(frame), "dry_run": bool(args.dry_run)}, default=str))
    else:
        print(f"[theme_screeners] rows={len(frame)} dry_run={args.dry_run}")
        if not frame.empty:
            for slug, group in frame.groupby("screener_slug"):
                print(f"  {slug}: {len(group)} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
