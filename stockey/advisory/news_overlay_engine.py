from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from typing import Any

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_market_overlay_daily"
DEFAULT_OVERLAY = "NONE"
LOOKBACK_DAYS = 3
NEWS_OVERLAY_SCHEMA_MIGRATION_ID = "20260611_advisory_market_overlay_daily_base"
NEWS_OVERLAY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        base_regime TEXT,
        overlay_name TEXT,
        overlay_intensity DOUBLE PRECISION,
        overlay_reason TEXT,
        source_count BIGINT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date)
    )
    """,
    *[
        f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}"
        for column, sql_type in {
            "base_regime": "TEXT",
            "overlay_name": "TEXT",
            "overlay_intensity": "DOUBLE PRECISION",
            "overlay_reason": "TEXT",
            "source_count": "BIGINT",
            "load_ts": "TIMESTAMPTZ",
        }.items()
    ],
]
KEYWORD_BUCKETS = {
    "GEOPOLITICAL_RISK": ("war", "missile", "attack", "conflict", "border", "sanction", "geopolit"),
    "OIL_SHOCK": ("oil", "crude", "brent", "opec", "fuel", "diesel", "gas prices"),
    "TARIFF_PRESSURE": ("tariff", "duty", "anti-dumping", "trade war", "levy", "import duty"),
    "SECTOR_POLICY_SHOCK": ("policy", "regulation", "subsidy", "budget", "pli", "ban", "govt"),
    "EVENT_CLUSTER": ("results", "earnings", "guidance", "order win", "capex", "stake sale"),
}
_SPACE_RE = re.compile(r"[^a-z0-9]+")


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = _SPACE_RE.sub(" ", text)
    return " ".join(text.split())


def table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return not df.empty


def ensure_output_table() -> None:
    apply_schema_migration(
        migration_id=NEWS_OVERLAY_SCHEMA_MIGRATION_ID,
        statements=NEWS_OVERLAY_SCHEMA_STATEMENTS,
        owner="advisory.news_overlay_engine",
        description="Create daily market news overlay output table.",
        metadata={"tables": [TABLE_NAME], "workflow": "market_news_overlay"},
    )


def resolve_asof_date(requested_date: pd.Timestamp | None = None) -> pd.Timestamp | None:
    if requested_date is not None:
        return requested_date.normalize()
    if not table_exists("advisory_market_regime"):
        return None
    df = sql_to_df("SELECT MAX(asof_date) AS asof_date FROM advisory_market_regime")
    if df.empty:
        return None
    value = pd.to_datetime(df.iloc[0]["asof_date"], utc=True, errors="coerce")
    return None if pd.isna(value) else value.normalize()


def load_regime(asof_date: pd.Timestamp) -> dict[str, Any] | None:
    if not table_exists("advisory_market_regime"):
        return None
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_market_regime
        WHERE asof_date = %s
        LIMIT 1
        """,
        params=(asof_date,),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def load_recent_market_news(asof_date: pd.Timestamp, lookback_days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    published_from = asof_date - pd.Timedelta(days=lookback_days)
    published_to = asof_date + pd.Timedelta(days=1)
    if table_exists("economictimes_rss_items"):
        df = sql_to_df(
            """
            SELECT published_on, feed_name, title, description, categories_json
            FROM economictimes_rss_items
            WHERE published_on >= %s
              AND published_on < %s
            ORDER BY published_on DESC
            """,
            params=(published_from, published_to),
        )
        if not df.empty:
            return df
    if table_exists("advisory_news_events"):
        return sql_to_df(
            """
            SELECT published_on, feed_name, subject AS title, concise_summary_text AS description, categories_json
            FROM advisory_news_events
            WHERE published_on >= %s
              AND published_on < %s
            ORDER BY published_on DESC
            """,
            params=(published_from, published_to),
        )
    return pd.DataFrame()


def classify_overlay(regime_row: dict[str, Any] | None, news_rows: pd.DataFrame) -> tuple[str, float, str, int]:
    base_regime = str((regime_row or {}).get("regime_name") or "")
    counts: Counter[str] = Counter()
    reasons: list[str] = []

    if regime_row:
        if bool(regime_row.get("tariff_pressure_flag")):
            counts["TARIFF_PRESSURE"] += 2
            reasons.append("regime tariff_pressure_flag")
        if bool(regime_row.get("shock_flag")):
            counts["GEOPOLITICAL_RISK"] += 1
            reasons.append("regime shock_flag")
        if bool(regime_row.get("risk_off_flag")):
            counts["GEOPOLITICAL_RISK"] += 1
            reasons.append("regime risk_off_flag")

    source_count = 0
    for _, row in news_rows.iterrows():
        text = normalize_text(f"{row.get('title') or ''} {row.get('description') or ''}")
        if not text:
            continue
        source_count += 1
        for overlay_name, keywords in KEYWORD_BUCKETS.items():
            hits = sum(1 for keyword in keywords if keyword in text)
            if hits > 0:
                counts[overlay_name] += hits
                if len(reasons) < 5:
                    reasons.append(f"{overlay_name.lower()} news hit")

    if not counts:
        return DEFAULT_OVERLAY, 0.0, f"no dominant overlay signals under {base_regime or 'unknown regime'}", source_count

    overlay_name, raw_score = counts.most_common(1)[0]
    if raw_score < 2:
        return DEFAULT_OVERLAY, 0.0, f"signals too weak for overlay under {base_regime or 'unknown regime'}", source_count
    intensity = round(min(1.0, raw_score / 6.0), 4)
    return overlay_name, intensity, "; ".join(dict.fromkeys(reasons))[:500], source_count


def build_overlay_state(*, asof_date: pd.Timestamp | None = None) -> pd.DataFrame:
    resolved = resolve_asof_date(asof_date)
    if resolved is None:
        return pd.DataFrame()
    regime_row = load_regime(resolved)
    news_rows = load_recent_market_news(resolved)
    overlay_name, intensity, reason, source_count = classify_overlay(regime_row, news_rows)
    return pd.DataFrame(
        [
            {
                "asof_date": resolved,
                "base_regime": (regime_row or {}).get("regime_name"),
                "overlay_name": overlay_name,
                "overlay_intensity": intensity,
                "overlay_reason": reason,
                "source_count": source_count,
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )


def persist_overlay_state(df: pd.DataFrame, *, rebuild: bool = False, asof_date: pd.Timestamp | None = None) -> None:
    ensure_output_table()
    if df.empty:
        return
    if rebuild and asof_date is not None:
        def _delete_existing_overlay() -> None:
            with db_session() as (_, cur):
                cur.execute(f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s", (asof_date,))

        execute_db_operation(
            _delete_existing_overlay,
            operation_name="news_overlay_engine:delete_rebuild_overlay",
        )
    upsert_to_db(df, TABLE_NAME, unique_keys=["asof_date"], timescaledb_column="asof_date")


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "sample": df.to_dict(orient="records") if not df.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one daily market news overlay state.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_overlay_state(asof_date=asof_date)
    if not args.dry_run:
        persist_overlay_state(df, rebuild=args.rebuild, asof_date=asof_date or (df["asof_date"].max() if not df.empty else None))
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
