from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.news_theme_engine import load_active_theme_screener_mapping
from advisory.setup_registry import load_setup_registry
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


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


def normalize_timestamp(value: object) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def row_to_json_ready(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, pd.Timestamp):
            out[key] = None if pd.isna(value) else value.isoformat()
        elif pd.isna(value):
            out[key] = None
        else:
            out[key] = value
    return out


def df_to_records(df: pd.DataFrame, limit: int = 10) -> list[dict[str, Any]]:
    if df.empty:
        return []
    records: list[dict[str, Any]] = []
    for row in df.head(limit).to_dict(orient="records"):
        records.append(row_to_json_ready(row) or {})
    return records


def resolve_asof_date(setup_id: str, requested_date: pd.Timestamp | None = None) -> pd.Timestamp | None:
    if requested_date is not None:
        return requested_date.normalize()
    for table_name, column_name in [
        ("advisory_candidates", "asof_date"),
        ("advisory_candidate_rejections", "asof_date"),
        ("advisory_watchlist", "asof_date"),
        ("advisory_event_evaluations", "asof_date"),
        ("advisory_allocations", "asof_date"),
        ("advisory_portfolio_orders", "asof_date"),
    ]:
        if not table_exists(table_name):
            continue
        df = sql_to_df(
            f"""
            SELECT MAX({column_name}) AS asof_date
            FROM {table_name}
            WHERE setup_id = %s
            """,
            params=(setup_id.upper(),),
        )
        if df.empty:
            continue
        resolved = normalize_timestamp(df.iloc[0].get("asof_date"))
        if resolved is not None:
            return resolved
    return None


def load_setup_regime(asof_date: pd.Timestamp | None) -> dict[str, Any] | None:
    if asof_date is None or not table_exists("advisory_market_regime"):
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


def load_market_overlay(asof_date: pd.Timestamp | None) -> dict[str, Any] | None:
    if asof_date is None or not table_exists("advisory_market_overlay_daily"):
        return None
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_market_overlay_daily
        WHERE asof_date = %s
        LIMIT 1
        """,
        params=(asof_date,),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def resolve_setup_screeners(
    setup_id: str,
    overlay_name: str | None = None,
    *,
    asof_date: pd.Timestamp | None = None,
) -> tuple[list[str], list[str]]:
    screener_slugs: list[str] = []
    active_theme_ids: list[str] = []
    theme_mapping = load_active_theme_screener_mapping(asof_date=asof_date)
    for setup in load_setup_registry():
        if str(setup.get("setup_id", "")).upper() != setup_id.upper():
            continue
        screener_slugs = [str(value) for value in (setup.get("screeners") or setup.get("screener_slugs") or []) if value]
        if not screener_slugs and setup.get("screener_slug"):
            screener_slugs = [str(setup["screener_slug"])]
        if str(setup.get("setup_id", "")).upper() == "EVENT_OPPORTUNITY_V1":
            active_theme_ids = [str(value) for value in (theme_mapping.get("theme_ids") or []) if value]
            for value in (theme_mapping.get("screener_slugs") or []):
                if value and str(value) not in screener_slugs:
                    screener_slugs.append(str(value))
        overlay_cfg = (setup.get("overlay_screeners") or {}).get(str(overlay_name or "NONE").upper(), {})
        remove = {str(value) for value in (overlay_cfg.get("remove") or []) if value}
        add = [str(value) for value in (overlay_cfg.get("add") or []) if value]
        screener_slugs = [value for value in screener_slugs if value not in remove]
        for value in add:
            if value not in screener_slugs:
                screener_slugs.append(value)
        break
    return screener_slugs, active_theme_ids


def load_latest_setup_screener(setup_id: str, overlay_name: str | None = None) -> pd.DataFrame:
    if not table_exists("advisory_screener_constituents"):
        return pd.DataFrame()
    screener_slugs, _ = resolve_setup_screeners(setup_id, overlay_name=overlay_name)
    if not screener_slugs:
        return pd.DataFrame()
    return sql_to_df(
        """
        WITH
        latest AS (
            SELECT MAX(date) AS screener_date
            FROM advisory_screener_constituents
            WHERE screener_slug = ANY(%s)
        )
        SELECT s.*
        FROM advisory_screener_constituents s
        JOIN latest l
          ON l.screener_date = s.date
        WHERE s.screener_slug = ANY(%s)
        ORDER BY s.rank, s.ticker
        """,
        params=(screener_slugs, screener_slugs),
    )


def load_setup_rows(table_name: str, setup_id: str, asof_date: pd.Timestamp | None, limit: int = 50) -> pd.DataFrame:
    if not table_exists(table_name):
        return pd.DataFrame()
    clauses = ["setup_id = %s"]
    params: list[object] = [setup_id.upper()]
    if asof_date is not None and "asof_date" in sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
        """,
        params=(table_name,),
    )["column_name"].tolist():
        clauses.append("asof_date = %s")
        params.append(asof_date)
    order_column = "published_on" if table_name in {
        "advisory_watch_events",
        "advisory_news_events",
        "advisory_event_evaluations",
        "advisory_allocations",
        "advisory_portfolio_orders",
        "advisory_position_lifecycle",
        "advisory_execution_orders",
    } else "asof_date"
    return sql_to_df(
        f"""
        SELECT *
        FROM {table_name}
        WHERE {' AND '.join(clauses)}
        ORDER BY {order_column} DESC NULLS LAST, load_ts DESC NULLS LAST
        LIMIT {int(limit)}
        """,
        params=tuple(params),
    )


