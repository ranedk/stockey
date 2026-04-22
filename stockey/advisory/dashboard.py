from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.setup_registry import load_setup_registry
from advisory.setup_trace import (
    load_market_overlay,
    load_setup_regime,
    resolve_setup_screeners,
    table_exists,
)
from utils.db import sql_to_df
from utils.display_time import to_display_value
from utils.sync import parse_datetime_arg


def _resolve_dashboard_date(requested_date: pd.Timestamp | None = None) -> pd.Timestamp | None:
    if requested_date is not None:
        return requested_date.normalize()
    for table_name in [
        "advisory_candidates",
        "advisory_watchlist",
        "advisory_candidate_rejections",
        "advisory_event_evaluations",
        "advisory_allocations",
        "advisory_portfolio_orders",
    ]:
        if not table_exists(table_name):
            continue
        df = sql_to_df(f"SELECT MAX(asof_date) AS asof_date FROM {table_name}")
        if df.empty:
            continue
        value = pd.to_datetime(df.iloc[0].get("asof_date"), utc=True, errors="coerce")
        if pd.notna(value):
            return value.normalize()
    return None


def _load_asof_table(table_name: str, asof_date: pd.Timestamp | None, columns: list[str] | None = None) -> pd.DataFrame:
    if asof_date is None or not table_exists(table_name):
        return pd.DataFrame(columns=columns or [])
    select_list = ", ".join(columns) if columns else "*"
    return sql_to_df(
        f"""
        SELECT {select_list}
        FROM {table_name}
        WHERE asof_date = %s
        """,
        params=(asof_date,),
        statement_timeout_ms=0,
        retries=4,
    )


def _load_latest_screener_universe(active_screeners_by_setup: dict[str, list[str]]) -> pd.DataFrame:
    all_screeners = sorted({slug for values in active_screeners_by_setup.values() for slug in values if slug})
    if not all_screeners or not table_exists("advisory_screener_constituents"):
        return pd.DataFrame(columns=["screener_slug", "ticker"])
    return sql_to_df(
        """
        WITH latest AS (
            SELECT screener_slug, MAX(date) AS screener_date
            FROM advisory_screener_constituents
            WHERE screener_slug = ANY(%s)
            GROUP BY screener_slug
        )
        SELECT s.screener_slug, s.ticker
        FROM advisory_screener_constituents s
        JOIN latest l
          ON l.screener_slug = s.screener_slug
         AND l.screener_date = s.date
        WHERE s.screener_slug = ANY(%s)
        """,
        params=(all_screeners, all_screeners),
        statement_timeout_ms=0,
        retries=4,
    )


def _count_unique(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, int]:
    if df.empty or group_col not in df.columns or value_col not in df.columns:
        return {}
    grouped = (
        df.dropna(subset=[group_col, value_col])
        .assign(
            **{
                group_col: df[group_col].astype("string").str.upper(),
                value_col: df[value_col].astype("string").str.upper(),
            }
        )
        .groupby(group_col)[value_col]
        .nunique()
    )
    return {str(key): int(value) for key, value in grouped.items()}


def _count_rows(df: pd.DataFrame, group_col: str) -> dict[str, int]:
    if df.empty or group_col not in df.columns:
        return {}
    grouped = df.assign(**{group_col: df[group_col].astype("string").str.upper()}).groupby(group_col).size()
    return {str(key): int(value) for key, value in grouped.items()}


def _mean_numeric(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, float]:
    if df.empty or group_col not in df.columns or value_col not in df.columns:
        return {}
    work = df[[group_col, value_col]].copy()
    work[group_col] = work[group_col].astype("string").str.upper()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")
    grouped = work.dropna(subset=[group_col, value_col]).groupby(group_col)[value_col].mean()
    return {str(key): round(float(value), 6) for key, value in grouped.items()}


def _value_counts_by_group(df: pd.DataFrame, group_col: str, value_col: str) -> dict[str, dict[str, int]]:
    if df.empty or group_col not in df.columns or value_col not in df.columns:
        return {}
    work = df[[group_col, value_col]].copy()
    work[group_col] = work[group_col].astype("string").str.upper()
    work[value_col] = work[value_col].astype("string")
    work = work.dropna(subset=[group_col, value_col])
    out: dict[str, dict[str, int]] = {}
    for setup_id, setup_df in work.groupby(group_col):
        counts = setup_df[value_col].value_counts().to_dict()
        out[str(setup_id)] = {str(key): int(value) for key, value in counts.items()}
    return out


