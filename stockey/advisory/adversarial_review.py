from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


REVIEWS_TABLE = "advisory_event_reviews"
SCORES_TABLE = "advisory_event_model_scores"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


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
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {REVIEWS_TABLE} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                reviewed_at TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                event_source TEXT,
                review_status TEXT,
                review_action TEXT,
                review_score DOUBLE PRECISION,
                veto BOOLEAN,
                review_reason TEXT,
                review_flags_json TEXT,
                feature_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id)
            )
            """
        )


def load_event_evaluations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_reviewed: bool = False,
) -> pd.DataFrame:
    if not table_exists("advisory_event_evaluations"):
        return pd.DataFrame()

    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("e.asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("e.asof_date = (SELECT MAX(asof_date) FROM advisory_event_evaluations)")
    if symbols:
        clauses.append("e.symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("e.setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])

    review_join = ""
    review_select = ""
    if table_exists(REVIEWS_TABLE):
        review_join = f"""
        LEFT JOIN {REVIEWS_TABLE} r
          ON r.published_on = e.published_on
         AND r.setup_id = e.setup_id
         AND r.symbol = e.symbol
         AND r.unique_id = e.unique_id
        """
        review_select = ", r.unique_id AS reviewed_unique_id"
        if not include_reviewed:
            clauses.append("r.unique_id IS NULL")

    score_join = ""
    score_select = ""
    if table_exists(SCORES_TABLE):
        score_join = f"""
        LEFT JOIN LATERAL (
            SELECT
                m.event_meta_score,
                m.model_name AS event_meta_model_name,
                m.model_version AS event_meta_model_version,
                m.horizon_days AS event_meta_horizon_days
            FROM {SCORES_TABLE} m
            WHERE m.published_on = e.published_on
              AND m.setup_id = e.setup_id
              AND m.symbol = e.symbol
              AND m.unique_id = e.unique_id
            ORDER BY m.scored_at DESC NULLS LAST, m.load_ts DESC NULLS LAST
            LIMIT 1
        ) m ON true
        """
        score_select = """
            , m.event_meta_score
            , m.event_meta_model_name
            , m.event_meta_model_version
            , m.event_meta_horizon_days
        """

    df = sql_to_df(
        f"""
        SELECT
            e.*
            {review_select}
            {score_select}
        FROM advisory_event_evaluations e
        {review_join}
        {score_join}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.published_on, e.setup_id, e.symbol, e.unique_id
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def _reliability_score(value: Any) -> float:
    return {"low": 0.2, "medium": 0.6, "high": 1.0}.get(str(value or "").lower(), 0.4)


def _materiality_score(value: Any) -> float:
    return {"low": 0.25, "medium": 0.6, "high": 1.0}.get(str(value or "").lower(), 0.25)


