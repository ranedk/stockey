from __future__ import annotations

import argparse
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from advisory.news_overlay_engine import load_recent_market_news, normalize_text, resolve_asof_date
from data.screenerin.screener_parser import upsert_registered_screener
from utils.db import db_session, sql_to_df, upsert_to_db


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_THEME_CONFIG = REPO_ROOT / "config" / "news_theme_screeners.yaml"
DEFAULT_LOOKBACK_LIMIT = 25
THEME_SCREENERS_TABLE = "advisory_news_theme_screeners"


@lru_cache(maxsize=1)
def load_theme_config(config_path: str | None = None) -> list[dict[str, Any]]:
    path = Path(config_path) if config_path else DEFAULT_THEME_CONFIG
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    themes = payload.get("themes") or []
    out: list[dict[str, Any]] = []
    for raw in themes:
        if not isinstance(raw, dict):
            continue
        out.append(
            {
                "theme_id": str(raw.get("theme_id") or "").upper(),
                "theme_name": str(raw.get("theme_name") or raw.get("theme_id") or "").strip(),
                "keywords": [str(value).lower() for value in (raw.get("keywords") or []) if value],
                "positive_sectors": [str(value) for value in (raw.get("positive_sectors") or []) if value],
                "suggested_screeners": [
                    {
                        "slug": str(screener.get("slug") or "").strip(),
                        "screener_name": str(screener.get("screener_name") or screener.get("slug") or "").strip(),
                        "holding_horizon_note": str(screener.get("holding_horizon_note") or "").strip(),
                        "screener_query": str(screener.get("screener_query") or "").strip(),
                    }
                    for screener in (raw.get("suggested_screeners") or [])
                    if isinstance(screener, dict)
                ],
            }
        )
    return out


def classify_themes(news_rows: pd.DataFrame, themes: list[dict[str, Any]], limit: int = DEFAULT_LOOKBACK_LIMIT) -> list[dict[str, Any]]:
    if news_rows.empty:
        return []

    limited_rows = news_rows.head(limit).copy()
    recommendations: list[dict[str, Any]] = []

    for theme in themes:
        keywords = theme.get("keywords") or []
        if not keywords:
            continue
        matched_titles: list[str] = []
        hit_score = 0

        for _, row in limited_rows.iterrows():
            text = normalize_text(f"{row.get('title') or ''} {row.get('description') or ''}")
            if not text:
                continue
            hits = [keyword for keyword in keywords if keyword in text]
            if not hits:
                continue
            hit_score += len(hits)
            if len(matched_titles) < 3:
                matched_titles.append(str(row.get("title") or "").strip())

        if hit_score <= 0:
            continue

        intensity = round(min(1.0, hit_score / 6.0), 4)
        recommendations.append(
            {
                "theme_id": theme["theme_id"],
                "theme_name": theme["theme_name"],
                "theme_reason": "; ".join(title for title in matched_titles if title)[:500],
                "theme_intensity": intensity,
                "positive_sectors": theme.get("positive_sectors") or [],
                "suggested_screeners": theme.get("suggested_screeners") or [],
            }
        )

    recommendations.sort(key=lambda item: (item["theme_intensity"], item["theme_id"]), reverse=True)
    return recommendations


def build_theme_recommendations(*, asof_date: pd.Timestamp | None = None, config_path: str | None = None) -> dict[str, Any]:
    try:
        resolved = resolve_asof_date(asof_date)
    except Exception as exc:
        return {"asof_date": None, "recommendations": [], "news_count": 0, "error": f"failed_to_resolve_asof_date: {exc.__class__.__name__}"}
    if resolved is None:
        return {"asof_date": None, "recommendations": [], "news_count": 0}
    try:
        news_rows = load_recent_market_news(resolved)
    except Exception as exc:
        return {
            "asof_date": resolved,
            "recommendations": [],
            "news_count": 0,
            "error": f"failed_to_load_recent_market_news: {exc.__class__.__name__}",
        }
    themes = load_theme_config(config_path=config_path)
    recommendations = classify_themes(news_rows, themes)
    return {
        "asof_date": resolved,
        "news_count": int(len(news_rows)),
        "recommendations": recommendations,
    }


