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
DEFAULT_THEME_CONFIG = REPO_ROOT / "config" / "investment_themes.yaml"
DEFAULT_LOOKBACK_LIMIT = 25
THEME_SCREENERS_TABLE = "advisory_news_theme_screeners"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set):
        return list(value)
    return [value]


def _string_list(value: Any, *, upper: bool = False, lower: bool = False) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        text = str(item or "").strip()
        if not text:
            continue
        if upper:
            text = text.upper()
        elif lower:
            text = text.lower()
        out.append(text)
    return out


def _normalize_screener_templates(raw_templates: Any) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for raw in _as_list(raw_templates):
        if not isinstance(raw, dict):
            continue
        slug = str(raw.get("slug") or raw.get("template_id") or "").strip()
        if not slug:
            continue
        templates.append(
            {
                "template_id": str(raw.get("template_id") or slug).strip(),
                "provider": str(raw.get("provider") or "screenerin").strip(),
                "slug": slug,
                "screener_name": str(raw.get("label") or raw.get("screener_name") or slug).strip(),
                "holding_horizon_note": str(raw.get("holding_horizon_note") or "").strip(),
                "screener_query": str(raw.get("query") or raw.get("screener_query") or "").strip(),
                "entry_style": str(raw.get("entry_style") or "").strip(),
                "horizon": str(raw.get("horizon") or "").strip(),
            }
        )
    return templates


def _normalize_theme_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    theme_id = str(raw.get("theme_id") or "").strip().upper()
    if not theme_id:
        return None

    classification = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
    detection = raw.get("detection") if isinstance(raw.get("detection"), dict) else {}
    detection_keywords = detection.get("keywords") if isinstance(detection.get("keywords"), dict) else {}
    detection_match = detection.get("match") if isinstance(detection.get("match"), dict) else {}
    portfolio_guidance = raw.get("portfolio_guidance") if isinstance(raw.get("portfolio_guidance"), dict) else {}
    decay = raw.get("decay") if isinstance(raw.get("decay"), dict) else {}

    suggested_screeners = _normalize_screener_templates(raw.get("screener_templates") or raw.get("suggested_screeners"))

    positive_sectors = _string_list(
        raw.get("positive_sectors")
        or portfolio_guidance.get("positive_sectors")
        or classification.get("sector_bias")
    )
    negative_sectors = _string_list(
        raw.get("negative_sectors")
        or portfolio_guidance.get("negative_sectors")
    )

    return {
        "theme_id": theme_id,
        "theme_name": str(raw.get("name") or raw.get("theme_name") or theme_id).strip(),
        "status": str(raw.get("status") or "active").strip().lower(),
        "description": str(raw.get("description") or "").strip(),
        "keywords": _string_list(
            raw.get("keywords")
            or detection_keywords.get("include"),
            lower=True,
        ),
        "negative_keywords": _string_list(detection_keywords.get("exclude"), lower=True),
        "title_weight": float(detection_match.get("title_weight") or 2.0),
        "description_weight": float(detection_match.get("description_weight") or 1.0),
        "min_hit_score": float(detection_match.get("min_hit_score") or 1.0),
        "max_titles_for_reason": int(detection_match.get("max_titles_for_reason") or 3),
        "classification": classification,
        "market_cap_fit": _string_list(classification.get("market_cap_fit")),
        "holding_profile": str(classification.get("holding_profile") or ("event_driven" if suggested_screeners else "")).strip(),
        "holding_period_days": classification.get("holding_period_days") or {},
        "risk_level": str(classification.get("risk_level") or ("medium_high" if suggested_screeners else "")).strip(),
        "exit_trigger_types": _string_list(classification.get("exit_trigger_types")),
        "positive_sectors": positive_sectors,
        "negative_sectors": negative_sectors,
        "suggested_screeners": suggested_screeners,
        "ideal_screener_logic": str(portfolio_guidance.get("ideal_screener_logic") or "").strip(),
        "invalidation_signals": _string_list(portfolio_guidance.get("invalidation_signals")),
        "priority": int((raw.get("routing") or {}).get("priority") or 0) if isinstance(raw.get("routing"), dict) else 0,
        "decay": decay,
    }


@lru_cache(maxsize=1)
def load_theme_config(config_path: str | None = None) -> list[dict[str, Any]]:
    paths = [Path(config_path)] if config_path else [DEFAULT_THEME_CONFIG]
    merged: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for raw in payload.get("themes") or []:
            if not isinstance(raw, dict):
                continue
            normalized = _normalize_theme_entry(raw)
            if normalized is None:
                continue
            merged[normalized["theme_id"]] = normalized
    return sorted(merged.values(), key=lambda item: (item.get("priority") or 0, item["theme_id"]), reverse=True)


