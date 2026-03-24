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


def normalize_df(df: pd.DataFrame, date_cols: list[str] | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in date_cols or []:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    return out


def latest_rows(
    table_name: str,
    *,
    symbol: str,
    setup_id: str | None = None,
    date_column: str | None = None,
    symbol_column: str = "symbol",
    extra_where: str = "",
    extra_params: list[object] | None = None,
    limit: int = 20,
) -> pd.DataFrame:
    if not table_exists(table_name):
        return pd.DataFrame()
    clauses = [f"{symbol_column} = %s"]
    params: list[object] = [symbol.upper()]
    if setup_id:
        clauses.append("setup_id = %s")
        params.append(setup_id.upper())
    if extra_where:
        clauses.append(extra_where)
    if extra_params:
        params.extend(extra_params)
    order_by = date_column if date_column else "load_ts"
    return sql_to_df(
        f"""
        SELECT *
        FROM {table_name}
        WHERE {' AND '.join(clauses)}
        ORDER BY {order_by} DESC NULLS LAST, load_ts DESC NULLS LAST
        LIMIT {int(limit)}
        """,
        params=tuple(params),
    )


def latest_single_row(
    table_name: str,
    *,
    symbol: str,
    setup_id: str | None = None,
    date_column: str | None = None,
    symbol_column: str = "symbol",
    extra_where: str = "",
    extra_params: list[object] | None = None,
) -> dict[str, Any] | None:
    df = latest_rows(
        table_name,
        symbol=symbol,
        setup_id=setup_id,
        date_column=date_column,
        symbol_column=symbol_column,
        extra_where=extra_where,
        extra_params=extra_params,
        limit=1,
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def load_latest_screener_rows(symbol: str, setup_id: str | None = None) -> pd.DataFrame:
    if not table_exists("advisory_screener_constituents"):
        return pd.DataFrame()
    params: list[object] = [symbol.upper()]
    setup_join = ""
    if setup_id:
        setup_join = """
        JOIN (
            SELECT DISTINCT screener_slug
            FROM advisory_candidates
            WHERE setup_id = %s
        ) cfg
          ON cfg.screener_slug = s.screener_slug
        """
        params.append(setup_id.upper())
    return sql_to_df(
        f"""
        SELECT s.*
        FROM advisory_screener_constituents s
        {setup_join}
        WHERE s.ticker = %s
          AND s.date = (
              SELECT MAX(date)
              FROM advisory_screener_constituents
              WHERE ticker = %s
          )
        ORDER BY s.rank, s.screener_slug
        """,
        params=tuple(params + [symbol.upper()]),
    )


def load_latest_rejections(symbol: str, setup_id: str | None = None) -> pd.DataFrame:
    if not table_exists("advisory_candidate_rejections"):
        return pd.DataFrame()
    clauses = ["symbol = %s"]
    params: list[object] = [symbol.upper()]
    if setup_id:
        clauses.append("setup_id = %s")
        params.append(setup_id.upper())
    clauses.append(
        """
        asof_date = (
            SELECT MAX(asof_date)
            FROM advisory_candidate_rejections
            WHERE symbol = %s
        )
        """
    )
    params.append(symbol.upper())
    return sql_to_df(
        f"""
        SELECT *
        FROM advisory_candidate_rejections
        WHERE {' AND '.join(clauses)}
        ORDER BY setup_id, reason_code
        """,
        params=tuple(params),
    )


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


def df_to_records(df: pd.DataFrame, date_cols: list[str] | None = None, limit: int = 10) -> list[dict[str, Any]]:
    if df.empty:
        return []
    norm = normalize_df(df, date_cols=date_cols)
    records: list[dict[str, Any]] = []
    for row in norm.head(limit).to_dict(orient="records"):
        records.append(row_to_json_ready(row) or {})
    return records


def build_trace(symbol: str, *, setup_id: str | None = None) -> dict[str, Any]:
    symbol_upper = symbol.upper()
    screener_rows = load_latest_screener_rows(symbol_upper, setup_id=setup_id)
    technical_row = latest_single_row("advisory_technical_daily", symbol=symbol_upper, setup_id=None, date_column="asof_date")
    fundamental_row = latest_single_row("advisory_fundamentals_daily", symbol=symbol_upper, setup_id=None, date_column="asof_date")
    candidate_row = latest_single_row("advisory_candidates", symbol=symbol_upper, setup_id=setup_id, date_column="asof_date")
    rejection_rows = load_latest_rejections(symbol_upper, setup_id=setup_id)
    watchlist_row = latest_single_row("advisory_watchlist", symbol=symbol_upper, setup_id=setup_id, date_column="asof_date")
    announcement_events = latest_rows("advisory_watch_events", symbol=symbol_upper, setup_id=setup_id, date_column="published_on", limit=10)
    news_events = latest_rows("advisory_news_events", symbol=symbol_upper, setup_id=setup_id, date_column="published_on", limit=10)
    event_eval = latest_single_row("advisory_event_evaluations", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    allocation_row = latest_single_row("advisory_allocations", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    portfolio_row = latest_single_row("advisory_portfolio_orders", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    lifecycle_row = latest_single_row("advisory_position_lifecycle", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    execution_row = latest_single_row("advisory_execution_orders", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")

    stage_summary = {
        "in_latest_screener": not screener_rows.empty,
        "has_technical_snapshot": technical_row is not None,
        "has_fundamental_snapshot": fundamental_row is not None,
        "passed_rule_engine": candidate_row is not None,
        "latest_rejection_count": int(len(rejection_rows)),
        "on_watchlist": watchlist_row is not None,
        "announcement_event_count": int(len(announcement_events)),
        "news_event_count": int(len(news_events)),
        "has_event_evaluation": event_eval is not None,
        "has_allocation": allocation_row is not None,
        "has_portfolio_order": portfolio_row is not None,
        "has_lifecycle_row": lifecycle_row is not None,
        "has_execution_row": execution_row is not None,
    }

    decision_summary = {
        "latest_setup_id": (
            (candidate_row or {}).get("setup_id")
            or (watchlist_row or {}).get("setup_id")
            or (event_eval or {}).get("setup_id")
            or (allocation_row or {}).get("setup_id")
            or (portfolio_row or {}).get("setup_id")
        ),
        "latest_rejection_reasons": sorted(
            {
                str(value)
                for value in rejection_rows.get("reason_code", pd.Series(dtype="string")).dropna().astype(str).tolist()
            }
        ),
        "latest_event_verdict": (event_eval or {}).get("verdict"),
        "latest_event_source": (event_eval or {}).get("event_source"),
        "latest_allocation_status": (allocation_row or {}).get("allocation_status"),
        "latest_portfolio_status": (portfolio_row or {}).get("portfolio_status"),
        "latest_execution_status": (execution_row or {}).get("execution_status"),
        "latest_lifecycle_action": (lifecycle_row or {}).get("next_action"),
    }

    return {
        "status": "ok",
        "symbol": symbol_upper,
        "setup_id": setup_id.upper() if setup_id else None,
        "stage_summary": stage_summary,
        "decision_summary": decision_summary,
        "stages": {
            "screeners": df_to_records(screener_rows, date_cols=["date"]),
            "technical": row_to_json_ready(technical_row),
            "fundamentals": row_to_json_ready(fundamental_row),
            "candidate": row_to_json_ready(candidate_row),
            "rejections": df_to_records(rejection_rows, date_cols=["asof_date", "screener_date"]),
            "watchlist": row_to_json_ready(watchlist_row),
            "announcement_events": df_to_records(announcement_events, date_cols=["published_on", "asof_date"]),
            "news_events": df_to_records(news_events, date_cols=["published_on", "asof_date"]),
            "event_evaluation": row_to_json_ready(event_eval),
            "allocation": row_to_json_ready(allocation_row),
            "portfolio": row_to_json_ready(portfolio_row),
            "lifecycle": row_to_json_ready(lifecycle_row),
            "execution": row_to_json_ready(execution_row),
        },
    }


def format_text(trace: dict[str, Any]) -> str:
    lines = [
        f"Symbol: {trace['symbol']}",
        f"Setup: {trace.get('setup_id') or '(latest across setups)'}",
        "",
        "Stage summary:",
    ]
    for key, value in trace["stage_summary"].items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "Decision summary:",
            f"- latest_setup_id: {trace['decision_summary'].get('latest_setup_id')}",
            f"- latest_rejection_reasons: {trace['decision_summary'].get('latest_rejection_reasons')}",
            f"- latest_event_verdict: {trace['decision_summary'].get('latest_event_verdict')}",
            f"- latest_event_source: {trace['decision_summary'].get('latest_event_source')}",
            f"- latest_allocation_status: {trace['decision_summary'].get('latest_allocation_status')}",
            f"- latest_portfolio_status: {trace['decision_summary'].get('latest_portfolio_status')}",
            f"- latest_execution_status: {trace['decision_summary'].get('latest_execution_status')}",
            f"- latest_lifecycle_action: {trace['decision_summary'].get('latest_lifecycle_action')}",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trace one symbol through the advisory pipeline.")
    parser.add_argument("symbol", help="NSE symbol to trace")
    parser.add_argument("--setup", dest="setup_id", help="Optional setup id filter")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--date", type=parse_datetime_arg, help="Reserved for future point-in-time trace support")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trace = build_trace(args.symbol, setup_id=args.setup_id)
    if args.format == "text":
        print(format_text(trace))
    else:
        print(json.dumps(trace, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
