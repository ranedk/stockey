from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.decision_trace import append_trace_step, make_trace_id, record_event_processing, safe_trace_call
from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


REVIEWS_TABLE = "advisory_event_reviews"
REVIEWS_SCHEMA_MIGRATION_ID = "20260611_advisory_event_reviews_base"
SCORES_TABLE = "advisory_event_model_scores"
EXCHANGE_FEATURES_TABLE = "advisory_exchange_features_daily"
EVENT_MODEL_SCORE_POLICY_MODE_ENV = "STOCKEY_EVENT_MODEL_SCORE_POLICY_MODE"
DEFAULT_EVENT_MODEL_SCORE_POLICY_MODE = "research_only"
EVENT_MODEL_SCORE_POLICY_MODES = {"research_only", "promoted"}
REVIEWS_SCHEMA_STATEMENTS = [
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
    """,
]


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
    apply_schema_migration(
        migration_id=REVIEWS_SCHEMA_MIGRATION_ID,
        description="Create advisory adversarial event review table.",
        statements=REVIEWS_SCHEMA_STATEMENTS,
        metadata={"tables": [REVIEWS_TABLE]},
    )


def _event_model_promotion_args() -> argparse.Namespace:
    from advisory import event_meta_model
    from advisory.event_model_promotion_check import DEFAULT_LOG_PATH

    return argparse.Namespace(
        artifact_dir=str(event_meta_model.DEFAULT_ARTIFACT_DIR),
        model_basename=event_meta_model.DEFAULT_MODEL_BASENAME,
        horizon_days=event_meta_model.DEFAULT_HORIZON_DAYS,
        return_threshold=event_meta_model.DEFAULT_RETURN_THRESHOLD,
        log_path=str(DEFAULT_LOG_PATH),
        run_window_days=35,
        max_score_age_days=14,
        min_successful_runs=3,
        min_train_rows=80,
        min_test_rows=20,
        min_labeled_rows=100,
        min_dates=20,
        min_symbols=25,
        min_event_classes=4,
        min_score_rows=10,
        min_precision=0.55,
        min_precision_lift=0.10,
        min_roc_auc=0.55,
    )


def evaluate_event_model_score_policy(policy_mode: str | None = None) -> dict[str, Any]:
    mode = str(policy_mode or os.getenv(EVENT_MODEL_SCORE_POLICY_MODE_ENV, DEFAULT_EVENT_MODEL_SCORE_POLICY_MODE)).strip().lower()
    if mode not in EVENT_MODEL_SCORE_POLICY_MODES:
        return {
            "allowed": False,
            "mode": mode,
            "reason": "invalid_policy_mode",
            "valid_modes": sorted(EVENT_MODEL_SCORE_POLICY_MODES),
        }
    if mode == "research_only":
        return {
            "allowed": False,
            "mode": mode,
            "reason": "research_only_default",
        }

    try:
        from advisory.event_model_promotion_check import build_promotion_check

        payload = build_promotion_check(_event_model_promotion_args())
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.adversarial_review",
            fallback_type="adversarial_event_model_score_policy_check_failed",
            source=SCORES_TABLE,
            severity="warn",
            reason="Event-model score policy gate failed closed before adversarial review could use model scores.",
            error=exc,
            metadata={"policy_mode": mode},
        )
        return {
            "allowed": False,
            "mode": mode,
            "reason": "promotion_check_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    scorecard = payload.get("scorecard") if isinstance(payload, dict) else {}
    usable = isinstance(scorecard, dict) and bool(scorecard.get("usable"))
    return {
        "allowed": bool(usable),
        "mode": mode,
        "reason": "promotion_gate_passed" if usable else "promotion_gate_failed",
        "decision": payload.get("decision") if isinstance(payload, dict) else None,
        "scorecard_status": scorecard.get("status") if isinstance(scorecard, dict) else None,
        "failed_gates": payload.get("failed_gates", []) if isinstance(payload, dict) else [],
    }


def load_event_evaluations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_reviewed: bool = False,
    event_model_score_policy_mode: str | None = None,
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

    score_policy = evaluate_event_model_score_policy(event_model_score_policy_mode)
    score_join = ""
    score_select = ""
    if score_policy.get("allowed") and table_exists(SCORES_TABLE):
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

    exchange_join = ""
    exchange_select = ""
    if table_exists(EXCHANGE_FEATURES_TABLE):
        exchange_join = f"""
        LEFT JOIN LATERAL (
            SELECT
                x.exchange_distribution_score,
                x.insider_net_value_90d,
                x.short_selling_event_count_20d,
                x.exchange_event_score
            FROM {EXCHANGE_FEATURES_TABLE} x
            WHERE x.symbol = e.symbol
              AND x.asof_date <= e.published_on
            ORDER BY x.asof_date DESC
            LIMIT 1
        ) x ON true
        """
        exchange_select = """
            , x.exchange_distribution_score
            , x.insider_net_value_90d
            , x.short_selling_event_count_20d
            , x.exchange_event_score
        """

    df = sql_to_df(
        f"""
        SELECT
            e.*
            {review_select}
            {score_select}
            {exchange_select}
        FROM advisory_event_evaluations e
        {review_join}
        {score_join}
        {exchange_join}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.published_on, e.setup_id, e.symbol, e.unique_id
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        df.attrs["event_model_score_policy"] = score_policy
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df.attrs["event_model_score_policy"] = score_policy
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
    exchange_distribution_score = pd.to_numeric(row.get("exchange_distribution_score"), errors="coerce")
    insider_net_value_90d = pd.to_numeric(row.get("insider_net_value_90d"), errors="coerce")
    short_selling_event_count_20d = pd.to_numeric(row.get("short_selling_event_count_20d"), errors="coerce")
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

    if pd.notna(exchange_distribution_score) and float(exchange_distribution_score) >= 0.35 and score_impact > 0:
        flags.append("exchange_distribution_contradicts_positive_event")
        penalties.append(0.18)
    if pd.notna(insider_net_value_90d) and float(insider_net_value_90d) < 0 and score_impact > 0:
        flags.append("recent_insider_net_selling")
        penalties.append(0.12)
    if pd.notna(short_selling_event_count_20d) and float(short_selling_event_count_20d) >= 3 and score_impact > 0:
        flags.append("repeated_short_selling_pressure")
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
        "exchange_distribution_score": None if pd.isna(exchange_distribution_score) else float(exchange_distribution_score),
        "insider_net_value_90d": None if pd.isna(insider_net_value_90d) else float(insider_net_value_90d),
        "short_selling_event_count_20d": None if pd.isna(short_selling_event_count_20d) else float(short_selling_event_count_20d),
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
        return pd.DataFrame(), {
            "input_event_count": 0,
            "reviewed_count": 0,
            "veto_count": 0,
            "manual_count": 0,
            "event_model_score_policy": events.attrs.get("event_model_score_policy", {}),
        }
    rows: list[dict[str, Any]] = []
    for _, row in events.iterrows():
        review = review_event_row(row)
        _trace_review(row, review)
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
        "event_model_score_policy": events.attrs.get("event_model_score_policy", {}),
    }
    return out, meta