def classify_themes(news_rows: pd.DataFrame, themes: list[dict[str, Any]], limit: int = DEFAULT_LOOKBACK_LIMIT) -> list[dict[str, Any]]:
    if news_rows.empty:
        return []

    limited_rows = news_rows.head(limit).copy()
    recommendations: list[dict[str, Any]] = []

    for theme in themes:
        if str(theme.get("status") or "active").lower() != "active":
            continue
        keywords = theme.get("keywords") or []
        if not keywords:
            continue
        matched_titles: list[str] = []
        hit_score = 0.0
        matched_sources: list[dict[str, Any]] = []
        title_weight = float(theme.get("title_weight") or 2.0)
        description_weight = float(theme.get("description_weight") or 1.0)
        max_titles_for_reason = int(theme.get("max_titles_for_reason") or 3)
        negative_keywords = theme.get("negative_keywords") or []

        for _, row in limited_rows.iterrows():
            title_text = normalize_text(str(row.get("title") or ""))
            description_text = normalize_text(str(row.get("description") or ""))
            combined_text = f"{title_text} {description_text}".strip()
            if not combined_text:
                continue
            if any(keyword in combined_text for keyword in negative_keywords):
                continue
            title_hits = [keyword for keyword in keywords if keyword in title_text]
            description_hits = [keyword for keyword in keywords if keyword in description_text]
            row_score = (len(title_hits) * title_weight) + (len(description_hits) * description_weight)
            if row_score <= 0:
                continue
            hit_score += row_score
            title = str(row.get("title") or "").strip()
            if title and len(matched_titles) < max_titles_for_reason and title not in matched_titles:
                matched_titles.append(title)
            if len(matched_sources) < max_titles_for_reason:
                matched_sources.append(
                    {
                        "title": title,
                        "description": str(row.get("description") or "").strip(),
                        "published_on": row.get("published_on"),
                        "hit_score": round(row_score, 4),
                    }
                )

        if hit_score < float(theme.get("min_hit_score") or 1.0):
            continue

        intensity = round(min(1.0, hit_score / max(6.0, float(theme.get("min_hit_score") or 1.0) * 3.0)), 4)
        recommendations.append(
            {
                "theme_id": theme["theme_id"],
                "theme_name": theme["theme_name"],
                "theme_description": theme.get("description") or "",
                "theme_reason": "; ".join(title for title in matched_titles if title)[:500],
                "theme_intensity": intensity,
                "hit_score": round(hit_score, 4),
                "positive_sectors": theme.get("positive_sectors") or [],
                "negative_sectors": theme.get("negative_sectors") or [],
                "market_cap_fit": theme.get("market_cap_fit") or [],
                "holding_profile": theme.get("holding_profile") or "",
                "holding_period_days": theme.get("holding_period_days") or {},
                "risk_level": theme.get("risk_level") or "",
                "exit_trigger_types": theme.get("exit_trigger_types") or [],
                "ideal_screener_logic": theme.get("ideal_screener_logic") or "",
                "decay": theme.get("decay") or {},
                "invalidation_signals": theme.get("invalidation_signals") or [],
                "matched_sources": matched_sources,
                "suggested_screeners": theme.get("suggested_screeners") or [],
            }
        )

    recommendations.sort(key=lambda item: (item["theme_intensity"], item.get("hit_score") or 0.0, item["theme_id"]), reverse=True)
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
        return {
            "theme_ids": [],
            "screener_slugs": [],
            "themes": payload.get("recommendations") or [],
            "error": payload.get("error"),
        }
    try:
        df = list_theme_screeners()
    except Exception as exc:
        return {
            "theme_ids": active_theme_ids,
            "screener_slugs": [],
            "themes": payload.get("recommendations") or [],
            "error": f"failed_to_list_theme_screeners: {exc.__class__.__name__}",
        }
    if df.empty:
        return {
            "theme_ids": active_theme_ids,
            "screener_slugs": [],
            "themes": payload.get("recommendations") or [],
            "error": payload.get("error"),
        }
    working = df.copy()
    working["theme_id"] = working["theme_id"].astype("string").str.upper()
    working = working[working["theme_id"].isin(active_theme_ids)]
    if "is_active" in working.columns:
        working = working[working["is_active"].fillna(False)]
    slugs = sorted(working.get("screener_slug", pd.Series(dtype="string")).dropna().astype(str).unique().tolist())
    return {
        "theme_ids": active_theme_ids,
        "screener_slugs": slugs,
        "themes": payload.get("recommendations") or [],
        "error": payload.get("error"),
    }


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
                f"Market-cap fit: {', '.join(item.get('market_cap_fit') or []) or '-'}",
                f"Holding profile: {item.get('holding_profile') or '-'} | risk={item.get('risk_level') or '-'}",
                f"Ideal screener logic: {item.get('ideal_screener_logic') or '-'}",
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