def ensure_theme_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {THEME_SCREENERS_TABLE} (
                theme_id TEXT NOT NULL,
                screener_slug TEXT NOT NULL,
                screener_name TEXT,
                screener_url TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_ts TIMESTAMPTZ NOT NULL,
                updated_ts TIMESTAMPTZ NOT NULL,
                UNIQUE (theme_id, screener_slug)
            )
            """
        )


def register_theme_screener(*, theme_id: str, screener_url: str, screener_name: str | None = None) -> dict[str, Any]:
    ensure_theme_table()
    normalized_theme_id = str(theme_id or "").strip().upper()
    if not normalized_theme_id:
        raise ValueError("theme_id is required")

    registered = upsert_registered_screener(screener_url=screener_url, screener_name=screener_name)
    now = pd.Timestamp.utcnow()
    df = pd.DataFrame(
        [
            {
                "theme_id": normalized_theme_id,
                "screener_slug": registered["screener_slug"],
                "screener_name": registered["screener_name"],
                "screener_url": registered["screener_url"],
                "is_active": True,
                "created_ts": now,
                "updated_ts": now,
            }
        ]
    )
    upsert_to_db(df, THEME_SCREENERS_TABLE, unique_keys=["theme_id", "screener_slug"])
    return {
        "theme_id": normalized_theme_id,
        "screener_slug": registered["screener_slug"],
        "screener_name": registered["screener_name"],
        "screener_url": registered["screener_url"],
        "status": "registered",
    }


def list_theme_screeners(theme_id: str | None = None) -> pd.DataFrame:
    ensure_theme_table()
    if theme_id:
        return sql_to_df(
            f"""
            SELECT theme_id, screener_slug, screener_name, screener_url, is_active, created_ts, updated_ts
            FROM {THEME_SCREENERS_TABLE}
            WHERE theme_id = %s
            ORDER BY updated_ts DESC, screener_slug
            """,
            params=(str(theme_id).upper(),),
        )
    return sql_to_df(
        f"""
        SELECT theme_id, screener_slug, screener_name, screener_url, is_active, created_ts, updated_ts
        FROM {THEME_SCREENERS_TABLE}
        ORDER BY theme_id, updated_ts DESC, screener_slug
        """
    )


def load_active_theme_screener_mapping(*, asof_date: pd.Timestamp | None = None, config_path: str | None = None) -> dict[str, Any]:
    payload = build_theme_recommendations(asof_date=asof_date, config_path=config_path)
    active_theme_ids = [str(item.get("theme_id") or "").upper() for item in (payload.get("recommendations") or []) if item.get("theme_id")]
    if not active_theme_ids:
        return {"theme_ids": [], "screener_slugs": [], "error": payload.get("error")}
    try:
        df = list_theme_screeners()
    except Exception as exc:
        return {"theme_ids": active_theme_ids, "screener_slugs": [], "error": f"failed_to_list_theme_screeners: {exc.__class__.__name__}"}
    if df.empty:
        return {"theme_ids": active_theme_ids, "screener_slugs": [], "error": payload.get("error")}
    working = df.copy()
    working["theme_id"] = working["theme_id"].astype("string").str.upper()
    working = working[working["theme_id"].isin(active_theme_ids)]
    if "is_active" in working.columns:
        working = working[working["is_active"].fillna(False)]
    slugs = sorted(working.get("screener_slug", pd.Series(dtype="string")).dropna().astype(str).unique().tolist())
    return {"theme_ids": active_theme_ids, "screener_slugs": slugs, "error": payload.get("error")}


def format_text(payload: dict[str, Any]) -> str:
    lines = [
        f"Asof date: {payload.get('asof_date')}",
        f"Recent news rows scanned: {payload.get('news_count')}",
    ]
    if payload.get("error"):
        lines.append(f"Error: {payload.get('error')}")
    recommendations = payload.get("recommendations") or []
    if not recommendations:
        lines.append("")
        lines.append("No active news themes detected.")
        return "\n".join(lines)

    for item in recommendations:
        lines.extend(
            [
                "",
                f"Theme: {item.get('theme_id')} | intensity={item.get('theme_intensity')}",
                f"Reason: {item.get('theme_reason') or 'No concise reason available'}",
                f"Positive sectors: {', '.join(item.get('positive_sectors') or [])}",
            ]
        )
        for screener in item.get("suggested_screeners") or []:
            lines.extend(
                [
                    f"Screener slug: {screener.get('slug')}",
                    f"Screener name: {screener.get('screener_name')}",
                    f"Holding note: {screener.get('holding_horizon_note')}",
                    "Screener.in query:",
                    screener.get("screener_query") or "",
                    "Next step: create this screener in Screener.in, then register the URL with:",
                    f"  python -m advisory.news_theme_engine register-url --theme-id {item.get('theme_id')} --url <SCREENER_URL> --name \"{screener.get('screener_name')}\"",
                ]
            )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Suggest and register event/news-driven Screener.in screeners.")
    subparsers = parser.add_subparsers(dest="command", required=False)

    recommend = subparsers.add_parser("recommend", help="Suggest news-theme screeners")
    recommend.add_argument("--date", help="Asof date in YYYY-MM-DD")
    recommend.add_argument("--format", choices=["text", "json"], default="text")
    recommend.add_argument("--config", dest="config_path")

    register = subparsers.add_parser("register-url", help="Register a Screener.in URL against a theme")
    register.add_argument("--theme-id", required=True)
    register.add_argument("--url", required=True)
    register.add_argument("--name")

    list_parser = subparsers.add_parser("list-registered", help="List registered theme screener mappings")
    list_parser.add_argument("--theme-id")

    parser.set_defaults(command="recommend", format="text", config_path=None, date=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "register-url":
        result = register_theme_screener(theme_id=args.theme_id, screener_url=args.url, screener_name=args.name)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.command == "list-registered":
        df = list_theme_screeners(theme_id=args.theme_id)
        print(json.dumps(df.to_dict(orient="records"), indent=2, ensure_ascii=False, default=str))
        return 0

    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    payload = build_theme_recommendations(asof_date=asof_date, config_path=args.config_path)
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