def _trace_review(row: pd.Series, review: dict[str, Any]) -> None:
    trace_id = make_trace_id(
        asof_date=row.get("asof_date"),
        symbol=row.get("symbol"),
        unique_id=row.get("unique_id"),
        trigger_type="event_evaluation",
    )
    safe_trace_call(
        record_event_processing,
        unique_id=row.get("unique_id"),
        symbol=row.get("symbol"),
        source_type=row.get("event_source"),
        stage="adversarial_review",
        status=review.get("review_status", "completed"),
        input_payload=row.to_dict(),
        output_payload=review,
        payload={
            "review_action": review.get("review_action"),
            "review_score": review.get("review_score"),
            "veto": review.get("veto"),
            "review_reason": review.get("review_reason"),
        },
    )
    safe_trace_call(
        append_trace_step,
        trace_id=trace_id,
        step_idx=20,
        stage="adversarial_review",
        status=review.get("review_status", "completed"),
        reason=review.get("review_reason"),
        input_payload=row.to_dict(),
        output_payload=review,
        payload={
            "review_action": review.get("review_action"),
            "review_score": review.get("review_score"),
            "veto": review.get("veto"),
            "flags": review.get("review_flags_json"),
        },
    )


def persist_reviews(reviews: pd.DataFrame) -> None:
    ensure_output_table()
    if reviews.empty:
        return
    out = reviews.copy()
    def _to_bool_series(series: pd.Series) -> pd.Series:
        normalized = series.map(
            lambda value: (
                False if pd.isna(value)
                else value if isinstance(value, bool)
                else str(value).strip().lower() in {"1", "true", "t", "yes", "y"}
            )
        )
        return normalized.astype(bool)
    if "veto" in out.columns:
        out["veto"] = _to_bool_series(out["veto"])
    if "review_score" in out.columns:
        out["review_score"] = pd.to_numeric(out["review_score"], errors="coerce")
    upsert_to_db(
        out,
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
    parser.add_argument(
        "--event-model-score-policy-mode",
        choices=sorted(EVENT_MODEL_SCORE_POLICY_MODES),
        default=os.getenv(EVENT_MODEL_SCORE_POLICY_MODE_ENV, DEFAULT_EVENT_MODEL_SCORE_POLICY_MODE),
        help="Controls whether event meta-model scores can influence adversarial review. Default research_only ignores persisted scores.",
    )
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
        event_model_score_policy_mode=args.event_model_score_policy_mode,
    )
    reviews, meta = build_reviews(events)
    if not args.dry_run:
        persist_reviews(reviews)
    print(json.dumps({**summarize(reviews, meta), "dry_run": bool(args.dry_run)}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