def _top_reason_by_setup(df: pd.DataFrame) -> dict[str, tuple[str, int]]:
    counts_by_setup = _value_counts_by_group(df, "setup_id", "reason_code")
    out: dict[str, tuple[str, int]] = {}
    for setup_id, counts in counts_by_setup.items():
        if not counts:
            continue
        reason, count = next(iter(counts.items()))
        out[setup_id] = (reason, int(count))
    return out


def _latest_row_value(
    df: pd.DataFrame,
    *,
    group_col: str,
    order_cols: list[str],
    value_col: str,
) -> dict[str, Any]:
    if df.empty or group_col not in df.columns or value_col not in df.columns:
        return {}
    work = df.copy()
    work[group_col] = work[group_col].astype("string").str.upper()
    available_order_cols = [column for column in order_cols if column in work.columns]
    if available_order_cols:
        for column in available_order_cols:
            work[column] = pd.to_datetime(work[column], utc=True, errors="coerce")
        work = work.sort_values(available_order_cols, ascending=[False] * len(available_order_cols), na_position="last")
    latest = work.dropna(subset=[group_col]).drop_duplicates(subset=[group_col], keep="first")
    return {str(row[group_col]): row.get(value_col) for _, row in latest.iterrows()}


def _bool_sum_by_setup(df: pd.DataFrame, flag_col: str) -> dict[str, int]:
    if df.empty or "setup_id" not in df.columns or flag_col not in df.columns:
        return {}
    work = df[["setup_id", flag_col]].copy()
    work["setup_id"] = work["setup_id"].astype("string").str.upper()
    work[flag_col] = work[flag_col].fillna(False).astype(bool)
    grouped = work.groupby("setup_id")[flag_col].sum()
    return {str(key): int(value) for key, value in grouped.items()}


