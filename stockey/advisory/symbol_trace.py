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


def load_aggregated_event_decision(symbol: str, setup_id: str | None = None) -> dict[str, Any] | None:
    if not table_exists("advisory_event_evaluations"):
        return None

    clauses = ["symbol = %s"]
    params: list[object] = [symbol.upper()]
    if setup_id:
        clauses.append("setup_id = %s")
        params.append(setup_id.upper())
    clauses.append(
        """
        asof_date = (
            SELECT MAX(asof_date)
            FROM advisory_event_evaluations
            WHERE symbol = %s
        )
        """
    )
    params.append(symbol.upper())

    df = sql_to_df(
        f"""
        SELECT *
        FROM advisory_event_evaluations
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on, load_ts NULLS LAST
        """,
        params=tuple(params),
    )
    if df.empty:
        return None

    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["score_impact"] = pd.to_numeric(df["score_impact"], errors="coerce").fillna(0.0)
    latest = df.sort_values(["published_on"], ascending=[True], kind="stable").iloc[-1].to_dict()
    hints = {str(value).upper() for value in df.get("state_transition_hint", pd.Series(dtype="string")).dropna().astype(str)}
    verdicts = {str(value).lower() for value in df.get("verdict", pd.Series(dtype="string")).dropna().astype(str)}
    total_score_impact = round(max(-0.35, min(0.35, float(df["score_impact"].sum()))), 4)
    any_investable = bool(pd.Series(df.get("investable_now")).fillna(False).astype(bool).any())

    if "DOWNGRADE_TO_REJECT" in hints or "reject" in verdicts:
        transition_hint = "DOWNGRADE_TO_REJECT"
    elif "UPGRADE_TO_PASS_NOW" in hints:
        transition_hint = "UPGRADE_TO_PASS_NOW"
    elif total_score_impact >= 0.12:
        transition_hint = "RAISE_SCORE_ONLY"
    elif total_score_impact <= -0.12:
        transition_hint = "CUT_SCORE_ONLY"
    elif "REVIEW_MANUAL" in hints:
        transition_hint = "REVIEW_MANUAL"
    else:
        transition_hint = "NO_CHANGE"

    if "reject" in verdicts:
        verdict = "reject"
    elif "continue" in verdicts:
        verdict = "continue"
    elif "review_manual" in verdicts:
        verdict = "review_manual"
    else:
        verdict = str(latest.get("verdict") or "review_manual")

    if verdict == "reject" or transition_hint == "DOWNGRADE_TO_REJECT":
        investable_now = False
    else:
        investable_now = any_investable

    return {
        "asof_date": latest.get("asof_date"),
        "published_on": latest.get("published_on"),
        "setup_id": latest.get("setup_id"),
        "symbol": latest.get("symbol"),
        "effective_event_class": latest.get("event_class"),
        "effective_event_verdict": verdict,
        "effective_state_transition_hint": transition_hint,
        "effective_event_score_impact": total_score_impact,
        "effective_investable_now": investable_now,
        "effective_event_source": latest.get("event_source"),
        "raw_event_count": int(len(df)),
        "has_review_manual": "REVIEW_MANUAL" in hints or "review_manual" in verdicts,
    }


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
    aggregated_event = load_aggregated_event_decision(symbol_upper, setup_id=setup_id)
    allocation_row = latest_single_row("advisory_allocations", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    portfolio_row = latest_single_row("advisory_portfolio_orders", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    lifecycle_row = latest_single_row("advisory_position_lifecycle", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")
    execution_row = latest_single_row("advisory_execution_orders", symbol=symbol_upper, setup_id=setup_id, date_column="published_on")

    stage_summary = {
        "in_latest_screener": not screener_rows.empty,
        "has_technical_snapshot": technical_row is not None,
        "has_fundamental_snapshot": fundamental_row is not None,
        "passed_rule_engine": candidate_row is not None,
        "base_regime": (candidate_row or {}).get("base_regime") or (watchlist_row or {}).get("base_regime"),
        "news_overlay": (candidate_row or {}).get("news_overlay") or (watchlist_row or {}).get("news_overlay"),
        "candidate_state": (candidate_row or {}).get("candidate_state"),
        "current_watch_state": (watchlist_row or {}).get("current_state") or (watchlist_row or {}).get("candidate_state"),
        "latest_rejection_count": int(len(rejection_rows)),
        "on_watchlist": watchlist_row is not None,
        "announcement_event_count": int(len(announcement_events)),
        "news_event_count": int(len(news_events)),
        "has_event_evaluation": event_eval is not None,
        "has_aggregated_event_decision": aggregated_event is not None,
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
        "latest_event_class": (event_eval or {}).get("event_class"),
        "latest_state_transition_hint": (event_eval or {}).get("state_transition_hint"),
        "latest_event_source": (event_eval or {}).get("event_source"),
        "source_screener_slug": (watchlist_row or {}).get("source_screener_slug") or (candidate_row or {}).get("source_screener_slug"),
        "source_screener_list": (watchlist_row or {}).get("source_screener_list") or (candidate_row or {}).get("source_screener_list"),
        "effective_event_verdict": (aggregated_event or {}).get("effective_event_verdict"),
        "effective_event_class": (aggregated_event or {}).get("effective_event_class"),
        "effective_state_transition_hint": (aggregated_event or {}).get("effective_state_transition_hint"),
        "effective_event_score_impact": (aggregated_event or {}).get("effective_event_score_impact"),
        "effective_investable_now": (aggregated_event or {}).get("effective_investable_now"),
        "effective_event_source": (aggregated_event or {}).get("effective_event_source"),
        "effective_raw_event_count": (aggregated_event or {}).get("raw_event_count"),
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
            "aggregated_event_decision": row_to_json_ready(aggregated_event),
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
            f"- latest_event_class: {trace['decision_summary'].get('latest_event_class')}",
            f"- latest_state_transition_hint: {trace['decision_summary'].get('latest_state_transition_hint')}",
            f"- latest_event_source: {trace['decision_summary'].get('latest_event_source')}",
            f"- source_screener_slug: {trace['decision_summary'].get('source_screener_slug')}",
            f"- source_screener_list: {trace['decision_summary'].get('source_screener_list')}",
            f"- effective_event_verdict: {trace['decision_summary'].get('effective_event_verdict')}",
            f"- effective_event_class: {trace['decision_summary'].get('effective_event_class')}",
            f"- effective_state_transition_hint: {trace['decision_summary'].get('effective_state_transition_hint')}",
            f"- effective_event_score_impact: {trace['decision_summary'].get('effective_event_score_impact')}",
            f"- effective_investable_now: {trace['decision_summary'].get('effective_investable_now')}",
            f"- effective_event_source: {trace['decision_summary'].get('effective_event_source')}",
            f"- effective_raw_event_count: {trace['decision_summary'].get('effective_raw_event_count')}",
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