def review_event_row(row: pd.Series) -> dict[str, Any]:
    asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
    published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
    days_since_event = 0
    if not pd.isna(asof_date) and not pd.isna(published_on):
        days_since_event = max(0, int((asof_date.normalize() - published_on.normalize()).days))

    expected_decay_days = int(max(0, pd.to_numeric(row.get("expected_decay_days"), errors="coerce") or 0))
    decay_ratio = 0.0 if expected_decay_days <= 0 else round(days_since_event / float(expected_decay_days), 4)
    contradiction = float(pd.to_numeric(row.get("contradiction"), errors="coerce") or 0.0)
    confidence = float(pd.to_numeric(row.get("confidence"), errors="coerce") or 0.0)
    novelty = float(pd.to_numeric(row.get("novelty"), errors="coerce") or 0.0)
    surprise = float(pd.to_numeric(row.get("surprise"), errors="coerce") or 0.0)
    score_impact = float(pd.to_numeric(row.get("score_impact"), errors="coerce") or 0.0)
    model_score = pd.to_numeric(row.get("event_meta_score"), errors="coerce")
    reliability_score = _reliability_score(row.get("source_reliability"))
    materiality_score = _materiality_score(row.get("materiality"))
    event_class = str(row.get("event_class") or "").upper()
    verdict = str(row.get("verdict") or "").lower()
    setup_effect = str(row.get("setup_effect") or "").lower()
    state_transition_hint = str(row.get("state_transition_hint") or "").upper()
    investable_now = bool(row.get("investable_now", False))

    flags: list[str] = []
    penalties: list[float] = []

    if contradiction >= 0.70:
        flags.append("high_contradiction")
        penalties.append(0.45)
    elif contradiction >= 0.45:
        flags.append("moderate_contradiction")
        penalties.append(0.20)

    if reliability_score <= 0.2 and confidence <= 0.45:
        flags.append("low_reliability_low_confidence")
        penalties.append(0.30)
    elif reliability_score <= 0.2:
        flags.append("low_reliability")
        penalties.append(0.15)

    if expected_decay_days > 0 and days_since_event > expected_decay_days:
        flags.append("stale_event_decay_exceeded")
        penalties.append(0.35 if score_impact > 0 else 0.15)

    if event_class == "OTHER" and novelty <= 0.20 and materiality_score <= 0.25:
        flags.append("procedural_low_novelty")
        penalties.append(0.12)

    if verdict == "review_manual":
        flags.append("llm_review_manual")
        penalties.append(0.18)

    if state_transition_hint == "UPGRADE_TO_PASS_NOW" and not investable_now and contradiction >= 0.35:
        flags.append("upgrade_but_conflicted")
        penalties.append(0.22)

    if setup_effect in {"weakens", "contradicts"} and score_impact > 0:
        flags.append("inconsistent_positive_scoring")
        penalties.append(0.20)

    if surprise <= 0.15 and novelty <= 0.15 and event_class == "OTHER":
        flags.append("low_information_event")
        penalties.append(0.08)

    if pd.notna(model_score):
        if float(model_score) < 0.20 and score_impact > 0.12:
            flags.append("low_model_edge")
            penalties.append(0.28)
        elif float(model_score) < 0.35:
            flags.append("weak_model_edge")
            penalties.append(0.12)

    raw_review_score = -sum(penalties)
    review_score = round(max(-1.0, min(0.0, raw_review_score)), 4)

    if "high_contradiction" in flags or "stale_event_decay_exceeded" in flags and score_impact > 0.12:
        review_action = "veto"
    elif "llm_review_manual" in flags or "low_reliability_low_confidence" in flags:
        review_action = "review_manual"
    elif review_score <= -0.12:
        review_action = "penalize"
    else:
        review_action = "clear"

    reason = ", ".join(flags[:4]) if flags else "no_material_adversarial_issue"
    feature_snapshot = {
        "days_since_event": days_since_event,
        "expected_decay_days": expected_decay_days,
        "decay_ratio": decay_ratio,
        "contradiction": contradiction,
        "confidence": confidence,
        "novelty": novelty,
        "surprise": surprise,
        "score_impact": score_impact,
        "model_score": None if pd.isna(model_score) else float(model_score),
        "reliability_score": reliability_score,
        "materiality_score": materiality_score,
    }
    return {
        "review_status": "completed",
        "review_action": review_action,
        "review_score": review_score,
        "veto": review_action == "veto",
        "review_reason": reason,
        "review_flags_json": json_dumps(flags),
        "feature_snapshot_json": json_dumps(feature_snapshot),
    }


def build_reviews(events: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    if events.empty:
        return pd.DataFrame(), {"input_event_count": 0, "reviewed_count": 0, "veto_count": 0, "manual_count": 0}
    rows: list[dict[str, Any]] = []
    for _, row in events.iterrows():
        review = review_event_row(row)
        rows.append(
            {
                "published_on": row.get("published_on"),
                "asof_date": row.get("asof_date"),
                "reviewed_at": pd.Timestamp.utcnow(),
                "setup_id": row.get("setup_id"),
                "symbol": row.get("symbol"),
                "unique_id": row.get("unique_id"),
                "event_source": row.get("event_source"),
                **review,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    out = pd.DataFrame(rows)
    meta = {
        "input_event_count": int(len(events)),
        "reviewed_count": int(len(out)),
        "veto_count": int((out["review_action"] == "veto").sum()) if not out.empty else 0,
        "manual_count": int((out["review_action"] == "review_manual").sum()) if not out.empty else 0,
        "penalize_count": int((out["review_action"] == "penalize").sum()) if not out.empty else 0,
    }
    return out, meta


def persist_reviews(reviews: pd.DataFrame) -> None:
    ensure_output_table()
    if reviews.empty:
        return
    upsert_to_db(
        reviews,
        REVIEWS_TABLE,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="published_on",
    )


def summarize(reviews: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "reviews_table": REVIEWS_TABLE,
        **meta,
        "sample": reviews.head(10).to_dict(orient="records") if not reviews.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run adversarial review over advisory event evaluations.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Event-evaluation asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--include-reviewed", action="store_true", help="Re-review rows already present in advisory_event_reviews")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    events = load_event_evaluations(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        include_reviewed=bool(args.include_reviewed),
    )
    reviews, meta = build_reviews(events)
    if not args.dry_run:
        persist_reviews(reviews)
    print(json.dumps({**summarize(reviews, meta), "dry_run": bool(args.dry_run)}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