def build_dashboard(*, asof_date: pd.Timestamp | None = None, setup_ids: list[str] | None = None) -> pd.DataFrame:
    resolved_asof_date = _resolve_dashboard_date(asof_date)
    setups = load_setup_registry()
    if setup_ids:
        selected = {value.upper() for value in setup_ids}
        setups = [setup for setup in setups if str(setup["setup_id"]).upper() in selected]

    regime_row = load_setup_regime(resolved_asof_date) or {}
    overlay_row = load_market_overlay(resolved_asof_date) or {}

    active_screeners_by_setup: dict[str, list[str]] = {}
    active_themes_by_setup: dict[str, list[str]] = {}
    for setup in setups:
        setup_id = str(setup["setup_id"]).upper()
        active_screeners, active_theme_ids = resolve_setup_screeners(
            setup_id,
            overlay_name=overlay_row.get("overlay_name"),
            asof_date=resolved_asof_date,
        )
        active_screeners_by_setup[setup_id] = active_screeners
        active_themes_by_setup[setup_id] = active_theme_ids

    candidates = _load_asof_table(
        "advisory_candidates",
        resolved_asof_date,
        columns=[
            "setup_id",
            "symbol",
            "candidate_state",
            "setup_score",
            "technical_score",
            "fundamental_score",
            "near_miss_flag",
            "source_screener_slug",
        ],
    )
    rejections = _load_asof_table(
        "advisory_candidate_rejections",
        resolved_asof_date,
        columns=["setup_id", "symbol", "reason_code", "is_near_miss"],
    )
    watchlist = _load_asof_table(
        "advisory_watchlist",
        resolved_asof_date,
        columns=["setup_id", "symbol", "current_state"],
    )
    evaluations = _load_asof_table(
        "advisory_event_evaluations",
        resolved_asof_date,
        columns=["setup_id", "symbol", "verdict", "event_source", "evaluated_at", "published_on"],
    )
    allocations = _load_asof_table(
        "advisory_allocations",
        resolved_asof_date,
        columns=["setup_id", "symbol", "published_on"],
    )
    portfolio = _load_asof_table(
        "advisory_portfolio_orders",
        resolved_asof_date,
        columns=["setup_id", "symbol", "portfolio_status", "published_on"],
    )
    lifecycle = _load_asof_table(
        "advisory_position_lifecycle",
        resolved_asof_date,
        columns=["setup_id", "symbol"],
    )
    watch_events = _load_asof_table("advisory_watch_events", resolved_asof_date, columns=["setup_id"])
    news_events = _load_asof_table("advisory_news_events", resolved_asof_date, columns=["setup_id"])
    execution = _load_asof_table("advisory_execution_orders", resolved_asof_date, columns=["setup_id", "symbol"])
    screener_universe = _load_latest_screener_universe(active_screeners_by_setup)

    candidate_counts = _count_unique(candidates, "setup_id", "symbol")
    rejection_counts = _count_rows(rejections, "setup_id")
    watchlist_counts = _count_unique(watchlist, "setup_id", "symbol")
    evaluation_counts = _count_unique(evaluations, "setup_id", "symbol")
    allocation_counts = _count_unique(allocations, "setup_id", "symbol")
    portfolio_counts = _count_unique(portfolio, "setup_id", "symbol")
    lifecycle_counts = _count_unique(lifecycle, "setup_id", "symbol")
    execution_counts = _count_unique(execution, "setup_id", "symbol")
    announcement_counts = _count_rows(watch_events, "setup_id")
    news_counts = _count_rows(news_events, "setup_id")

    avg_setup_scores = _mean_numeric(candidates, "setup_id", "setup_score")
    avg_technical_scores = _mean_numeric(candidates, "setup_id", "technical_score")
    avg_fundamental_scores = _mean_numeric(candidates, "setup_id", "fundamental_score")
    watch_state_counts = _value_counts_by_group(candidates, "setup_id", "candidate_state")
    current_watch_state_counts = _value_counts_by_group(watchlist, "setup_id", "current_state")
    candidate_count_by_screener = _value_counts_by_group(candidates, "setup_id", "source_screener_slug")
    top_rejections = _top_reason_by_setup(rejections)
    latest_event_verdict = _latest_row_value(
        evaluations,
        group_col="setup_id",
        order_cols=["evaluated_at", "published_on"],
        value_col="verdict",
    )
    latest_event_source = _latest_row_value(
        evaluations,
        group_col="setup_id",
        order_cols=["evaluated_at", "published_on"],
        value_col="event_source",
    )
    latest_portfolio_status = _latest_row_value(
        portfolio,
        group_col="setup_id",
        order_cols=["published_on"],
        value_col="portfolio_status",
    )

    near_miss_counts = _bool_sum_by_setup(candidates, "near_miss_flag")
    rejection_near_miss_counts = _bool_sum_by_setup(rejections, "is_near_miss")
    for setup_id, value in rejection_near_miss_counts.items():
        near_miss_counts[setup_id] = near_miss_counts.get(setup_id, 0) + int(value)

    screener_universe_counts: dict[str, int] = {}
    if not screener_universe.empty and {"screener_slug", "ticker"}.issubset(screener_universe.columns):
        screener_universe = screener_universe.copy()
        screener_universe["screener_slug"] = screener_universe["screener_slug"].astype("string")
        screener_universe["ticker"] = screener_universe["ticker"].astype("string").str.upper()
        for setup_id, screeners in active_screeners_by_setup.items():
            if not screeners:
                screener_universe_counts[setup_id] = 0
                continue
            matched = screener_universe[screener_universe["screener_slug"].isin(screeners)]
            screener_universe_counts[setup_id] = int(matched["ticker"].nunique())

    rows: list[dict[str, Any]] = []
    for setup in setups:
        setup_id = str(setup["setup_id"]).upper()
        setup_name = setup["setup_name"]
        screener_universe_count = screener_universe_counts.get(setup_id, 0)
        candidate_count = candidate_counts.get(setup_id, 0)
        watchlist_count = watchlist_counts.get(setup_id, 0)
        evaluation_count = evaluation_counts.get(setup_id, 0)
        allocation_count = allocation_counts.get(setup_id, 0)
        portfolio_count = portfolio_counts.get(setup_id, 0)
        top_reason = top_rejections.get(setup_id)
        rows.append(
            {
                "setup_id": setup_id,
                "setup_name": setup_name,
                "screener_slug": setup.get("screener_slug"),
                "active_screeners": active_screeners_by_setup.get(setup_id, []),
                "active_theme_ids": active_themes_by_setup.get(setup_id, []),
                "asof_date": resolved_asof_date,
                "regime_name": regime_row.get("regime_name"),
                "overlay_name": overlay_row.get("overlay_name"),
                "overlay_reason": overlay_row.get("overlay_reason"),
                "screener_universe_count": screener_universe_count,
                "candidate_count_by_screener": candidate_count_by_screener.get(setup_id, {}),
                "candidate_count": candidate_count,
                "rejection_count": rejection_counts.get(setup_id, 0),
                "watchlist_count": watchlist_count,
                "announcement_event_count": announcement_counts.get(setup_id, 0),
                "news_event_count": news_counts.get(setup_id, 0),
                "evaluation_count": evaluation_count,
                "allocation_count": allocation_count,
                "portfolio_count": portfolio_count,
                "lifecycle_count": lifecycle_counts.get(setup_id, 0),
                "execution_count": execution_counts.get(setup_id, 0),
                "watch_state_counts": watch_state_counts.get(setup_id, {}),
                "current_watch_state_counts": current_watch_state_counts.get(setup_id, {}),
                "avg_setup_score": avg_setup_scores.get(setup_id),
                "avg_technical_score": avg_technical_scores.get(setup_id),
                "avg_fundamental_score": avg_fundamental_scores.get(setup_id),
                "near_miss_count": near_miss_counts.get(setup_id, 0),
                "screener_to_candidate": round(candidate_count / screener_universe_count, 4) if screener_universe_count else None,
                "candidate_to_watchlist": round(watchlist_count / candidate_count, 4) if candidate_count else None,
                "evaluation_to_allocation": round(allocation_count / evaluation_count, 4) if evaluation_count else None,
                "latest_event_verdict": latest_event_verdict.get(setup_id),
                "latest_event_source": latest_event_source.get(setup_id),
                "latest_portfolio_status": latest_portfolio_status.get(setup_id),
                "top_rejection_reason": None if top_reason is None else top_reason[0],
                "top_rejection_count": None if top_reason is None else top_reason[1],
            }
        )
    return pd.DataFrame(rows)