def load_top_rejection_reasons(setup_id: str, asof_date: pd.Timestamp | None) -> dict[str, int]:
    df = load_setup_rows("advisory_candidate_rejections", setup_id, asof_date, limit=5000)
    if df.empty or "reason_code" not in df.columns:
        return {}
    return df["reason_code"].astype("string").value_counts().head(15).to_dict()


def build_trace(setup_id: str, *, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    setup_id_upper = setup_id.upper()
    resolved_asof_date = resolve_asof_date(setup_id_upper, requested_date=asof_date)

    theme_mapping = load_active_theme_screener_mapping(asof_date=resolved_asof_date)
    overlay_row = load_market_overlay(resolved_asof_date)
    screener_rows = load_latest_setup_screener(setup_id_upper, overlay_name=(overlay_row or {}).get("overlay_name"))
    candidate_rows = load_setup_rows("advisory_candidates", setup_id_upper, resolved_asof_date)
    rejection_rows = load_setup_rows("advisory_candidate_rejections", setup_id_upper, resolved_asof_date, limit=5000)
    watchlist_rows = load_setup_rows("advisory_watchlist", setup_id_upper, resolved_asof_date)
    announcement_rows = load_setup_rows("advisory_watch_events", setup_id_upper, resolved_asof_date)
    news_rows = load_setup_rows("advisory_news_events", setup_id_upper, resolved_asof_date)
    evaluation_rows = load_setup_rows("advisory_event_evaluations", setup_id_upper, resolved_asof_date)
    allocation_rows = load_setup_rows("advisory_allocations", setup_id_upper, resolved_asof_date)
    portfolio_rows = load_setup_rows("advisory_portfolio_orders", setup_id_upper, resolved_asof_date)
    lifecycle_rows = load_setup_rows("advisory_position_lifecycle", setup_id_upper, resolved_asof_date)
    execution_rows = load_setup_rows("advisory_execution_orders", setup_id_upper, resolved_asof_date)
    regime_row = load_setup_regime(resolved_asof_date)

    screener_symbols = set(screener_rows.get("ticker", pd.Series(dtype="string")).dropna().astype(str).str.upper().tolist())
    candidate_symbols = set(candidate_rows.get("symbol", pd.Series(dtype="string")).dropna().astype(str).str.upper().tolist())
    watch_symbols = set(watchlist_rows.get("symbol", pd.Series(dtype="string")).dropna().astype(str).str.upper().tolist())
    evaluated_symbols = set(evaluation_rows.get("symbol", pd.Series(dtype="string")).dropna().astype(str).str.upper().tolist())
    allocated_symbols = set(allocation_rows.get("symbol", pd.Series(dtype="string")).dropna().astype(str).str.upper().tolist())
    portfolio_symbols = set(portfolio_rows.get("symbol", pd.Series(dtype="string")).dropna().astype(str).str.upper().tolist())
    active_screeners, active_theme_ids = resolve_setup_screeners(
        setup_id_upper,
        overlay_name=(overlay_row or {}).get("overlay_name"),
        asof_date=resolved_asof_date,
    )
    active_theme_agent_roles = theme_mapping.get("recommended_agent_roles") or []
    active_theme_pipeline_branches = theme_mapping.get("recommended_pipeline_branches") or []
    candidate_count_by_screener = (
        screener_rows.get("screener_slug", pd.Series(dtype="string")).dropna().astype("string").value_counts().to_dict()
        if not screener_rows.empty and "screener_slug" in screener_rows.columns
        else {}
    )
    candidate_state_counts = (
        candidate_rows.get("candidate_state", pd.Series(dtype="string")).dropna().astype("string").value_counts().to_dict()
        if not candidate_rows.empty
        else {}
    )
    current_watch_state_counts = (
        watchlist_rows.get("current_state", pd.Series(dtype="string")).dropna().astype("string").value_counts().to_dict()
        if not watchlist_rows.empty
        else {}
    )
    latest_transition_counts = (
        evaluation_rows.get("state_transition_hint", pd.Series(dtype="string")).dropna().astype("string").value_counts().to_dict()
        if not evaluation_rows.empty
        else {}
    )
    avg_setup_score = None
    avg_technical_score = None
    avg_fundamental_score = None
    if not candidate_rows.empty:
        if "setup_score" in candidate_rows.columns:
            avg_setup_score = round(float(pd.to_numeric(candidate_rows["setup_score"], errors="coerce").dropna().mean()), 6) if not pd.to_numeric(candidate_rows["setup_score"], errors="coerce").dropna().empty else None
        if "technical_score" in candidate_rows.columns:
            avg_technical_score = round(float(pd.to_numeric(candidate_rows["technical_score"], errors="coerce").dropna().mean()), 6) if not pd.to_numeric(candidate_rows["technical_score"], errors="coerce").dropna().empty else None
        if "fundamental_score" in candidate_rows.columns:
            avg_fundamental_score = round(float(pd.to_numeric(candidate_rows["fundamental_score"], errors="coerce").dropna().mean()), 6) if not pd.to_numeric(candidate_rows["fundamental_score"], errors="coerce").dropna().empty else None
    near_miss_count = 0
    if not candidate_rows.empty and "near_miss_flag" in candidate_rows.columns:
        near_miss_count += int(candidate_rows["near_miss_flag"].fillna(False).astype(bool).sum())
    if not rejection_rows.empty and "is_near_miss" in rejection_rows.columns:
        near_miss_count += int(rejection_rows["is_near_miss"].fillna(False).astype(bool).sum())

    stage_summary = {
        "asof_date": None if resolved_asof_date is None else resolved_asof_date.isoformat(),
        "regime_name": (regime_row or {}).get("regime_name"),
        "overlay_name": (overlay_row or {}).get("overlay_name"),
        "overlay_reason": (overlay_row or {}).get("overlay_reason"),
        "active_theme_ids": active_theme_ids,
        "active_theme_agent_roles": active_theme_agent_roles,
        "active_theme_pipeline_branches": active_theme_pipeline_branches,
        "active_screeners": active_screeners,
        "screener_universe_count": int(len(screener_symbols)),
        "candidate_count_by_screener": candidate_count_by_screener,
        "candidate_count": int(len(candidate_symbols)),
        "rejection_count": int(len(rejection_rows)),
        "watchlist_count": int(len(watch_symbols)),
        "announcement_event_count": int(len(announcement_rows)),
        "news_event_count": int(len(news_rows)),
        "evaluation_count": int(len(evaluated_symbols)),
        "allocation_count": int(len(allocated_symbols)),
        "portfolio_count": int(len(portfolio_symbols)),
        "lifecycle_count": int(len(lifecycle_rows)),
        "execution_count": int(len(execution_rows)),
        "watch_state_counts": candidate_state_counts,
        "current_watch_state_counts": current_watch_state_counts,
        "latest_transition_counts": latest_transition_counts,
        "avg_setup_score": avg_setup_score,
        "avg_technical_score": avg_technical_score,
        "avg_fundamental_score": avg_fundamental_score,
        "near_miss_count": near_miss_count,
    }

    funnel_summary = {
        "screener_to_candidate": round((len(candidate_symbols) / len(screener_symbols)), 4) if screener_symbols else None,
        "candidate_to_watchlist": round((len(watch_symbols) / len(candidate_symbols)), 4) if candidate_symbols else None,
        "watchlist_to_evaluation": round((len(evaluated_symbols) / len(watch_symbols)), 4) if watch_symbols else None,
        "evaluation_to_allocation": round((len(allocated_symbols) / len(evaluated_symbols)), 4) if evaluated_symbols else None,
        "allocation_to_portfolio": round((len(portfolio_symbols) / len(allocated_symbols)), 4) if allocated_symbols else None,
    }

    latest_eval = evaluation_rows.head(1)
    latest_eval_row = latest_eval.iloc[0].to_dict() if not latest_eval.empty else None
    latest_portfolio = portfolio_rows.head(1)
    latest_portfolio_row = latest_portfolio.iloc[0].to_dict() if not latest_portfolio.empty else None

    decision_summary = {
        "top_rejection_reasons": load_top_rejection_reasons(setup_id_upper, resolved_asof_date),
        "overlay_name": (overlay_row or {}).get("overlay_name"),
        "overlay_reason": (overlay_row or {}).get("overlay_reason"),
        "active_theme_ids": active_theme_ids,
        "active_theme_agent_roles": active_theme_agent_roles,
        "active_theme_pipeline_branches": active_theme_pipeline_branches,
        "active_screeners": active_screeners,
        "candidate_count_by_screener": candidate_count_by_screener,
        "latest_event_verdict": (latest_eval_row or {}).get("verdict"),
        "latest_event_source": (latest_eval_row or {}).get("event_source"),
        "latest_portfolio_status": (latest_portfolio_row or {}).get("portfolio_status"),
    }

    return {
        "status": "ok",
        "setup_id": setup_id_upper,
        "stage_summary": stage_summary,
        "funnel_summary": funnel_summary,
        "decision_summary": decision_summary,
        "stages": {
            "regime": row_to_json_ready(regime_row),
            "overlay": row_to_json_ready(overlay_row),
            "screeners": df_to_records(screener_rows, limit=15),
            "candidates": df_to_records(candidate_rows, limit=15),
            "rejections": df_to_records(rejection_rows, limit=15),
            "watchlist": df_to_records(watchlist_rows, limit=15),
            "announcement_events": df_to_records(announcement_rows, limit=15),
            "news_events": df_to_records(news_rows, limit=15),
            "event_evaluations": df_to_records(evaluation_rows, limit=15),
            "allocations": df_to_records(allocation_rows, limit=15),
            "portfolio": df_to_records(portfolio_rows, limit=15),
            "lifecycle": df_to_records(lifecycle_rows, limit=15),
            "execution": df_to_records(execution_rows, limit=15),
        },
    }


def format_text(trace: dict[str, Any]) -> str:
    lines = [
        f"Setup: {trace['setup_id']}",
        f"As of: {trace['stage_summary'].get('asof_date')}",
        "",
        "Stage summary:",
    ]
    for key, value in trace["stage_summary"].items():
        if key == "asof_date":
            continue
        lines.append(f"- {key}: {value}")
    lines.extend(["", "Funnel summary:"])
    for key, value in trace["funnel_summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "Decision summary:",
            f"- top_rejection_reasons: {trace['decision_summary'].get('top_rejection_reasons')}",
            f"- overlay_name: {trace['decision_summary'].get('overlay_name')}",
            f"- overlay_reason: {trace['decision_summary'].get('overlay_reason')}",
            f"- active_theme_ids: {trace['decision_summary'].get('active_theme_ids')}",
            f"- active_theme_agent_roles: {trace['decision_summary'].get('active_theme_agent_roles')}",
            f"- active_theme_pipeline_branches: {trace['decision_summary'].get('active_theme_pipeline_branches')}",
            f"- active_screeners: {trace['decision_summary'].get('active_screeners')}",
            f"- candidate_count_by_screener: {trace['decision_summary'].get('candidate_count_by_screener')}",
            f"- watch_state_counts: {trace['stage_summary'].get('watch_state_counts')}",
            f"- current_watch_state_counts: {trace['stage_summary'].get('current_watch_state_counts')}",
            f"- latest_transition_counts: {trace['stage_summary'].get('latest_transition_counts')}",
            f"- avg_setup_score: {trace['stage_summary'].get('avg_setup_score')}",
            f"- near_miss_count: {trace['stage_summary'].get('near_miss_count')}",
            f"- latest_event_verdict: {trace['decision_summary'].get('latest_event_verdict')}",
            f"- latest_event_source: {trace['decision_summary'].get('latest_event_source')}",
            f"- latest_portfolio_status: {trace['decision_summary'].get('latest_portfolio_status')}",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trace one setup through the advisory pipeline.")
    parser.add_argument("setup_id", help="Setup id to trace")
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date in YYYY-MM-DD")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    trace = build_trace(args.setup_id, asof_date=asof_date)
    if args.format == "text":
        print(format_text(trace))
    else:
        print(json.dumps(trace, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
