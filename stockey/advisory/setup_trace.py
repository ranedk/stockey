from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

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


def load_latest_setup_screener(setup_id: str) -> pd.DataFrame:
    if not table_exists("advisory_candidates") or not table_exists("advisory_screener_constituents"):
        return pd.DataFrame()
    return sql_to_df(
        """
        WITH cfg AS (
            SELECT DISTINCT screener_slug
            FROM advisory_candidates
            WHERE setup_id = %s
            UNION
            SELECT DISTINCT screener_slug
            FROM advisory_screener_constituents
            WHERE screener_slug = (
                SELECT screener_slug
                FROM advisory_candidates
                WHERE setup_id = %s
                ORDER BY asof_date DESC
                LIMIT 1
            )
        ),
        latest AS (
            SELECT MAX(date) AS screener_date
            FROM advisory_screener_constituents
            WHERE screener_slug IN (SELECT screener_slug FROM cfg)
        )
        SELECT s.*
        FROM advisory_screener_constituents s
        JOIN latest l
          ON l.screener_date = s.date
        WHERE s.screener_slug IN (SELECT screener_slug FROM cfg)
        ORDER BY s.rank, s.ticker
        """,
        params=(setup_id.upper(), setup_id.upper()),
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

    screener_rows = load_latest_setup_screener(setup_id_upper)
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

    stage_summary = {
        "asof_date": None if resolved_asof_date is None else resolved_asof_date.isoformat(),
        "regime_name": (regime_row or {}).get("regime_name"),
        "screener_universe_count": int(len(screener_symbols)),
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