def _text_cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if text else "-"


def render_text_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "No setups found."
    columns = [
        ("setup_id", 22),
        ("regime_name", 20),
        ("overlay_name", 18),
        ("themes", 18),
        ("univ", 5),
        ("cand", 5),
        ("rej", 5),
        ("watch", 5),
        ("near", 5),
        ("score", 6),
        ("eval", 5),
        ("alloc", 5),
        ("port", 5),
        ("top_rejection_reason", 28),
    ]
    header = " ".join(label.ljust(width) for label, width in columns)
    separator = " ".join("-" * width for _, width in columns)
    lines = [header, separator]
    for _, row in df.iterrows():
        values = [
            _text_cell(row.get("setup_id"))[:22],
            _text_cell(row.get("regime_name"))[:20],
            _text_cell(row.get("overlay_name"))[:18],
            _text_cell(row.get("active_theme_ids"))[:18],
            _text_cell(row.get("screener_universe_count"))[:5],
            _text_cell(row.get("candidate_count"))[:5],
            _text_cell(row.get("rejection_count"))[:5],
            _text_cell(row.get("watchlist_count"))[:5],
            _text_cell(row.get("near_miss_count"))[:5],
            _text_cell(row.get("avg_setup_score"))[:6],
            _text_cell(row.get("evaluation_count"))[:5],
            _text_cell(row.get("allocation_count"))[:5],
            _text_cell(row.get("portfolio_count"))[:5],
            _text_cell(row.get("top_rejection_reason"))[:28],
        ]
        lines.append(" ".join(value.ljust(width) for value, (_, width) in zip(values, columns)))
    return "\n".join(lines)


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "status": "ok",
        "setup_count": int(len(df)),
        "rows": to_display_value(df) if not df.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show the advisory setup dashboard.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_dashboard(asof_date=asof_date, setup_ids=args.setup_ids)
    if args.format == "json":
        print(json.dumps(to_display_value(summarize(df)), indent=2, ensure_ascii=False, default=str))
    else:
        print(render_text_table(df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
